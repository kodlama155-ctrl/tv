#!/usr/bin/env python3
from __future__ import annotations

import html
import re
import unicodedata
import urllib.request
from functools import lru_cache
from html.parser import HTMLParser

UA = "Mozilla/5.0 (EmirTV-NameBot/1.0)"
TIMEOUT = 6

TURKSAT_URLS = [
    "https://www.turksat.com.tr/uydu/yayincilik-hizmetleri/turksat-frekans-listesi?page=0",
    "https://www.turksat.com.tr/uydu/yayincilik-hizmetleri/turksat-frekans-listesi?page=1",
    "https://www.turksat.com.tr/uydu/yayincilik-hizmetleri/turksat-frekans-listesi?page=2",
    "https://www.turksat.com.tr/uydu/yayincilik-hizmetleri/turksat-frekans-listesi?page=3",
]
DIGITURK_URL = "https://digiturk.net.tr/kanallar"

# Stable Türksat display-name seed. The live Türksat pages are still parsed
# when available, but this keeps Türksat-first naming deterministic when the
# site changes markup or blocks automated HTML fetches. Quality suffixes such
# as HD/SD are intentionally omitted from the app-facing channel name.
TURKSAT_CANONICAL_NAMES = {
    "trt1": "TRT 1",
    "atv": "ATV",
    "kanald": "Kanal D",
    "showtv": "Show TV",
    "startv": "Star TV",
    "nowtv": "NOW TV",
    "tv8": "TV8",
    "tv85": "TV8.5",
    "kanal7": "Kanal 7",
    "a2tv": "A2",
    "teve2": "Teve2",
    "beyaztv": "Beyaz TV",
    "trt2": "TRT 2",
    "cnbce": "CNBC-e",
    "trthaber": "TRT Haber",
    "ntv": "NTV",
    "cnnturk": "CNN Türk",
    "haberturktv": "Habertürk",
    "haberglobal": "Haber Global",
    "ahaber": "A Haber",
    "tgrthaber": "TGRT Haber",
    "tv100": "TV100",
    "sozcutv": "Sözcü TV",
    "halktv": "Halk TV",
    "tele1": "TELE1",
    "tvnet": "TVNET",
    "ulketv": "Ülke TV",
    "24tv": "24 TV",
    "bloomberght": "Bloomberg HT",
    "ekoturk": "EKOTÜRK",
    "trtspor": "TRT Spor",
    "trtsporyildiz": "TRT Spor Yıldız",
    "aspor": "A Spor",
    "htsportv": "HT Spor",
    "tjktv": "TJK TV",
    "trtbelgesel": "TRT Belgesel",
    "dmax": "DMAX",
    "tlc": "TLC",
    "trtcocuk": "TRT Çocuk",
    "disneyjunior": "Disney Junior",
    "diyanettv": "Diyanet TV",
    "trtmuzik": "TRT Müzik",
    "dreamturk": "Dream Türk",
    "powerturktv": "PowerTürk TV",
    "powertv": "Power TV",
    "bbcfirst": "BBC First",
    "flashtv": "Flash TV",
    "kanal1": "Kanal 1",
    "showmax": "Show Max",
    "meltemtv": "Meltem TV",
    "kanalb": "Kanal B",
    "fmtv": "FM TV",
}

IDENTITY_ALIASES = {
    "a2": "a2tv",
    "now": "nowtv",
    "a2hd": "a2tv",
    "tv8bucuk": "tv85",
    "tv85hd": "tv85",
    "tv85": "tv85",
    "trt3trtspor": "trtspor",
    "cnnturkhd": "cnnturk",
    "haberturk": "haberturktv",
    "htspor": "htsportv",
    "benguturk": "benguturktv",
    "powerturk": "powerturktv",
    "bbcfirstturkiye": "bbcfirst",
    "ntvturkiye": "ntv",
    "tr24tv": "24tv",
    "sozcutvtr": "sozcutv",
    "flashhabertv": "flashtv",
}

# Exact display spelling/casing we want after Türksat/Digiturk identity matching.
# This is a fallback spelling table, not a source-precedence table.
PRETTY_NAMES = {
    "trt1": "TRT 1",
    "atv": "ATV",
    "kanald": "Kanal D",
    "showtv": "Show TV",
    "startv": "Star TV",
    "nowtv": "NOW TV",
    "tv8": "TV8",
    "tv85": "TV8.5",
    "kanal7": "Kanal 7",
    "a2tv": "A2",
    "teve2": "Teve2",
    "beyaztv": "Beyaz TV",
    "tv4": "TV4",
    "trt2": "TRT 2",
    "cnbce": "CNBC-e",
    "trthaber": "TRT Haber",
    "ntv": "NTV",
    "cnnturk": "CNN Türk",
    "haberturktv": "Habertürk",
    "haberglobal": "Haber Global",
    "ahaber": "A Haber",
    "tgrthaber": "TGRT Haber",
    "tv100": "TV100",
    "sozcutv": "Sözcü TV",
    "halktv": "Halk TV",
    "tele1": "TELE1",
    "tvnet": "TVNET",
    "ulketv": "Ülke TV",
    "24tv": "24 TV",
    "bloomberght": "Bloomberg HT",
    "ekoturk": "EKOTÜRK",
    "benguturktv": "BengüTürk",
    "trtspor": "TRT Spor",
    "trtsporyildiz": "TRT Spor Yıldız",
    "aspor": "A Spor",
    "htsportv": "HT Spor",
    "tjktv": "TJK TV",
    "dmax": "DMAX",
    "tlc": "TLC",
    "trtbelgesel": "TRT Belgesel",
    "trtcocuk": "TRT Çocuk",
    "minikacocuk": "Minika Çocuk",
    "minikago": "Minika GO",
    "babytv": "BabyTV",
    "disneyjr": "Disney Junior",
    "disneyjunior": "Disney Junior",
    "diyanettv": "Diyanet TV",
    "trtmuzik": "TRT Müzik",
    "dreamturk": "Dream Türk",
    "powerturktv": "PowerTürk TV",
    "powertv": "Power TV",
    "number1tv": "Number 1 TV",
    "bbcfirst": "BBC First",
    "kanalddrama": "Kanal D Drama",
    "anews": "A News",
    "apara": "A Para",
    "vavtv": "VAV TV",
    "flashtv": "Flash TV",
    "kanal1": "Kanal 1",
    "showmax": "Show Max",
    "meltemtv": "Meltem TV",
    "kanalb": "Kanal B",
    "fmtv": "FM TV",
}

NOISE_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*")
NOISE_PAREN_RE = re.compile(
    r"\s*\([^)]*(?:\d{3,4}p|turkiye|turkey|geo[- ]?blocked|not\s*24/?7|hd|sd|uhd|fhd|4k|8k)[^)]*\)\s*",
    re.I,
)
TRAILING_QUALITY_RE = re.compile(
    r"(?:\s+|[-_/])(?:HD|SD|FHD|UHD|4K|8K|\d{3,4}P)\s*$",
    re.I,
)


def fold(text: str) -> str:
    text = str(text or "").replace("ı", "i").replace("İ", "I")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def normalize_identity(text: str) -> str:
    value = fold(text)
    value = re.sub(r"@[^@]+$", "", value)
    value = re.sub(r"\.(?:tr|cy|uk|de|fr|az|iq|ir|ca|us|kg)$", "", value)
    value = NOISE_BRACKET_RE.sub("", value)
    value = NOISE_PAREN_RE.sub("", value)
    value = re.sub(r"\b(?:hd|sd|uhd|fhd|4k|8k)\b", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return IDENTITY_ALIASES.get(value, value)


def clean_display_name(name: str) -> str:
    value = html.unescape(str(name or "")).strip()
    value = NOISE_BRACKET_RE.sub(" ", value)
    value = NOISE_PAREN_RE.sub(" ", value)
    value = TRAILING_QUALITY_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip(" -_/")

    key = normalize_identity(value)
    return PRETTY_NAMES.get(key, value or "Unknown")


class _TurksatTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_tr = False
        self.in_td = False
        self.current_cell = []
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.in_tr = True
            self.row = []
        elif tag == "td" and self.in_tr:
            self.in_td = True
            self.current_cell = []

    def handle_data(self, data):
        if self.in_td:
            value = " ".join(data.split())
            if value:
                self.current_cell.append(value)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "td" and self.in_td:
            self.row.append(" ".join(self.current_cell).strip())
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            if self.row:
                self.rows.append(self.row)
            self.in_tr = False


class _DigiturkHeadingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_h2 = False
        self.parts = []
        self.names = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "h2":
            self.in_h2 = True
            self.parts = []

    def handle_data(self, data):
        if self.in_h2:
            value = " ".join(data.split())
            if value:
                self.parts.append(value)

    def handle_endtag(self, tag):
        if tag.lower() == "h2" and self.in_h2:
            value = " ".join(self.parts).strip()
            if value:
                self.names.append(value)
            self.in_h2 = False


def _fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read(4_000_000).decode("utf-8", errors="replace")


@lru_cache(maxsize=1)
def naming_catalogs() -> dict:
    turksat = dict(TURKSAT_CANONICAL_NAMES)
    digiturk = {}

    for url in TURKSAT_URLS:
        try:
            parser = _TurksatTableParser()
            parser.feed(_fetch(url))
            for row in parser.rows:
                if not row:
                    continue
                name = clean_display_name(row[0])
                key = normalize_identity(name)
                if key and key not in {"kanal", "kanaladi"}:
                    turksat.setdefault(key, name)
        except Exception:
            continue

    try:
        parser = _DigiturkHeadingParser()
        parser.feed(_fetch(DIGITURK_URL))
        for raw in parser.names:
            name = clean_display_name(raw)
            key = normalize_identity(name)
            if key:
                digiturk.setdefault(key, name)
    except Exception:
        pass

    return {
        "turksat": turksat,
        "digiturk": digiturk,
    }


def canonical_channel_name(meta_tvg_id: str, source_name: str) -> tuple[str, str]:
    raw_id = str(meta_tvg_id or "").strip().lower().split("@", 1)[0]
    if raw_id in {"trt4k", "trt4k.tr"}:
        return "TRT 4K", "official-fallback"

    candidates = []
    for value in (meta_tvg_id, source_name):
        key = normalize_identity(value)
        if key and key not in candidates:
            candidates.append(key)

    catalogs = naming_catalogs()

    for key in candidates:
        if key in catalogs["turksat"]:
            return clean_display_name(catalogs["turksat"][key]), "turksat"

    for key in candidates:
        if key in catalogs["digiturk"]:
            return clean_display_name(catalogs["digiturk"][key]), "digiturk"

    for key in candidates:
        if key in PRETTY_NAMES:
            return PRETTY_NAMES[key], "official-fallback"

    return clean_display_name(source_name), "cleaned-source"
