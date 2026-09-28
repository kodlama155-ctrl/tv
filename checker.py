#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources.txt"
DISCOVERED = ROOT / "discovered.m3u"

VERIFIED_OUTPUT = ROOT / "a.m3u"
RESTRICTED_OUTPUT = ROOT / "r.m3u"
UNKNOWN_OUTPUT = ROOT / "u.m3u"
ALL_OUTPUT = ROOT / "all.m3u"
STATS = ROOT / "stats.json"
VALIDATION = ROOT / "validation.json"

UA = "Mozilla/5.0 (EmirTV-M3U-Bot/2.0)"
PLAYLIST_TIMEOUT = 20
MAX_WORKERS = 24

CREDENTIAL_PATH_RE = re.compile(
    r"/(?:live|iptv)/[^/]{3,}/[^/]{6,}/",
    flags=re.I,
)
PRIVATE_QUERY_KEYS = {"username", "password", "passwd"}

CATEGORY_ORDER = [
    "Genel",
    "Haber",
    "Spor",
    "Eğlence",
    "Dizi / Film",
    "Müzik",
    "Çocuk",
    "Belgesel",
    "Yerel",
    "Dini",
    "Eğitim",
    "Diğer",
]
CATEGORY_INDEX = {name: i for i, name in enumerate(CATEGORY_ORDER)}

CATEGORY_KEYWORDS = {
    "Haber": ["news", "haber", "gundem", "gazete", "breaking", "politika"],
    "Spor": [
        "sports", "sport", "spor", "futbol", "football", "soccer",
        "basket", "basketbol", "voleybol", "volleyball",
    ],
    "Dizi / Film": [
        "movie", "movies", "film", "films", "cinema", "sinema",
        "series", "serial", "dizi",
    ],
    "Müzik": ["music", "musical", "muzik", "radio", "radyo"],
    "Çocuk": [
        "kids", "kid", "children", "child", "cocuk", "cartoon",
        "animation", "animasyon", "cizgi",
    ],
    "Belgesel": [
        "documentary", "documentaries", "belgesel", "nature", "doga",
        "history", "tarih", "science", "bilim",
    ],
    "Dini": [
        "religious", "religion", "dini", "islam", "islamic",
        "quran", "kuran", "ilahiyat",
    ],
    "Eğitim": [
        "education", "educational", "egitim", "school", "okul",
        "university", "universite", "ders",
    ],
    "Yerel": ["local", "regional", "region", "yerel", "belediye"],
    "Eğlence": [
        "entertainment", "eglence", "comedy", "komedi",
        "lifestyle", "variety", "show",
    ],
    "Genel": ["general", "genel", "national", "ulusal"],
}

def fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=PLAYLIST_TIMEOUT) as r:
        raw = r.read(8_000_000)
    return raw.decode("utf-8", errors="replace")

def parse_playlist(text: str):
    lines = [x.strip() for x in text.splitlines()]
    out = []
    meta = None
    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF"):
            meta = line
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            out.append((meta or "#EXTINF:-1,Unknown", line))
            meta = None
    return out

def safe_public_candidate(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False

    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if p.username or p.password:
        return False
    if CREDENTIAL_PATH_RE.search(p.path):
        return False

    query_keys = {
        key.lower()
        for key, _ in urllib.parse.parse_qsl(
            p.query,
            keep_blank_values=True,
        )
    }
    if query_keys & PRIVATE_QUERY_KEYS:
        return False

    return True

def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path, p.query, "")
    )

def fold(text: str) -> str:
    text = text.replace("ı", "i").replace("İ", "I")
    text = unicodedata.normalize("NFKD", text)
    return "".join(
        ch for ch in text if not unicodedata.combining(ch)
    ).lower()

def channel_name(meta: str) -> str:
    if "," not in meta:
        return "Unknown"
    return meta.split(",", 1)[1].strip() or "Unknown"

def existing_group(meta: str) -> str:
    m = re.search(r'group-title="([^"]*)"', meta, flags=re.I)
    return m.group(1).strip() if m else ""

def category_for(meta: str) -> str:
    group = existing_group(meta)
    name = channel_name(meta)
    haystack = fold(f"{group} {name}")

    for category in CATEGORY_ORDER:
        if category == "Diğer":
            continue
        if fold(group).strip() == fold(category).strip():
            return category

    for category in [
        "Haber", "Spor", "Dizi / Film", "Müzik", "Çocuk",
        "Belgesel", "Dini", "Eğitim", "Yerel", "Eğlence", "Genel",
    ]:
        for keyword in CATEGORY_KEYWORDS[category]:
            if re.search(
                rf"(?<![a-z0-9]){re.escape(fold(keyword))}(?![a-z0-9])",
                haystack,
            ):
                return category

    return "Diğer"

def normalize_meta(meta: str):
    category = category_for(meta)

    if "," in meta:
        head, label = meta.split(",", 1)
    else:
        head, label = meta, "Unknown"

    if re.search(r'group-title="[^"]*"', head, flags=re.I):
        head = re.sub(
            r'group-title="[^"]*"',
            f'group-title="{category}"',
            head,
            count=1,
            flags=re.I,
        )
    else:
        head = head.rstrip() + f' group-title="{category}"'

    return f"{head},{label.strip()}", category, label.strip()

def write_playlist(path: Path, entries):
    ordered = sorted(
        entries,
        key=lambda x: (
            CATEGORY_INDEX.get(x["category"], 999),
            fold(x["name"]),
            canonical(x["url"]),
        ),
    )
    lines = ["#EXTM3U"]
    for item in ordered:
        lines.extend([item["meta"], item["url"]])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def main():
    source_urls = [
        x.strip()
        for x in SOURCES.read_text(encoding="utf-8").splitlines()
        if x.strip() and not x.lstrip().startswith("#")
    ]

    entries = []
    source_report = []

    for src in source_urls:
        try:
            parsed = parse_playlist(fetch_text(src))
            entries.extend(parsed)
            source_report.append({
                "url": src,
                "status": "ok",
                "entries": len(parsed),
            })
        except Exception as e:
            source_report.append({
                "url": src,
                "status": "error",
                "error": type(e).__name__,
            })

    discovered_entries = 0
    if DISCOVERED.exists():
        try:
            parsed = parse_playlist(
                DISCOVERED.read_text(encoding="utf-8")
            )
            discovered_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:discovered.m3u",
                "status": "ok",
                "entries": discovered_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:discovered.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

    unique = {}
    filtered_private_style = 0

    for meta, url in entries:
        if not safe_public_candidate(url):
            filtered_private_style += 1
            continue
        unique.setdefault(canonical(url), (meta, url))

    candidates = list(unique.values())

    validation_results = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as ex:
        future_map = {
            ex.submit(validate_hls, url): url
            for _, url in candidates
        }
        for fut in concurrent.futures.as_completed(future_map):
            url = future_map[fut]
            try:
                validation_results[canonical(url)] = fut.result()
            except Exception as e:
                validation_results[canonical(url)] = {
                    "status": "unknown",
                    "reason": type(e).__name__,
                }

    buckets = {
        "verified": [],
        "restricted": [],
        "unknown": [],
        "dead": [],
        "drm": [],
    }
    category_counts = {
        "verified": Counter(),
        "restricted": Counter(),
        "unknown": Counter(),
    }
    report = []

    for meta, url in candidates:
        result = validation_results.get(
            canonical(url),
            {"status": "unknown", "reason": "missing result"},
        )
        status = result.get("status", "unknown")
        if status not in buckets:
            status = "unknown"

        normalized_meta, category, name = normalize_meta(meta)
        item = {
            "meta": normalized_meta,
            "url": url,
            "category": category,
            "name": name,
        }
        buckets[status].append(item)

        if status in category_counts:
            category_counts[status][category] += 1

        report.append({
            "name": name,
            "category": category,
            "url": url,
            **result,
        })

    # Ana liste yalnızca gerçek HLS media segmenti doğrulanan yayınlardan oluşur.
    write_playlist(VERIFIED_OUTPUT, buckets["verified"])
    write_playlist(RESTRICTED_OUTPUT, buckets["restricted"])
    write_playlist(UNKNOWN_OUTPUT, buckets["unknown"])

    all_candidates = (
        buckets["verified"]
        + buckets["restricted"]
        + buckets["unknown"]
    )
    write_playlist(ALL_OUTPUT, all_candidates)

    report.sort(
        key=lambda x: (
            x.get("status", ""),
            CATEGORY_INDEX.get(x["category"], 999),
            fold(x["name"]),
        )
    )
    VALIDATION.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    counts = {key: len(value) for key, value in buckets.items()}
    stats = {
        "updated_at_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        "validation_mode": "HLS manifest + variant + real media segments",
        "sources": source_report,
        "raw_entries": len(entries),
        "discovered_entries": discovered_entries,
        "unique_entries": len(candidates),
        "filtered_private_style_entries": filtered_private_style,
        "verified_entries": counts["verified"],
        "restricted_entries": counts["restricted"],
        "unknown_entries": counts["unknown"],
        "dead_entries": counts["dead"],
        "drm_entries": counts["drm"],
        "all_non_dead_non_drm_entries": len(all_candidates),
        "categories_verified": {
            category: category_counts["verified"].get(category, 0)
            for category in CATEGORY_ORDER
        },
        "categories_restricted": {
            category: category_counts["restricted"].get(category, 0)
            for category in CATEGORY_ORDER
        },
        "categories_unknown": {
            category: category_counts["unknown"].get(category, 0)
            for category in CATEGORY_ORDER
        },
    }

    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
