#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from channel_policy import (
    CATEGORY_INDEX,
    CATEGORY_ORDER,
    build_missing_report,
    channel_key,
    fold,
    normalize_meta,
    select_representatives,
    tvg_id,
)
from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources.txt"
PRIORITY = ROOT / "priority_sources.m3u"
DISCOVERED = ROOT / "discovered.m3u"

VERIFIED_OUTPUT = ROOT / "a.m3u"
RESTRICTED_OUTPUT = ROOT / "r.m3u"
UNKNOWN_OUTPUT = ROOT / "u.m3u"
ALL_OUTPUT = ROOT / "all.m3u"
STATS = ROOT / "stats.json"
VALIDATION = ROOT / "validation.json"
MISSING_OUTPUT = ROOT / "missing_channels.json"

UA = "Mozilla/5.0 (EmirTV-M3U-Bot/3.0)"
PLAYLIST_TIMEOUT = 20
MAX_WORKERS = 24
RETRY_WORKERS = 8
UNKNOWN_RETRY_DELAY = 15

CREDENTIAL_PATH_RE = re.compile(
    r"/(?:iptv|live)/[A-Za-z0-9_-]{6,}/[A-Za-z0-9_-]{6,}/",
    flags=re.I,
)
SENSITIVE_QUERY_KEYS = {
    "token", "auth", "authorization", "password", "passwd", "username",
    "user", "key", "sig", "signature", "jwt", "session", "hdnts", "hdnea",
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
    if query_keys & SENSITIVE_QUERY_KEYS:
        return False

    return True


def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path, p.query, "")
    )


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

    # Hand-picked public candidates are loaded first so their clean metadata
    # wins when the same URL also appears in a larger upstream playlist.
    priority_entries = 0
    if PRIORITY.exists():
        try:
            parsed = parse_playlist(PRIORITY.read_text(encoding="utf-8"))
            priority_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:priority_sources.m3u",
                "status": "ok",
                "entries": priority_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:priority_sources.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

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

    retry_urls = [
        url
        for _, url in candidates
        if validation_results.get(
            canonical(url),
            {"status": "unknown"},
        ).get("status") == "unknown"
    ]
    retry_recovered = 0

    if retry_urls:
        time.sleep(UNKNOWN_RETRY_DELAY)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=RETRY_WORKERS
        ) as ex:
            retry_map = {
                ex.submit(validate_hls, url): url
                for url in retry_urls
            }
            for fut in concurrent.futures.as_completed(retry_map):
                url = retry_map[fut]
                key = canonical(url)
                first = validation_results.get(
                    key,
                    {"status": "unknown", "reason": "missing first result"},
                )

                try:
                    second = fut.result()
                except Exception as e:
                    second = {
                        "status": "unknown",
                        "reason": type(e).__name__,
                    }

                merged = dict(second)
                merged["retry_attempted"] = True
                merged["first_reason"] = first.get("reason")

                if (
                    first.get("status") == "unknown"
                    and merged.get("status") != "unknown"
                ):
                    retry_recovered += 1

                validation_results[key] = merged

    variant_items = []
    report = []

    for meta, url in candidates:
        result = validation_results.get(
            canonical(url),
            {"status": "unknown", "reason": "missing result"},
        )
        status = result.get("status", "unknown")
        if status not in {"verified", "restricted", "unknown", "dead", "drm"}:
            status = "unknown"

        normalized_meta, category, name = normalize_meta(meta)
        key = channel_key(normalized_meta, name)

        item = {
            "meta": normalized_meta,
            "url": url,
            "category": category,
            "name": name,
            "channel_key": key,
            "status": status,
            **result,
        }
        item["status"] = status
        variant_items.append(item)

        report.append({
            "name": name,
            "tvg_id": tvg_id(normalized_meta),
            "channel_key": key,
            "category": category,
            "url": url,
            **result,
            "status": status,
        })

    # Collapse multiple URLs/qualities of the same channel after all variants
    # have been validated. A verified stream always beats a restricted/unknown
    # one; within the same status, higher quality and more stable hosts win.
    selected = select_representatives(variant_items)

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

    for item in selected:
        status = item.get("status", "unknown")
        if status not in buckets:
            status = "unknown"
        buckets[status].append(item)
        if status in category_counts:
            category_counts[status][item["category"]] += 1

    # Ana liste yalnızca gerçek HLS media segmenti doğrulanan, semantik olarak
    # tekilleştirilmiş kanal kayıtlarından oluşur.
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

    coverage = build_missing_report(report, MISSING_OUTPUT)

    counts = {key: len(value) for key, value in buckets.items()}
    raw_status_counts = Counter(
        item.get("status", "unknown") for item in variant_items
    )
    stats = {
        "updated_at_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        "validation_mode": "HLS manifest + variant + real media segments",
        "sources": source_report,
        "raw_entries": len(entries),
        "priority_entries": priority_entries,
        "discovered_entries": discovered_entries,
        "unique_entries": len(candidates),
        "unique_stream_candidates": len(candidates),
        "semantic_channels": len(selected),
        "duplicate_stream_variants_collapsed": max(
            0, len(candidates) - len(selected)
        ),
        "filtered_private_style_entries": filtered_private_style,
        "stream_validation_statuses": dict(raw_status_counts),
        "verified_entries": counts["verified"],
        "restricted_entries": counts["restricted"],
        "unknown_entries": counts["unknown"],
        "unknown_retry_candidates": len(retry_urls),
        "unknown_retry_recovered": retry_recovered,
        "unknown_retry_delay_seconds": UNKNOWN_RETRY_DELAY,
        "unknown_retry_workers": RETRY_WORKERS,
        "dead_entries": counts["dead"],
        "drm_entries": counts["drm"],
        "all_non_dead_non_drm_entries": len(all_candidates),
        "core_channels_total": coverage["core_channels_total"],
        "core_channels_verified": coverage["core_channels_verified"],
        "core_channels_not_verified": coverage["core_channels_not_verified"],
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
