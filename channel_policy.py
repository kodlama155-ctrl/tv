#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

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
CATEGORY_INDEX = {name: i for i, name in enumerate(CATEGORY_ORDER)}

# Strong channel-level overrides. Source group-title values are often wrong,
# so known channels are classified by identity first.
CATEGORY_IDS = {
    "Ulusal": {
        "a2tv", "atv", "beyaztv", "cine1", "cnbce", "diyanettv", "diyartv",
        "dosttv", "fortunatv", "kanal7", "lalegultv", "nowtv", "sercemtv",
        "semerkandtv", "showtv", "startv", "teve2", "trt1", "trt2", "trteba",
        "trtgenc", "tv4", "tv8", "tv85", "360",
    },
    "Haber": {
        "24tv", "ahaber", "akittv", "benguturktv", "bloomberght", "cnnturk",
        "dha", "ekoturk", "finansturktv", "flashhabertv", "gzt", "haberglobal",
        "haberturktv", "halktv", "ilketv", "ntv", "sozcutv", "tbmmtv",
        "tele1", "tgrthaber", "trthaber", "turkhabertv", "tv100", "tvnet",
        "ulketv",
    },
    "Spor": {
        "aspor", "htsportv", "sportstv", "tjktv", "tjktv2", "trtspor",
        "trtsporyildiz",
    },
    "Film & Dizi": {
        "bbcfirst", "fx", "gempixel", "grandcinema", "kanald", "kanaldDrama",
    },
    "Çocuk": {
        "babyfirst", "babytv", "disneyjr", "minikacocuk", "minikago",
        "spacetoonturkey", "trtcocuk", "trtdiyanetcocuk", "zaroktv",
    },
    "Belgesel": {
        "lovenature", "trtbelgesel",
    },
    "Yaşam": {
        "ciftcitv", "dmax", "myzentv", "naturaltv", "stingraynaturescape",
        "tlc", "vavtv",
    },
    "Müzik": {
        "dreamturk", "knmusictv", "medmuzik", "number1ask", "number1damar",
        "number1dance", "number1tv", "powerdance", "powerlove", "powerturktv",
        "powerturkakustik", "powerturkslow", "powerturktaptaze", "powertv",
        "trtmuzik",
    },
    "Yerel": {
        "aksutv", "alanyapostatv", "altastv", "anadolunettv", "arastv",
        "astv", "atvalanya", "brtv", "caytv", "denizpostasitv", "dimtv",
        "edessatv", "ertv", "erzurumwebtv", "estv", "etvkayseri",
        "etvmanisa", "guneydogutv", "haber61tv", "hunattv", "iceltv",
        "kanal12", "kanal15", "kanal23", "kanal26", "kanal3", "kanal32",
        "kanal33", "kanal58", "kanalfirat", "kanalv", "kaytv", "kentturk",
        "kocaelitv", "konyaolaytv", "linetv", "mavikaradeniztv", "mercantv",
        "mturktv", "olayturktv", "sunrtv", "tempotv", "tontv", "tv1",
        "tv264", "tv41", "tv52", "tvden", "urfanatiktv", "van65tv",
    },
    "Uluslararası": {
        "adatv", "afroturktv", "almahriatv", "almazrah", "alzahratvturkic",
        "elsharqtv", "imamhusseintv5", "kanal7avrupa", "kurdistantv", "luystv",
        "manastv", "mctv", "mekameleentv", "persianaturkiye", "sat7turk",
        "trtarabi", "trtavaz", "trtkurdi", "trtturk", "trtworld",
        "westazerbaijantv", "yoltv", "eurod",
    },
}

# Fix a few identity spellings used by different lists.
IDENTITY_ALIASES = {
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

GROUP_MAP = {
    "general": "Ulusal",
    "genel": "Ulusal",
    "national": "Ulusal",
    "ulusal": "Ulusal",
    "entertainment": "Ulusal",
    "eglence": "Ulusal",
    "religious": "Ulusal",
    "religion": "Ulusal",
    "dini": "Ulusal",
    "education": "Ulusal",
    "educational": "Ulusal",
    "egitim": "Ulusal",
    "news": "Haber",
    "haber": "Haber",
    "sports": "Spor",
    "sport": "Spor",
    "spor": "Spor",
    "movie": "Film & Dizi",
    "movies": "Film & Dizi",
    "film": "Film & Dizi",
    "cinema": "Film & Dizi",
    "series": "Film & Dizi",
    "dizi": "Film & Dizi",
    "kids": "Çocuk",
    "children": "Çocuk",
    "cocuk": "Çocuk",
    "documentary": "Belgesel",
    "belgesel": "Belgesel",
    "lifestyle": "Yaşam",
    "yasam": "Yaşam",
    "music": "Müzik",
    "muzik": "Müzik",
    "local": "Yerel",
    "regional": "Yerel",
    "yerel": "Yerel",
    "international": "Uluslararası",
    "global": "Uluslararası",
    "world": "Uluslararası",
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
    {"id": "360", "name": "360", "category": "Ulusal"},
    {"id": "A2TV", "name": "A2", "category": "Ulusal"},
    {"id": "Teve2", "name": "Teve2", "category": "Ulusal"},
    {"id": "BeyazTV", "name": "Beyaz TV", "category": "Ulusal"},
    {"id": "TV4", "name": "TV4", "category": "Ulusal"},
    {"id": "TRT2", "name": "TRT 2", "category": "Ulusal"},
    {"id": "DiyanetTV", "name": "Diyanet TV", "category": "Ulusal"},
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


def _identity_category(identity: str) -> str | None:
    identity = IDENTITY_ALIASES.get(identity, identity)
    for category, ids in CATEGORY_IDS.items():
        normalized = {normalize_identity(x) for x in ids}
        if identity in normalized:
            return category
    return None


def category_for(meta: str) -> str:
    name = channel_name(meta)
    identity = channel_key(meta, name)

    explicit = _identity_category(identity)
    if explicit:
        return explicit

    country = country_from_tvg_id(meta)
    if country and country != "tr":
        return "Uluslararası"

    group = fold(existing_group(meta)).strip()
    if group in GROUP_MAP:
        return GROUP_MAP[group]

    haystack = fold(f"{existing_group(meta)} {name}")

    keyword_groups = [
        ("Yerel", ["local", "regional", "yerel", "belediye"]),
        ("Haber", ["news", "haber", "gundem", "gazete", "breaking"]),
        ("Spor", ["sports", "sport", "spor", "futbol", "football", "basketbol"]),
        ("Film & Dizi", ["movie", "film", "cinema", "sinema", "series", "serial", "dizi"]),
        ("Çocuk", ["kids", "children", "cocuk", "cartoon", "animation", "cizgi"]),
        ("Belgesel", ["documentary", "belgesel", "nature", "doga", "history", "science"]),
        ("Yaşam", ["lifestyle", "yasam", "gezi", "yemek", "travel", "food", "hobi"]),
        ("Müzik", ["music", "muzik", "radyo", "radio"]),
        ("Uluslararası", ["international", "global", "world"]),
    ]
    for category, keywords in keyword_groups:
        for keyword in keywords:
            if re.search(
                rf"(?<![a-z0-9]){re.escape(fold(keyword))}(?![a-z0-9])",
                haystack,
            ):
                return category

    # Turkish channels with no stronger signal are treated as national/general.
    if country == "tr":
        return "Ulusal"

    return "Diğer"


def normalize_meta(meta: str) -> tuple[str, str, str]:
    category = category_for(meta)
    head, label = split_extinf(meta)

    # Collapse duplicate group-title attributes left by malformed source metadata.
    head = re.sub(r'\s+group-title="[^"]*"', "", head, flags=re.I)
    head = head.rstrip() + f' group-title="{category}"'

    return f"{head},{label.strip()}", category, label.strip()


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


def representative_score(item: dict) -> tuple:
    status = item.get("status", "unknown")
    status_score = STATUS_PRIORITY.get(status, 2)
    quality = _resolution_height(item)
    stability = _host_quality(item.get("url", ""))

    name = fold(item.get("name", ""))
    if "not 24/7" in name:
        stability -= 100

    return (
        status_score,
        quality,
        stability,
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
        "core_channels_not_verified": len(CORE_CHANNELS) - verified_count,
        "status_counts": dict(status_counts),
        "missing_channels": missing,
    }
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result
