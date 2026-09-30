#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import html
import json
import re
import unicodedata
import urllib.request
from collections import Counter
from functools import lru_cache
from html.parser import HTMLParser

from channel_catalog import category_for_channel

# EmirTV dynamic category/order engine.
#
# IMPORTANT:
# - No individual channel is hard-coded into a category.
# - The bot learns current placement/order from professional platform pages.
# - A broad "general", ".tr", religious or culture tag does NOT
#   automatically make a channel "Ulusal".
# - If reliable category evidence is missing, the channel goes to "Diğer".

CATEGORY_ORDER = [
    "Ulusal",
    "Haber",
    "Spor",
    "Film & Dizi",
    "Çocuk",
    "Belgesel",
    "Dini",
    "Müzik",
    "Yerel",
    "Uluslararası",
    "Diğer",
]

IPTV_ORG_CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
IPTV_ORG_FEEDS_URL = "https://iptv-org.github.io/api/feeds.json"

# Only professional CATEGORY URLs are stored here; never channel lists.
PLATFORM_PAGES = {
    "Tivibu": {
        "Ulusal": ["https://www.tivibu.com.tr/canli-tv/ulusal"],
        "Haber": ["https://www.tivibu.com.tr/canli-tv/haber"],
        "Spor": ["https://www.tivibu.com.tr/canli-tv/spor"],
        "Film & Dizi": [
            "https://www.tivibu.com.tr/canli-tv/dizi",
            "https://www.tivibu.com.tr/canli-tv/sinema",
        ],
        "Çocuk": ["https://www.tivibu.com.tr/canli-tv/cocuk"],
        "Belgesel": ["https://www.tivibu.com.tr/canli-tv/belgesel"],
        "Müzik": ["https://www.tivibu.com.tr/canli-tv/muzik"],
        "Uluslararası": ["https://www.tivibu.com.tr/canli-tv/global"],
    },
}

# TV+ category pages are client-rendered and their static HTML also contains
# unrelated footer/popular links. Searching the whole page can therefore
# produce false category matches. Use TV+ only as a live lineup/order source.
TVPLUS_ALL_URL = "https://tvplus.com.tr/canli-tv"

UA = "Mozilla/5.0 (EmirTV-CategoryBot/4.0)"

# Strong content-type mappings only.
# Broad tags such as general/entertainment/religious/culture are
# intentionally NOT mapped to Ulusal.
IPTV_CATEGORY_MAP = {
    "news": "Haber",
    "business": "Haber",
    "legislative": "Haber",
    "sports": "Spor",
    "movies": "Film & Dizi",
    "series": "Film & Dizi",
    "classic": "Film & Dizi",
    "comedy": "Film & Dizi",
    "animation": "Çocuk",
    "kids": "Çocuk",
    "documentary": "Belgesel",
    "science": "Belgesel",
    "history": "Belgesel",
    "lifestyle": "Diğer",
    "cooking": "Diğer",
    "travel": "Diğer",
    "auto": "Diğer",
    "outdoor": "Diğer",
    "shop": "Diğer",
    "religious": "Dini",
    "education": "Belgesel",
    "music": "Müzik",
}

GROUP_MAP = {
    "haber": "Haber",
    "news": "Haber",
    "spor": "Spor",
    "sport": "Spor",
    "sports": "Spor",
    "film": "Film & Dizi",
    "movie": "Film & Dizi",
    "movies": "Film & Dizi",
    "cinema": "Film & Dizi",
    "sinema": "Film & Dizi",
    "dizi": "Film & Dizi",
    "series": "Film & Dizi",
    "cocuk": "Çocuk",
    "kids": "Çocuk",
    "children": "Çocuk",
    "belgesel": "Belgesel",
    "documentary": "Belgesel",
    "yasam": "Diğer",
    "lifestyle": "Diğer",
    "religious": "Dini",
    "religion": "Dini",
    "dini": "Dini",
    "education": "Belgesel",
    "educational": "Belgesel",
    "egitim": "Belgesel",
    "muzik": "Müzik",
    "music": "Müzik",
    "yerel": "Yerel",
    "local": "Yerel",
    "regional": "Yerel",
    "uluslararasi": "Uluslararası",
    "international": "Uluslararası",
    "global": "Uluslararası",
}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.channel_links: list[str] = []
        self._channel_anchor = False
        self._channel_anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href", "")
        if "/kanallar/" in href:
            self._channel_anchor = True
            self._channel_anchor_parts = []

    def handle_endtag(self, tag: str):
        if tag.lower() != "a" or not self._channel_anchor:
            return
        label = " ".join(self._channel_anchor_parts).strip()
        if label:
            self.channel_links.append(label)
        self._channel_anchor = False
        self._channel_anchor_parts = []

    def handle_data(self, data: str):
        value = " ".join(data.split())
        if value:
            self.parts.append(value)
            if self._channel_anchor:
                self._channel_anchor_parts.append(value)


def fold(text: str) -> str:
    text = str(text or "").replace("ı", "i").replace("İ", "I")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def normalize_identity(text: str) -> str:
    value = fold(text)
    value = re.sub(r"@[^@]+$", "", value)
    value = re.sub(r"\.(?:tr|cy|uk|de|fr|az|iq|ir|ca|us|kg)$", "", value)
    value = re.sub(r"\[[^\]]*\]", "", value)
    value = re.sub(
        r"\([^)]*(?:\d{3,4}p|turkiye|turkey|hd|sd|uhd|4k|8k|geo-blocked|not 24/7)[^)]*\)",
        "",
        value,
    )
    value = re.sub(r"\b(?:hd|sd|uhd|fhd|4k|8k)\b", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def split_extinf(meta: str) -> tuple[str, str]:
    quoted = False
    escaped = False
    for i, ch in enumerate(meta):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            quoted = not quoted
            continue
        if ch == "," and not quoted:
            return meta[:i], meta[i + 1 :].strip()
    return meta, "Unknown"


def _attr(meta: str, name: str) -> str:
    m = re.search(rf'{re.escape(name)}="([^"]*)"', meta, flags=re.I)
    return m.group(1).strip() if m else ""


def _tvg_base(meta: str) -> str:
    return _attr(meta, "tvg-id").split("@", 1)[0].strip()


def _country(meta: str) -> str:
    base = _tvg_base(meta)
    if "." not in base:
        return ""
    return fold(base.rsplit(".", 1)[-1])


def _clean_display_name(name: str) -> str:
    value = re.sub(r"\[[^\]]*\]", " ", name)
    value = re.sub(
        r"\([^)]*(?:\d{3,4}p|turkiye|turkey|hd|sd|uhd|4k|8k|geo-blocked|not 24/7)[^)]*\)",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\b(?:2160p|1440p|1080p|900p|720p|576p|540p|480p|360p|288p)\b",
        " ",
        value,
        flags=re.I,
    )
    return " ".join(value.split()).strip()


def _camel_words(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return value.replace("_", " ").replace("-", " ")


def _match_variants(meta: str) -> list[str]:
    display = _clean_display_name(split_extinf(meta)[1])
    base = _tvg_base(meta)
    if "." in base:
        base = base.rsplit(".", 1)[0]

    values = [display, _camel_words(base), base]
    out: list[str] = []
    seen: set[str] = set()

    for value in values:
        value = fold(value)
        value = re.sub(r"[^a-z0-9]+", " ", value).strip()
        if not value:
            continue

        variants = [value]

        for suffix in (" turkiye", " turkey", " hd", " sd"):
            if value.endswith(suffix):
                variants.append(value[: -len(suffix)].strip())

        # Generic trailing-TV cleanup: "Haberturk TV" -> "Haberturk".
        if value.endswith(" tv") and len(value) > 5 and not value.startswith("tv"):
            variants.append(value[:-3].strip())

        compact = re.sub(r"\s+", "", value)
        if compact.endswith("tv") and not compact.startswith("tv"):
            short = compact[:-2]
            if len(short) >= 2:
                variants.append(short)

        for variant in variants:
            compact_variant = re.sub(r"\s+", "", variant)
            # Allow short alpha-numeric service names such as A2, but reject
            # generic one-character matches.
            if len(compact_variant) < 2 or variant in seen:
                continue
            seen.add(variant)
            out.append(variant)

    return out


def _label_keys(value: str) -> set[str]:
    value = fold(value)
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    if not value:
        return set()

    keys = {value, re.sub(r"\s+", "", value)}

    compact_alias = re.sub(r"\s+", "", value)
    # Tivibu brands Teve2 as "TV2" in its Ulusal rail.
    if compact_alias in {"tv2", "teve2"}:
        keys.update({"tv2", "teve2"})

    # Tivibu labels the TRT 3 feed as "TRT 3 SPOR".
    if compact_alias in {"trt3", "trt3spor"}:
        keys.update({"trt3", "trt3spor"})

    if value.endswith(" tv") and len(value) > 5 and not value.startswith("tv"):
        short = value[:-3].strip()
        keys.add(short)
        keys.add(re.sub(r"\s+", "", short))

    compact = re.sub(r"\s+", "", value)
    if compact.endswith("tv") and not compact.startswith("tv") and len(compact[:-2]) >= 2:
        keys.add(compact[:-2])

    return {x for x in keys if x}


@lru_cache(maxsize=32)
def _fetch_page(url: str) -> dict:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read(6_000_000).decode("utf-8", errors="replace")

        parser = _TextParser()
        try:
            parser.feed(raw)
        except Exception:
            pass

        parts = [html.unescape(x).strip() for x in parser.parts if x.strip()]
        visible = fold(" \n ".join(parts))

        # TV+ may serialize scoped channel cards in HTML/JSON.
        raw_folded = fold(html.unescape(raw)) if "tvplus.com.tr" in url else ""

        return {
            "ok": True,
            "parts": parts,
            "visible": visible,
            "raw": raw_folded,
            "channels": parser.channel_links,
        }
    except Exception as exc:
        return {
            "ok": False,
            "parts": [],
            "visible": "",
            "raw": "",
            "channels": [],
            "error": type(exc).__name__,
        }


@lru_cache(maxsize=1)
def _prefetch_pages() -> bool:
    urls: list[str] = []
    for category_pages in PLATFORM_PAGES.values():
        for page_urls in category_pages.values():
            urls.extend(page_urls)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_fetch_page, sorted(set(urls))))
    return True


@lru_cache(maxsize=32)
def _tivibu_catalog(url: str) -> tuple[str, ...]:
    page = _fetch_page(url)
    if not page["ok"]:
        return ()

    parts: list[str] = page["parts"]

    # Tivibu category page structure:
    # date selector -> CHANNEL -> current programme -> CHANNEL -> programme...
    # then the full EPG begins and contains clock ranges/arrow.
    #
    # We intentionally extract ONLY this first category rail. This avoids the
    # old bug where a channel mentioned later in EPG data appeared to belong to
    # every Tivibu category.
    date_indexes = [
        i
        for i, part in enumerate(parts[:300])
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", part.strip())
    ]
    if not date_indexes:
        # Some category pages (notably Global) can omit the date rail in the
        # server-rendered response. Only then fall back to channel-card links.
        linked_channels = []
        linked_seen = set()
        for label in page.get("channels", []):
            clean = " ".join(str(label).split()).strip()
            key = fold(clean)
            if clean and key not in linked_seen:
                linked_seen.add(key)
                linked_channels.append(clean)
        return tuple(linked_channels[:200])

    i = date_indexes[-1] + 1
    while i < len(parts) and fold(parts[i]) in {"dun", "bugun", "yarin"}:
        i += 1

    channels: list[str] = []
    while i < len(parts):
        channel = parts[i].strip()

        # First real EPG entry: stop before the programme grid.
        if (
            "→" in channel
            or re.search(r"\b\d{1,2}:\d{2}\b", channel)
            or ("canli" in fold(channel) and len(channels) >= 2)
        ):
            break

        if not channel:
            i += 1
            continue

        channels.append(channel)

        # The rail alternates channel name / current programme title.
        i += 2

        if len(channels) > 150:
            return ()

    return tuple(channels)


@lru_cache(maxsize=1)
def _tvplus_catalog() -> tuple[str, ...]:
    page = _fetch_page(TVPLUS_ALL_URL)
    if not page["ok"]:
        return ()

    parts: list[str] = page["parts"]
    folded = [fold(x) for x in parts]

    try:
        start = next(
            i for i, value in enumerate(folded)
            if value == "tum kanallar"
        )
    except StopIteration:
        return ()

    # The TV+ all-channel rail begins after the category selector. Starting
    # after "KKTC Yerel" keeps category labels out of the channel catalog.
    selector_end = None
    for i in range(start + 1, min(len(parts), start + 100)):
        if folded[i] == "kktc yerel":
            selector_end = i + 1
            break
    if selector_end is None:
        return ()

    stop_labels = {
        "tv+ta simdi ne var?",
        "tv+'ta simdi ne var?",
        "tv+’ta simdi ne var?",
        "cihazlar",
    }

    channels: list[str] = []
    seen: set[str] = set()
    for part in parts[selector_end:]:
        value = part.strip()
        value_fold = fold(value)

        if value_fold in stop_labels or value_fold.startswith("tv+ta simdi ne var"):
            break
        if not value or len(value) > 100:
            continue

        keys = _label_keys(value)
        if not keys:
            continue

        primary = min(keys, key=len)
        if primary in seen:
            continue
        seen.add(primary)
        channels.append(value)

        if len(channels) > 400:
            break

    return tuple(channels)


def _catalog_position(meta: str, catalog: tuple[str, ...]) -> int | None:
    candidate_keys: set[str] = set()
    for variant in _match_variants(meta):
        candidate_keys.update(_label_keys(variant))

    if not candidate_keys:
        return None

    for pos, label in enumerate(catalog):
        if candidate_keys & _label_keys(label):
            return pos

    return None


def _variant_position(text: str, variants: list[str]) -> int | None:
    best = None
    for variant in variants:
        pattern = rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])"
        m = re.search(pattern, text)
        if m is not None:
            best = m.start() if best is None else min(best, m.start())
    return best


@lru_cache(maxsize=4096)
def _platform_matches(meta: str) -> tuple[tuple, ...]:
    _prefetch_pages()
    matches: list[tuple] = []

    for platform, category_pages in PLATFORM_PAGES.items():
        for category, urls in category_pages.items():
            best = None

            for url in urls:
                page = _fetch_page(url)
                if not page["ok"]:
                    continue

                if platform == "Tivibu":
                    pos = _catalog_position(meta, _tivibu_catalog(url))
                    confidence = "catalog"
                else:
                    # TV+ page itself is scoped to Yerel, so visible/embedded
                    # matching cannot leak a channel from another category page.
                    variants = _match_variants(meta)
                    pos = _variant_position(page["visible"], variants)
                    confidence = "visible"
                    if pos is None:
                        pos = _variant_position(page["raw"], variants)
                        confidence = "embedded"

                if pos is not None and (best is None or pos < best[0]):
                    best = (pos, confidence, url)

            if best is not None:
                matches.append(
                    (
                        platform,
                        category,
                        best[0],
                        best[1],
                        best[2],
                    )
                )

    return tuple(matches)


@lru_cache(maxsize=1)
def _iptv_org_index() -> dict[str, list[str]]:
    try:
        req = urllib.request.Request(
            IPTV_ORG_CHANNELS_URL,
            headers={"User-Agent": UA, "Accept": "application/json,*/*"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read(12_000_000).decode("utf-8"))

        out = {}
        for row in data:
            channel_id = str(row.get("id") or "").strip().casefold()
            if not channel_id:
                continue
            out[channel_id] = [
                str(x).strip().lower()
                for x in (row.get("categories") or [])
                if str(x).strip()
            ]
        return out
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _iptv_org_feed_areas() -> dict[str, tuple[str, ...]]:
    try:
        req = urllib.request.Request(
            IPTV_ORG_FEEDS_URL,
            headers={"User-Agent": UA, "Accept": "application/json,*/*"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read(20_000_000).decode("utf-8"))

        out: dict[str, set[str]] = {}
        for row in data:
            channel_id = str(row.get("channel") or "").strip().casefold()
            if not channel_id:
                continue
            areas = {
                str(x).strip().lower()
                for x in (row.get("broadcast_area") or [])
                if str(x).strip()
            }
            if areas:
                out.setdefault(channel_id, set()).update(areas)

        return {
            channel_id: tuple(sorted(areas))
            for channel_id, areas in out.items()
        }
    except Exception:
        return {}


def _is_turkey_local_area(area: str) -> bool:
    value = str(area or "").strip().lower()
    return value.startswith("s/tr-") or value.startswith("ct/tr")


@lru_cache(maxsize=4096)
def classify(meta: str) -> dict:
    name = split_extinf(meta)[1]
    tvg = _attr(meta, "tvg-id")

    curated = category_for_channel(tvg, name)
    if curated:
        return {
            "category": curated,
            "source": "emirtv-catalog",
            "score": 1000,
            "votes": {curated: 1000},
            "evidence": ["EmirTV:authoritative-catalog"],
            "platform_matches": [],
        }

    # Unknown channels are never promoted into a curated category from
    # third-party metadata. External platforms remain discovery/order sources.
    return {
        "category": "Diğer",
        "source": "unreviewed",
        "score": 0,
        "votes": {},
        "evidence": ["not-in-emirtv-catalog->Diğer"],
        "platform_matches": [],
    }


@lru_cache(maxsize=4096)
def order_decision(meta: str, category: str | None = None) -> dict:
    if category is None:
        category = classify(meta)["category"]

    rows = []
    for platform, cat, position, confidence, url in _platform_matches(meta):
        if cat != category:
            continue
        rows.append(
            {
                "platform": platform,
                "position": position,
                "confidence": confidence,
                "url": url,
            }
        )

    # TV+ publishes a current all-channel lineup. It is valuable for ordering
    # but not used as category evidence because its category pages are
    # client-rendered and static HTML can contain unrelated footer links.
    tvplus_position = _catalog_position(meta, _tvplus_catalog())
    if tvplus_position is not None:
        rows.append(
            {
                "platform": "TV+",
                "position": tvplus_position,
                "confidence": "global-catalog",
                "url": TVPLUS_ALL_URL,
            }
        )

    if not rows:
        return {
            "known": False,
            "score": 999999999.0,
            "category": category,
            "sources": 0,
            "evidence": [],
        }

    platforms = {row["platform"] for row in rows}
    score = sum(float(row["position"]) for row in rows) / len(rows)

    return {
        "known": True,
        "score": score,
        "category": category,
        "sources": len(platforms),
        "evidence": rows,
    }


def channel_sort_key(
    meta: str,
    category: str | None = None,
    name: str | None = None,
):
    if category is None:
        category = classify(meta)["category"]
    if name is None:
        name = split_extinf(meta)[1]

    decision = order_decision(meta, category)

    return (
        0 if decision["known"] else 1,
        -decision["sources"],
        decision["score"],
        fold(name),
        normalize_identity(_tvg_base(meta) or name),
    )
