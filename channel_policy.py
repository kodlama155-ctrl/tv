#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

from category_engine import CATEGORY_ORDER
from name_engine import canonical_channel_name

CATEGORY_INDEX = {name: i for i, name in enumerate(CATEGORY_ORDER)}

# Legacy per-channel classification overrides were removed. Classification is
# owned exclusively by category_engine.py and learned from live platform/API
# evidence. Identity aliases below are only for duplicate-channel matching.
# Fix a few identity spellings used by different lists.
IDENTITY_ALIASES = {
    "a2": "a2tv",
    "now": "nowtv",
    "trt3trtspor": "trtspor",
    "bbcfirstturkiye": "bbcfirst",
    "ntvturkiye": "ntv",
    "kanalddrama": "kanalddrama",
    "htspor": "htsportv",
    "haberturk": "haberturktv",
    "benguturk": "benguturktv",
    "powerturk": "powerturktv",
    "tr24tv": "24tv",
    "tv8bucuk": "tv85",
    "tv85hd": "tv85",
    "cnnturkhd": "cnnturk",
    "sozcutvtr": "sozcutv",
}

CORE_CHANNELS = [
    # Ulusal
    {"id": "TRT1", "name": "TRT 1", "category": "Ulusal"},
    {"id": "ATV", "name": "ATV", "category": "Ulusal"},
    {"id": "KanalD", "name": "Kanal D", "category": "Ulusal"},
    {"id": "ShowTV", "name": "Show TV", "category": "Ulusal"},
    {"id": "StarTV", "name": "Star TV", "category": "Ulusal"},
    {"id": "NOWTV", "name": "NOW TV", "category": "Ulusal"},
    {"id": "TV8", "name": "TV8", "category": "Ulusal"},
    {"id": "TV85", "name": "TV8.5", "category": "Ulusal", "aliases": ["TV8 Bucuk"]},
    {"id": "Kanal7", "name": "Kanal 7", "category": "Ulusal"},
    {"id": "360", "name": "360", "category": "Haber"},
    {"id": "A2TV", "name": "A2", "category": "Ulusal"},
    {"id": "Teve2", "name": "Teve2", "category": "Ulusal"},
    {"id": "BeyazTV", "name": "Beyaz TV", "category": "Ulusal"},
    {"id": "TV4", "name": "TV4", "category": "Ulusal"},
    {"id": "TRT2", "name": "TRT 2", "category": "Ulusal"},
    {"id": "DiyanetTV", "name": "Diyanet TV", "category": "Dini"},
    {"id": "CNBCe", "name": "CNBC-e", "category": "Ulusal"},

    # Haber
    {"id": "TRTHaber", "name": "TRT Haber", "category": "Haber"},
    {"id": "NTV", "name": "NTV", "category": "Haber"},
    {"id": "CNNTurk", "name": "CNN Türk", "category": "Haber"},
    {"id": "HaberturkTV", "name": "Habertürk", "category": "Haber"},
    {"id": "HaberGlobal", "name": "Haber Global", "category": "Haber"},
    {"id": "AHaber", "name": "A Haber", "category": "Haber"},
    {"id": "TGRTHaber", "name": "TGRT Haber", "category": "Haber"},
    {"id": "TV100", "name": "TV100", "category": "Haber"},
    {"id": "SozcuTV", "name": "Sözcü TV", "category": "Haber"},
    {"id": "HalkTV", "name": "Halk TV", "category": "Haber"},
    {"id": "Tele1", "name": "Tele1", "category": "Haber"},
    {"id": "TVNET", "name": "TVNET", "category": "Haber"},
    {"id": "UlkeTV", "name": "Ülke TV", "category": "Haber"},
    {"id": "24TV", "name": "24 TV", "category": "Haber"},
    {"id": "BloombergHT", "name": "Bloomberg HT", "category": "Haber"},
    {"id": "Ekoturk", "name": "Ekotürk", "category": "Haber"},
    {"id": "BenguturkTV", "name": "BengüTürk", "category": "Haber"},

    # Spor
    {"id": "TRTSpor", "name": "TRT Spor", "category": "Spor"},
    {"id": "TRTSporYildiz", "name": "TRT Spor Yıldız", "category": "Spor"},
    {"id": "ASpor", "name": "A Spor", "category": "Spor"},
    {"id": "HTSporTV", "name": "HT Spor", "category": "Spor"},
    {"id": "TJKTV", "name": "TJK TV", "category": "Spor"},
    {"id": "FBTV", "name": "FB TV", "category": "Spor"},
    {"id": "EkolSports", "name": "Ekol Sports", "category": "Spor"},
    {"id": "beINSportsHaber", "name": "beIN Sports Haber", "category": "Spor"},
    {"id": "TRT3", "name": "TRT 3 Spor", "category": "Spor", "aliases": ["TRT 3"]},

    # Film/Dizi
    {"id": "FX", "name": "FX", "category": "Film & Dizi"},
    {"id": "BBCFirst", "name": "BBC First Türkiye", "category": "Film & Dizi"},
    {"id": "KanalDDrama", "name": "Kanal D Drama", "category": "Film & Dizi"},

    # Çocuk
    {"id": "TRTCocuk", "name": "TRT Çocuk", "category": "Çocuk"},
    {"id": "MinikaCocuk", "name": "Minika Çocuk", "category": "Çocuk"},
    {"id": "MinikaGo", "name": "Minika GO", "category": "Çocuk"},
    {"id": "BabyTV", "name": "BabyTV Türkiye", "category": "Çocuk"},
    {"id": "DisneyJr", "name": "Disney Junior", "category": "Çocuk"},

    # Belgesel / Yaşam
    {"id": "TRTBelgesel", "name": "TRT Belgesel", "category": "Belgesel"},
    {"id": "LoveNature", "name": "Love Nature", "category": "Belgesel"},
    {"id": "DMAX", "name": "DMAX", "category": "Yaşam"},
    {"id": "TLC", "name": "TLC", "category": "Yaşam"},

    # Müzik
    {"id": "TRTMuzik", "name": "TRT Müzik", "category": "Müzik"},
    {"id": "DreamTurk", "name": "Dream Türk", "category": "Müzik"},
    {"id": "PowerTurkTV", "name": "PowerTürk TV", "category": "Müzik"},
    {"id": "PowerTV", "name": "Power TV", "category": "Müzik"},
    {"id": "Number1TV", "name": "Number 1 TV", "category": "Müzik"},
]

STATUS_PRIORITY = {
    "verified": 5,
    "restricted": 4,
    "unknown": 3,
    "drm": 1,
    "dead": 0,
}

SOURCE_PRIORITY = {
    "official_api": 100,
    "official_browser": 95,
    "official_html": 90,
    "priority": 80,
    "iptv_org": 70,
    "upstream": 60,
    "github_discovery": 40,
    "unknown": 20,
}

PREFERRED_HOST_SUFFIXES = (
    "medya.trt.com.tr",
    "daioncdn.net",
    "ercdn.net",
    "mncdn.com",
    "lg.mncdn.com",
    "yayin.com.tr",
    "mediatriple.net",
    "socialsmart.tv",
)


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
        r"\([^)]*(?:\d{3,4}p|turkiye|turkey|hd|sd|uhd|4k|8k)[^)]*\)",
        "",
        value,
    )
    value = re.sub(r"\b(?:hd|sd|uhd|fhd)\b", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return IDENTITY_ALIASES.get(value, value)


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


def channel_name(meta: str) -> str:
    return split_extinf(meta)[1] or "Unknown"


def tvg_id(meta: str) -> str:
    m = re.search(r'tvg-id="([^"]*)"', meta, flags=re.I)
    return m.group(1).strip() if m else ""


def existing_group(meta: str) -> str:
    m = re.search(r'group-title="([^"]*)"', meta, flags=re.I)
    return m.group(1).strip() if m else ""


def tvg_logo(meta: str) -> str:
    m = re.search(r'tvg-logo="([^"]*)"', meta, flags=re.I)
    return m.group(1).strip() if m else ""


def _set_meta_attr(meta: str, attr: str, value: str) -> str:
    if not value:
        return meta
    head, label = split_extinf(meta)
    head = re.sub(
        rf'\s+{re.escape(attr)}="[^"]*"',
        "",
        head,
        flags=re.I,
    )
    safe = str(value).replace('"', "%22")
    head = head.rstrip() + f' {attr}="{safe}"'
    return f"{head},{label}"


def channel_key(meta: str, name: str | None = None) -> str:
    ident = normalize_identity(tvg_id(meta))
    if ident:
        return ident
    return normalize_identity(name if name is not None else channel_name(meta))


def country_from_tvg_id(meta: str) -> str:
    ident = tvg_id(meta)
    base = ident.split("@", 1)[0]
    if "." not in base:
        return ""
    return fold(base.rsplit(".", 1)[-1])


def category_decision(meta: str) -> dict:
    # Keep the classification engine separate so channel placement is decided
    # from platform/API evidence, not from ad-hoc overrides in this file.
    from category_engine import classify
    return classify(meta)


def category_for(meta: str) -> str:
    return category_decision(meta)["category"]

def canonical_name_decision(meta: str) -> dict:
    _, label = split_extinf(meta)
    name, source = canonical_channel_name(
        tvg_id(meta),
        label,
    )
    return {
        "name": name,
        "source": source,
        "original_name": label.strip(),
    }


def normalize_meta(meta: str) -> tuple[str, str, str]:
    category = category_for(meta)
    head, _ = split_extinf(meta)
    naming = canonical_name_decision(meta)
    label = naming["name"]

    # Collapse duplicate group-title attributes left by malformed source metadata.
    head = re.sub(r'\s+group-title="[^"]*"', "", head, flags=re.I)
    head = head.rstrip() + f' group-title="{category}"'

    return f"{head},{label}", category, label


def _resolution_height(item: dict) -> int:
    candidates = [
        str(item.get("resolution") or ""),
        str(item.get("name") or ""),
    ]
    for value in candidates:
        m = re.search(r"(\d{3,4})x(\d{3,4})", value)
        if m:
            return int(m.group(2))
        m = re.search(r"\b(\d{3,4})p\b", value, flags=re.I)
        if m:
            return int(m.group(1))
    return 0


def _host_quality(url: str) -> int:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return 0

    score = 0
    if url.lower().startswith("https://"):
        score += 80

    try:
        ipaddress.ip_address(host)
        score -= 80
    except ValueError:
        score += 30

    if any(host == suffix or host.endswith("." + suffix) for suffix in PREFERRED_HOST_SUFFIXES):
        score += 120

    return score


def _stability_score(item: dict) -> int:
    score = 100

    if item.get("retry_attempted"):
        score -= 30

    probes = int(item.get("segment_probes") or 1)
    score -= max(0, probes - 1) * 12

    name = fold(item.get("name", ""))
    if "not 24/7" in name:
        score -= 100

    return score


def _latency_score(item: dict) -> int:
    values = []
    for key in ("manifest_latency_ms", "segment_latency_ms"):
        try:
            value = int(item.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            values.append(value)

    if not values:
        return -999999

    return -sum(values)


def _durable_source_score(item: dict) -> int:
    score = SOURCE_PRIORITY.get(
        item.get("source_kind", "unknown"),
        SOURCE_PRIORITY["unknown"],
    )

    # Signed Turkuvaz-style URLs are valid at probe time but expire later.
    # Prefer a durable verified source for playlists consumed by devices.
    try:
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(item.get("url", "")).query
        )
    except Exception:
        query = {}

    keys = {str(key).lower() for key in query}
    if "st" in keys and "e" in keys:
        score -= 45

    return score


def representative_score(item: dict) -> tuple:
    status = item.get("status", "unknown")
    status_score = STATUS_PRIORITY.get(status, 2)
    source_score = _durable_source_score(item)
    quality = _resolution_height(item)

    try:
        bitrate = int(item.get("bandwidth") or 0)
    except (TypeError, ValueError):
        bitrate = 0

    stability = _stability_score(item)
    host_quality = _host_quality(item.get("url", ""))
    latency = _latency_score(item)

    return (
        status_score,
        source_score,
        quality,
        bitrate,
        stability,
        host_quality,
        latency,
        1 if item.get("url", "").startswith("https://") else 0,
    )


def select_representatives(items: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        key = item.get("channel_key") or channel_key(
            item.get("meta", ""),
            item.get("name", ""),
        )
        if not key:
            key = "url:" + item.get("url", "")
        grouped.setdefault(key, []).append(item)

    selected = []
    for key, variants in grouped.items():
        best = max(variants, key=representative_score)
        copy = dict(best)
        copy["channel_key"] = key
        copy["variant_count"] = len(variants)
        copy["geo_restricted"] = (
            copy.get("status") == "restricted"
            and any(bool(row.get("geo_hint")) for row in variants)
        )
        copy["device_fallback"] = (
            copy.get("status") in {"restricted", "unknown"}
            and any(bool(row.get("device_hint")) for row in variants)
        )

        # Preserve artwork even when the best stream variant lacks metadata.
        # Prefer logos from higher-trust variants.
        if not tvg_logo(copy.get("meta", "")):
            artwork_variants = sorted(
                variants,
                key=lambda row: SOURCE_PRIORITY.get(
                    row.get("source_kind", "unknown"),
                    SOURCE_PRIORITY["unknown"],
                ),
                reverse=True,
            )
            for row in artwork_variants:
                logo = tvg_logo(row.get("meta", ""))
                if logo:
                    copy["meta"] = _set_meta_attr(
                        copy.get("meta", ""),
                        "tvg-logo",
                        logo,
                    )
                    break

        selected.append(copy)

    return selected


def watchlist_keys(entry: dict) -> set[str]:
    values = [entry.get("id", ""), entry.get("name", "")]
    values.extend(entry.get("aliases", []))
    return {normalize_identity(v) for v in values if normalize_identity(v)}


def build_missing_report(validation_report: list[dict], output_path: Path) -> dict:
    by_key: dict[str, list[dict]] = {}
    for row in validation_report:
        key = row.get("channel_key") or normalize_identity(row.get("name", ""))
        if key:
            by_key.setdefault(key, []).append(row)

    missing = []
    verified_count = 0
    status_counts = Counter()

    for expected in CORE_CHANNELS:
        matches = []
        for key in watchlist_keys(expected):
            matches.extend(by_key.get(key, []))

        if matches:
            best = max(
                matches,
                key=lambda row: (
                    STATUS_PRIORITY.get(row.get("status", "unknown"), 2),
                    _resolution_height(row),
                ),
            )
            status = best.get("status", "unknown")
            if (
                status == "restricted"
                and any(bool(row.get("geo_hint")) for row in matches)
            ):
                status = "geo-restricted"
            elif (
                status == "restricted"
                and any(bool(row.get("device_hint")) for row in matches)
            ):
                status = "device-restricted"
            reason = best.get("reason", "")
            url = best.get("url", "")
        else:
            status = "absent"
            reason = "No candidate found in current sources"
            url = ""

        status_counts[status] += 1
        if status == "verified":
            verified_count += 1
            continue

        missing.append({
            "name": expected["name"],
            "expected_category": expected["category"],
            "status": status,
            "reason": reason,
            "candidate_url": url,
        })

    result = {
        "standard": "Core Turkish TV lineup tracked against current Türksat and major IPTV platform channel lineups",
        "core_channels_total": len(CORE_CHANNELS),
        "core_channels_verified": verified_count,
        "core_channels_geo_restricted": status_counts.get("geo-restricted", 0),
        "core_channels_device_restricted": status_counts.get("device-restricted", 0),
        "core_channels_not_verified": len(CORE_CHANNELS) - verified_count,
        "status_counts": dict(status_counts),
        "missing_channels": missing,
    }
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result
