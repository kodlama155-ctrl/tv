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

# EmirTV dynamic category/order engine.
#
# IMPORTANT:
# - No individual channel is hard-coded into a category.
# - The bot learns current placement/order from professional platform pages.
# - A broad "general", ".tr", religious, education or culture tag does NOT
#   automatically make a channel "Ulusal".
# - If reliable category evidence is missing, the channel goes to "Diğer".

CATEGORY_ORDER = [
    "Ulusal",
    "Haber",
    "Spor",
    "Film & Dizi",
    "Çocuk",
    "Belgesel",
    "Yaşam",
    "Müzik",
    "Yerel",
    "Uluslararası",
    "Diğer",
]

IPTV_ORG_CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"

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
        "Yaşam": ["https://www.tivibu.com.tr/canli-tv/yasam-stil"],
        "Müzik": ["https://www.tivibu.com.tr/canli-tv/muzik"],
        "Uluslararası": ["https://www.tivibu.com.tr/canli-tv/global"],
    },
    # TV+ has an explicit Yerel category. We use only that scoped page as a
    # second professional source, again without storing individual channel IDs.
    "TV+": {
        "Yerel": ["https://tvplus.com.tr/canli-tv/kategori/yerel"],
    },
}

UA = "Mozilla/5.0 (EmirTV-CategoryBot/4.0)"

# Strong content-type mappings only.
# Broad tags such as general/entertainment/religious/education/culture are
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
    "lifestyle": "Yaşam",
    "cooking": "Yaşam",
    "travel": "Yaşam",
    "auto": "Yaşam",
    "outdoor": "Yaşam",
    "shop": "Yaşam",
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
    "yasam": "Yaşam",
    "lifestyle": "Yaşam",
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

    def handle_data(self, data: str):
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


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
        }
    except Exception as exc:
        return {
            "ok": False,
            "parts": [],
            "visible": "",
            "raw": "",
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
        return ()

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


@lru_cache(maxsize=4096)
def classify(meta: str) -> dict:
    name = split_extinf(meta)[1]
    group = fold(_attr(meta, "group-title")).strip()
    country = _country(meta)

    scores = Counter()
    evidence: list[str] = []

    matches = _platform_matches(meta)
    for platform, category, position, confidence, url in matches:
        if confidence == "catalog":
            weight = 160
        elif confidence == "visible":
            weight = 110
        else:
            weight = 80

        scores[category] += weight
        evidence.append(f"{platform}:{category}:{confidence}")

    base = _tvg_base(meta).casefold()
    if base:
        for raw_category in _iptv_org_index().get(base, []):
            mapped = IPTV_CATEGORY_MAP.get(raw_category)
            if mapped:
                scores[mapped] += 35
                evidence.append(f"iptv-org:{raw_category}->{mapped}")

    if group in GROUP_MAP:
        mapped = GROUP_MAP[group]
        scores[mapped] += 18
        evidence.append(f"source-group:{group}->{mapped}")

    name_fold = fold(_clean_display_name(name))
    keyword_groups = [
        ("Haber", ["haber", "news"]),
        ("Spor", ["spor", "sport", "sports"]),
        ("Film & Dizi", ["drama", "dizi", "film", "movie", "cinema", "sinema"]),
        ("Çocuk", ["cocuk", "kids", "kid", "cartoon"]),
        ("Belgesel", ["belgesel", "documentary"]),
        ("Yaşam", ["yasam", "lifestyle"]),
        ("Müzik", ["muzik", "music", "radyo", "radio"]),
        ("Yerel", ["yerel", "local", "regional"]),
    ]

    for category, keywords in keyword_groups:
        if any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(fold(keyword))}(?![a-z0-9])",
                name_fold,
            )
            for keyword in keywords
        ):
            scores[category] += 25
            evidence.append(f"channel-name:{category}")

    # Foreign origin is a fallback signal only. Strong platform/content
    # evidence can still classify a foreign-origin Türkiye service as Çocuk,
    # Film & Dizi, etc.
    if country and country != "tr":
        scores["Uluslararası"] += 20
        evidence.append(f"country:{country}->Uluslararası")

    # Strict Ulusal: ONLY a live professional Ulusal category match may create
    # the category. No .tr/general/entertainment/religious fallback exists.
    has_ulusal_platform = any(
        category == "Ulusal"
        for _, category, _, _, _ in matches
    )
    if not has_ulusal_platform:
        scores.pop("Ulusal", None)

    if not scores:
        return {
            "category": "Diğer",
            "source": "fallback",
            "score": 0,
            "votes": {},
            "evidence": ["no-reliable-category-evidence"],
            "platform_matches": [],
        }

    category = max(
        CATEGORY_ORDER,
        key=lambda c: (scores.get(c, 0), -CATEGORY_ORDER.index(c)),
    )

    platform_rows = [
        {
            "platform": platform,
            "category": cat,
            "position": position,
            "confidence": confidence,
            "url": url,
        }
        for platform, cat, position, confidence, url in matches
    ]

    if any(row["category"] == category for row in platform_rows):
        source = "platform-live"
    elif any(
        e.startswith("iptv-org:") and e.endswith("->" + category)
        for e in evidence
    ):
        source = "iptv-org"
    elif group in GROUP_MAP and GROUP_MAP[group] == category:
        source = "source-group"
    else:
        source = "fallback"

    return {
        "category": category,
        "source": source,
        "score": scores[category],
        "votes": dict(scores),
        "evidence": evidence,
        "platform_matches": platform_rows,
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
