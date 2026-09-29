#!/usr/bin/env python3
from __future__ import annotations

import html
import concurrent.futures
import json
import re
import unicodedata
import urllib.request
from collections import Counter
from functools import lru_cache
from html.parser import HTMLParser

# EmirTV dynamic category/order engine.
#
# There are deliberately NO hard-coded channel-to-category lists in this file.
# The bot learns placement from current platform category pages on every run.
# If platforms do not classify a channel, it falls back to content metadata.
#
# Priority:
#   1) current professional platform category pages
#   2) iptv-org live channel metadata
#   3) strong channel/source content signals
#   4) foreign-country signal
#   5) Diğer
#
# Important: being a Turkish channel is NOT enough to become "Ulusal".

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

# Category URLs only. No individual channels are stored here.
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
    "TV+": {
        "Haber": ["https://tvplus.com.tr/canli-tv/kategori/haber"],
        "Spor": ["https://tvplus.com.tr/canli-tv/kategori/spor"],
        "Film & Dizi": ["https://tvplus.com.tr/canli-tv/kategori/filmdizi"],
        "Çocuk": ["https://tvplus.com.tr/canli-tv/kategori/cocuk"],
        "Belgesel": ["https://tvplus.com.tr/canli-tv/kategori/belgesel"],
        "Yaşam": ["https://tvplus.com.tr/canli-tv/kategori/yasam"],
        "Yerel": ["https://tvplus.com.tr/canli-tv/kategori/yerel"],
    },
}

# Digiturk is parsed dynamically by section heading when the public page exposes
# those headings in server-rendered HTML. If the page shape changes, it simply
# contributes no vote instead of inventing data.
DIGITURK_ALL_URL = "https://www.digiturk.com.tr/AllChannels"
DIGITURK_HEADINGS = {
    "Ulusal": ("ulusal", "genel"),
    "Haber": ("haber",),
    "Spor": ("spor",),
    "Film & Dizi": ("film", "dizi", "sinema"),
    "Çocuk": ("cocuk",),
    "Belgesel": ("belgesel",),
    "Yaşam": ("yasam", "eglence"),
    "Müzik": ("muzik",),
    "Uluslararası": ("uluslararasi", "global"),
}

# Only content-type mappings. Broad labels such as general/entertainment,
# religious/education/culture do NOT automatically mean "Ulusal".
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

UA = "Mozilla/5.0 (EmirTV-CategoryBot/2.0)"


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
    value = re.sub(r"\b(?:1080p|900p|720p|576p|540p|480p|360p|288p|1440p|2160p)\b", " ", value, flags=re.I)
    return " ".join(value.split()).strip()


def _camel_words(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return value.replace("_", " ").replace("-", " ")


def _match_variants(meta: str) -> list[str]:
    name = _clean_display_name(split_extinf(meta)[1])
    base = _tvg_base(meta)
    if "." in base:
        base = base.rsplit(".", 1)[0]

    raw = [
        name,
        _camel_words(base),
        base,
    ]

    variants = []
    seen = set()
    for item in raw:
        item = fold(item)
        item = re.sub(r"[^a-z0-9]+", " ", item).strip()
        if not item:
            continue

        candidates = [item]

        # Generic suffix cleanup, not channel-specific rules.
        if item.endswith(" turkiye"):
            candidates.append(item[:-8].strip())
        if item.endswith(" turkey"):
            candidates.append(item[:-7].strip())
        if item.endswith(" hd"):
            candidates.append(item[:-3].strip())
        if item.endswith(" tv") and len(item) > 5 and not item.startswith("tv"):
            candidates.append(item[:-3].strip())

        compact = re.sub(r"\s+", "", item)
        if compact:
            candidates.append(compact)

        for candidate in candidates:
            if len(candidate) < 3 or candidate in seen:
                continue
            seen.add(candidate)
            variants.append(candidate)

    return variants


def _visible_text(raw_html: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(raw_html)
    except Exception:
        pass
    return " \n ".join(parser.parts)


@lru_cache(maxsize=64)
def _fetch_page(url: str) -> dict:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read(8_000_000).decode("utf-8", errors="replace")
        visible = _visible_text(raw)
        return {
            "ok": True,
            "visible": fold(html.unescape(visible)),
            "raw": fold(html.unescape(raw)),
        }
    except Exception as exc:
        return {
            "ok": False,
            "visible": "",
            "raw": "",
            "error": type(exc).__name__,
        }


def _variant_position(text: str, variants: list[str]) -> int | None:
    best = None
    for variant in variants:
        # Prevent tiny identities such as "atv" matching inside a larger word.
        pattern = rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])"
        match = re.search(pattern, text)
        if match is None:
            compact = re.sub(r"\s+", "", variant)
            if len(compact) >= 5:
                pattern = rf"(?<![a-z0-9]){re.escape(compact)}(?![a-z0-9])"
                match = re.search(pattern, re.sub(r"\s+", "", text))
        if match is not None:
            pos = match.start()
            best = pos if best is None else min(best, pos)
    return best


@lru_cache(maxsize=1)
def _prefetch_platform_pages() -> bool:
    urls = {DIGITURK_ALL_URL}
    for category_pages in PLATFORM_PAGES.values():
        for page_urls in category_pages.values():
            urls.update(page_urls)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_fetch_page, sorted(urls)))
    return True


@lru_cache(maxsize=4096)
def _platform_matches(meta: str) -> list[dict]:
    _prefetch_platform_pages()
    variants = _match_variants(meta)
    matches = []

    for platform, category_pages in PLATFORM_PAGES.items():
        for category, urls in category_pages.items():
            best_position = None
            matched_url = None
            for url in urls:
                page = _fetch_page(url)
                if not page["ok"]:
                    continue

                # Visible page text is high confidence.
                pos = _variant_position(page["visible"], variants)
                confidence = "visible"

                # Some JS sites keep channel cards only in serialized HTML data.
                if pos is None:
                    pos = _variant_position(page["raw"], variants)
                    confidence = "embedded"

                if pos is not None and (
                    best_position is None or pos < best_position
                ):
                    best_position = pos
                    matched_url = url
                    matched_confidence = confidence

            if best_position is not None:
                matches.append({
                    "platform": platform,
                    "category": category,
                    "position": best_position,
                    "confidence": matched_confidence,
                    "url": matched_url,
                })

    return matches


@lru_cache(maxsize=1)
def _digiturk_sections() -> dict[str, str]:
    _prefetch_platform_pages()
    page = _fetch_page(DIGITURK_ALL_URL)
    if not page["ok"]:
        return {}

    text = page["visible"]
    headings = []
    for category, aliases in DIGITURK_HEADINGS.items():
        positions = []
        for alias in aliases:
            m = re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                text,
            )
            if m:
                positions.append(m.start())
        if positions:
            headings.append((min(positions), category))

    if len(headings) < 2:
        return {}

    headings.sort()
    sections = {}
    for i, (start, category) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        sections[category] = text[start:end]
    return sections


@lru_cache(maxsize=4096)
def _digiturk_match(meta: str) -> list[dict]:
    variants = _match_variants(meta)
    out = []
    for category, section in _digiturk_sections().items():
        pos = _variant_position(section, variants)
        if pos is not None:
            out.append({
                "platform": "Digiturk",
                "category": category,
                "position": pos,
                "confidence": "section",
                "url": DIGITURK_ALL_URL,
            })
    return out


@lru_cache(maxsize=1)
def _iptv_org_index() -> dict[str, list[str]]:
    try:
        req = urllib.request.Request(
            IPTV_ORG_CHANNELS_URL,
            headers={"User-Agent": UA, "Accept": "application/json,*/*"},
        )
        with urllib.request.urlopen(req, timeout=20) as response:
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
def _all_platform_matches(meta: str) -> list[dict]:
    return _platform_matches(meta) + _digiturk_match(meta)


@lru_cache(maxsize=4096)
def classify(meta: str) -> dict:
    name = split_extinf(meta)[1]
    group = fold(_attr(meta, "group-title")).strip()
    country = _country(meta)

    scores = Counter()
    evidence = []

    platform_matches = _all_platform_matches(meta)
    for match in platform_matches:
        weight = 100 if match["confidence"] in {"visible", "section"} else 70
        scores[match["category"]] += weight
        evidence.append(
            f'{match["platform"]}:{match["category"]}:{match["confidence"]}'
        )

    base = _tvg_base(meta).casefold()
    if base:
        for raw_category in _iptv_org_index().get(base, []):
            mapped = IPTV_CATEGORY_MAP.get(raw_category)
            if mapped:
                scores[mapped] += 35
                evidence.append(f"iptv-org:{raw_category}->{mapped}")

    # Source group is useful for strong content/local labels only.
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

    # Foreign origin is only a fallback signal. Content/platform evidence wins.
    if country and country != "tr":
        scores["Uluslararası"] += 20
        evidence.append(f"country:{country}->Uluslararası")

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

    if any(m["category"] == category for m in platform_matches):
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
        "platform_matches": platform_matches,
    }


@lru_cache(maxsize=4096)
def order_decision(meta: str, category: str | None = None) -> dict:
    if category is None:
        category = classify(meta)["category"]

    matches = [
        m for m in _all_platform_matches(meta)
        if m["category"] == category
    ]

    if not matches:
        return {
            "known": False,
            "score": 999999999.0,
            "category": category,
            "sources": 0,
            "evidence": [],
        }

    # More independent platforms = stronger professional ordering evidence.
    platforms = sorted({m["platform"] for m in matches})

    # Use page position normalized only among matched sources. Absolute position
    # is sufficient for stable ordering within the same platform page and the
    # multi-platform source count prevents one weak page from beating consensus.
    score = sum(float(m["position"]) for m in matches) / len(matches)

    return {
        "known": True,
        "score": score,
        "category": category,
        "sources": len(platforms),
        "evidence": matches,
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
