#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
import urllib.request
from collections import Counter
from functools import lru_cache

# EmirTV category engine.
#
# Decision order is evidence-based instead of one giant hand-written channel map:
#   1) current professional-platform placement (Tivibu + TV+ + Digiturk reference)
#   2) live iptv-org channel metadata (channels.json) by exact tvg-id
#   3) regional/international identity signals
#   4) source group-title and channel-name keywords
#   5) Turkish/general fallback
#
# Platform reference was rebuilt from current 2026 pages:
#   https://www.tivibu.com.tr/canli-tv/ulusal
#   https://www.tivibu.com.tr/canli-tv/haber
#   https://www.tivibu.com.tr/canli-tv/spor
#   https://www.tivibu.com.tr/canli-tv/dizi
#   https://www.tivibu.com.tr/canli-tv/cocuk
#   https://www.tivibu.com.tr/canli-tv/belgesel
#   https://www.tivibu.com.tr/canli-tv/yasam-stil
#   https://www.tivibu.com.tr/canli-tv/muzik
#   https://tvplus.com.tr/canli-tv
#   https://tvplus.com.tr/destek/sss/tvplus-kanallari
#   https://www.digiturk.com.tr/AllChannels
# D-Smart's current public pages confirm the same broad taxonomy (ulusal,
# haber, spor, film/dizi, belgesel, çocuk, müzik, yaşam) but do not expose a
# reliable machine-readable per-channel category list, so we do not invent
# channel votes from D-Smart.
#
# iptv-org is fetched live on every checker process (one request, cached in memory):
IPTV_ORG_CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"

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

ALIASES = {
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
    "szctv": "sozcutv",
    "trtcocukhd": "trtcocuk",
    "trtgenc": "trtgenc",
    "htsporhd": "htsportv",
}

# Names/IDs visibly placed in these categories by current Tivibu pages.
TIVIBU = {
    "Ulusal": {
        "trt1", "kanald", "atv", "showtv", "nowtv", "startv", "kanal7",
        "tv8", "beyaztv", "cnbce", "diyanettv", "a2tv", "teve2", "tv85",
        "trt2",
    },
    "Haber": {
        "360", "trthaber", "ntv", "ahaber", "24tv", "cnnturk",
        "haberturktv", "bloomberght", "ulketv", "tvnet", "tgrthaber",
        "akittv", "haberglobal", "tv100", "benguturktv", "ekoturk",
        "gzt", "halktv", "sozcutv",
    },
    "Spor": {
        "aspor", "trtspor", "trtsporyildiz", "fbtv", "htsportv",
    },
    "Film & Dizi": {
        "bbcfirst", "fx", "epicdrama",
    },
    "Çocuk": {
        "trtcocuk", "minikacocuk", "minikago", "trtdiyanetcocuk",
        "spacetoonturkey", "disneyjr", "babytv", "trtgenc",
    },
    "Belgesel": {
        "trtbelgesel", "lovenature", "tarihtv", "habitattv",
    },
    "Yaşam": {
        "tlc", "dmax",
    },
    "Müzik": {
        "trtmuzik", "dreamturk", "powertv", "powerturktv", "number1tv",
    },
}

# TV+ current lineup/category structure. Only channels whose placement is clear
# from TV+'s category pages/current lineup are used as votes.
TVPLUS = {
    "Ulusal": {
        "trt1", "kanald", "startv", "atv", "showtv", "nowtv", "tv8",
        "360", "a2tv", "cnbce", "teve2", "kanal7", "beyaztv", "tv85",
        "trt2", "tv4", "diyanettv",
    },
    "Haber": {
        "tv100", "flashhabertv", "gzt", "turkhabertv", "cnnturk", "ntv",
        "haberturktv", "ahaber", "trthaber", "24tv", "bloomberght",
        "halktv", "tele1", "haberglobal", "ekoturk", "ulketv", "tgrthaber",
        "tvnet", "akittv", "benguturktv", "sozcutv",
    },
    "Spor": {
        "trtspor", "trtsporyildiz", "aspor", "htsportv", "fbtv",
        "sportstv",
    },
    "Film & Dizi": {
        "fx", "epicdrama",
    },
    "Çocuk": {
        "babytv", "disneyjr", "trtcocuk", "minikacocuk", "minikago",
        "spacetoonturkey", "trtdiyanetcocuk", "trtgenc",
    },
    "Belgesel": {
        "trtbelgesel", "lovenature", "tarihtv",
    },
    "Yaşam": {
        "tlc", "dmax", "ciftcitv",
    },
    "Müzik": {
        "number1tv", "powertv", "dreamturk", "powerturktv", "trtmuzik",
    },
    "Yerel": {
        "kadirgatv", "kontv", "kanal23", "kanalv", "kanal26", "kanal33",
        "on6",
    },
}


# Digiturk current AllChannels page exposes explicit ULUSAL/HABER sections.
# Only placements that are actually visible on the current page are included.
# Conflicts are intentional: the voting system resolves them instead of
# pretending every professional platform agrees.
DIGITURK = {
    "Ulusal": {
        "trt1", "kanald", "atv", "showtv", "nowtv", "startv", "tv8",
        "360", "kanal7", "a2tv", "beyaztv", "tv100", "halktv", "teve2",
        "trteba", "gzt",
    },
    "Haber": {
        "haberglobal", "akittv", "benguturktv", "turkhabertv", "cncbe",
        "cnbce", "sozcutv", "trtworld",
    },
}

# Regional identity is needed because iptv-org removed city/subdivision fields
# from channels in 2025, while many Turkish local stations are tagged "general".
LOCAL_REFERENCE = {
    "aksutv", "alanyapostatv", "altastv", "anadolunettv", "arastv",
    "astv", "atvalanya", "brtv", "caytv", "denizpostasitv", "dimtv",
    "edessatv", "ertv", "erzurumwebtv", "estv", "etvkayseri", "etvmanisa",
    "guneydogutv", "haber61tv", "hunattv", "iceltv", "kanal12", "kanal15",
    "kanal23", "kanal26", "kanal3", "kanal32", "kanal33", "kanal58",
    "kanalfirat", "kanalv", "kaytv", "kentturk", "kocaelitv", "kontv",
    "konyaolaytv", "linetv", "mavikaradeniztv", "mercantv", "mturktv",
    "olayturktv", "sunrtv", "tempotv", "tontv", "tv1", "tv264", "tv41",
    "tv52", "tvden", "urfanatiktv", "van65tv", "kadirgatv", "on6",
}

OFFICIAL_CONTENT_REFERENCE = {
    # Used only when the channel is not placed by the professional-platform
    # reference. These are channel-purpose facts from the broadcaster itself.
    "ilketv": "Haber",
    "zaroktv": "Çocuk",
    "tbmmtv": "Haber",
}

INTERNATIONAL_REFERENCE = {
    "adatv", "afroturktv", "alzahratvturkic", "elsharqtv", "eurod",
    "imamhusseintv5", "kanal7avrupa", "kurdistantv", "luystv", "manastv",
    "mctv", "mekameleentv", "persianaturkiye", "sat7turk", "trtarabi",
    "trtavaz", "trtkurdi", "trtturk", "trtworld", "westazerbaijantv",
    "yoltv", "finesttv",
}

IPTV_CATEGORY_MAP = {
    "general": "Ulusal",
    "entertainment": "Ulusal",
    "religious": "Ulusal",
    "education": "Ulusal",
    "culture": "Ulusal",
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
    "sinema": "Film & Dizi",
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
    return ALIASES.get(value, value)


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


def _identity(meta: str) -> str:
    tvg = _attr(meta, "tvg-id")
    if tvg:
        return normalize_identity(tvg)
    return normalize_identity(split_extinf(meta)[1])


def _country(meta: str) -> str:
    base = _tvg_base(meta)
    if "." not in base:
        return ""
    return fold(base.rsplit(".", 1)[-1])


@lru_cache(maxsize=1)
def _iptv_org_index() -> dict[str, list[str]]:
    try:
        req = urllib.request.Request(
            IPTV_ORG_CHANNELS_URL,
            headers={"User-Agent": "Mozilla/5.0 (EmirTV-CategoryBot/1.0)"},
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


def _platform_votes(identity: str) -> list[tuple[str, str]]:
    votes = []
    for platform, mapping in (
        ("Tivibu", TIVIBU),
        ("TV+", TVPLUS),
        ("Digiturk", DIGITURK),
    ):
        for category, ids in mapping.items():
            if identity in ids:
                votes.append((platform, category))
    return votes


def classify(meta: str) -> dict:
    identity = _identity(meta)
    name = split_extinf(meta)[1]
    group = fold(_attr(meta, "group-title")).strip()
    country = _country(meta)

    scores = Counter()
    evidence = []

    # Professional platforms are the strongest evidence.
    for platform, category in _platform_votes(identity):
        scores[category] += 50
        evidence.append(f"{platform}:{category}")

    # Clear regional/international station identity.
    if identity in LOCAL_REFERENCE:
        # A regional station stays Yerel even if its actual programming is
        # news/music/general. Location is the defining category here.
        scores["Yerel"] += 90
        evidence.append("regional-reference:Yerel")

    if identity in INTERNATIONAL_REFERENCE:
        # Same idea for explicitly international/overseas services.
        scores["Uluslararası"] += 90
        evidence.append("international-reference:Uluslararası")

    official_category = OFFICIAL_CONTENT_REFERENCE.get(identity)
    if official_category:
        scores[official_category] += 70
        evidence.append(f"official-content:{official_category}")

    # Live machine-readable channel metadata from iptv-org.
    base = _tvg_base(meta).casefold()
    if base:
        categories = _iptv_org_index().get(base, [])
        for raw in categories:
            mapped = IPTV_CATEGORY_MAP.get(raw)
            if mapped:
                scores[mapped] += 20
                evidence.append(f"iptv-org:{raw}->{mapped}")

    # Original M3U group is useful but deliberately weaker than references.
    if group in GROUP_MAP:
        mapped = GROUP_MAP[group]
        scores[mapped] += 8
        evidence.append(f"source-group:{group}->{mapped}")

    # Strong words in the *channel name* beat broad metadata such as
    # "general" or "entertainment". This prevents cases like "Kanal D Drama"
    # being pushed into Ulusal merely because an upstream database calls it
    # entertainment.
    name_fold = fold(name)
    strong_name_groups = [
        ("Haber", ["haber", "news"]),
        ("Spor", ["spor", "sport", "sports"]),
        ("Film & Dizi", ["drama", "dizi", "film", "movie", "cinema", "sinema"]),
        ("Çocuk", ["cocuk", "kids", "kid", "cartoon"]),
        ("Belgesel", ["belgesel", "documentary"]),
        ("Müzik", ["muzik", "music"]),
    ]
    for category, keywords in strong_name_groups:
        if any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(fold(keyword))}(?![a-z0-9])",
                name_fold,
            )
            for keyword in keywords
        ):
            scores[category] += 40
            evidence.append(f"channel-name:{category}")

    haystack = fold(f"{group} {name}")
    keyword_groups = [
        ("Yerel", ["local", "regional", "yerel", "belediye"]),
        ("Haber", ["news", "haber", "gundem", "gazete", "breaking"]),
        ("Spor", ["sports", "sport", "spor", "futbol", "football", "basketbol"]),
        ("Film & Dizi", ["movie", "film", "cinema", "sinema", "series", "serial", "dizi", "drama"]),
        ("Çocuk", ["kids", "children", "cocuk", "cartoon", "animation", "cizgi"]),
        ("Belgesel", ["documentary", "belgesel", "nature", "doga", "history", "science"]),
        ("Yaşam", ["lifestyle", "yasam", "gezi", "yemek", "travel", "food", "hobi"]),
        ("Müzik", ["music", "muzik", "radyo", "radio"]),
        ("Uluslararası", ["international", "global", "world"]),
    ]
    for category, keywords in keyword_groups:
        if any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(fold(keyword))}(?![a-z0-9])",
                haystack,
            )
            for keyword in keywords
        ):
            scores[category] += 4
            evidence.append(f"keyword:{category}")

    # Country is a weak hint: content type should beat nationality.
    if country and country != "tr":
        scores["Uluslararası"] += 10
        evidence.append(f"country:{country}->Uluslararası")
    elif country == "tr":
        scores["Ulusal"] += 1
        evidence.append("country:tr->Ulusal-fallback")

    if not scores:
        return {
            "category": "Diğer",
            "source": "fallback",
            "identity": identity,
            "votes": {},
            "evidence": [],
        }

    # Deterministic tie-break follows UI category order.
    category = max(
        CATEGORY_ORDER,
        key=lambda c: (scores.get(c, 0), -CATEGORY_ORDER.index(c)),
    )
    top_score = scores[category]

    if any(e.startswith(("Tivibu:", "TV+:")) and e.endswith(":" + category) for e in evidence):
        source = "platform"
    elif any(e.startswith("iptv-org:") and e.endswith("->" + category) for e in evidence):
        source = "iptv-org"
    elif category == "Yerel" and identity in LOCAL_REFERENCE:
        source = "regional-reference"
    elif category == "Uluslararası" and identity in INTERNATIONAL_REFERENCE:
        source = "international-reference"
    elif OFFICIAL_CONTENT_REFERENCE.get(identity) == category:
        source = "official-content"
    elif group in GROUP_MAP and GROUP_MAP[group] == category:
        source = "source-group"
    else:
        source = "fallback"

    return {
        "category": category,
        "source": source,
        "identity": identity,
        "score": top_score,
        "votes": dict(scores),
        "evidence": evidence,
    }
