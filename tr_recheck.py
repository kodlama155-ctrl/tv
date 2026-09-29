#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import urllib.parse
from collections import Counter
from pathlib import Path

from channel_policy import (
    CATEGORY_INDEX,
    CATEGORY_ORDER,
    build_missing_report,
    channel_name,
    existing_group,
    fold,
)
from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
VERIFIED = ROOT / "a.m3u"
RESTRICTED = ROOT / "r.m3u"
UNKNOWN = ROOT / "u.m3u"
ALL = ROOT / "all.m3u"
VALIDATION = ROOT / "validation.json"
STATS = ROOT / "stats.json"
MISSING = ROOT / "missing_channels.json"

MAX_WORKERS = 8


def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path, p.query, "")
    )


def parse_playlist(path: Path):
    if not path.exists():
        return []

    entries = []
    meta = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            meta = line
            continue
        if line.startswith("#"):
            continue
        if meta and line.startswith(("http://", "https://")):
            entries.append({"meta": meta, "url": line})
            meta = None
    return entries


def category_from_meta(meta: str) -> str:
    return existing_group(meta) or "Diğer"


def write_playlist(path: Path, entries):
    ordered = sorted(
        entries,
        key=lambda item: (
            CATEGORY_INDEX.get(category_from_meta(item["meta"]), 999),
            fold(channel_name(item["meta"])),
            canonical(item["url"]),
        ),
    )
    lines = ["#EXTM3U"]
    for item in ordered:
        lines.extend([item["meta"], item["url"]])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dedupe(entries):
    out = []
    seen = set()
    for item in entries:
        key = canonical(item["url"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def main():
    verified = parse_playlist(VERIFIED)
    restricted = parse_playlist(RESTRICTED)
    unknown = parse_playlist(UNKNOWN)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {
            ex.submit(validate_hls, item["url"]): item
            for item in restricted
        }
        for fut in concurrent.futures.as_completed(future_map):
            item = future_map[fut]
            key = canonical(item["url"])
            try:
                results[key] = fut.result()
            except Exception as exc:
                results[key] = {
                    "status": "unknown",
                    "reason": type(exc).__name__,
                }

    tr_verified = []
    still_restricted = []

    for item in restricted:
        key = canonical(item["url"])
        result = results.get(
            key,
            {"status": "unknown", "reason": "missing TR result"},
        )

        if result.get("status") == "verified":
            tr_verified.append(item)
        else:
            still_restricted.append(item)

    verified = dedupe(verified + tr_verified)
    still_restricted = dedupe(still_restricted)
    unknown = dedupe(unknown)

    write_playlist(VERIFIED, verified)
    write_playlist(RESTRICTED, still_restricted)
    write_playlist(ALL, dedupe(verified + still_restricted + unknown))

    now = dt.datetime.now(dt.timezone.utc).isoformat()

    if VALIDATION.exists():
        report = json.loads(VALIDATION.read_text(encoding="utf-8"))
    else:
        report = []

    by_url = {
        canonical(item.get("url", "")): item
        for item in report
        if item.get("url")
    }

    for item in restricted:
        key = canonical(item["url"])
        result = results.get(
            key,
            {"status": "unknown", "reason": "missing TR result"},
        )
        row = by_url.get(key)
        if row is None:
            continue

        row["tr_recheck_attempted"] = True
        row["tr_recheck_at_utc"] = now
        row["hosted_status"] = row.get("status")
        row["hosted_reason"] = row.get("reason")
        row["tr_status"] = result.get("status", "unknown")
        row["tr_reason"] = result.get("reason")

        if result.get("status") == "verified":
            row.update(result)
            row["status"] = "verified"
            row["verified_from"] = "tr-self-hosted"

    VALIDATION.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    coverage = build_missing_report(report, MISSING)

    if STATS.exists():
        stats = json.loads(STATS.read_text(encoding="utf-8"))
    else:
        stats = {}

    categories_verified = Counter(
        category_from_meta(item["meta"]) for item in verified
    )
    categories_restricted = Counter(
        category_from_meta(item["meta"]) for item in still_restricted
    )
    categories_unknown = Counter(
        category_from_meta(item["meta"]) for item in unknown
    )

    stats["updated_at_utc"] = now
    stats["verified_entries"] = len(verified)
    stats["restricted_entries"] = len(still_restricted)
    stats["unknown_entries"] = len(unknown)
    stats["all_non_dead_non_drm_entries"] = (
        len(verified) + len(still_restricted) + len(unknown)
    )
    stats["tr_recheck_candidates"] = len(restricted)
    stats["tr_verified_entries"] = len(tr_verified)
    stats["tr_still_restricted_entries"] = len(still_restricted)
    stats["tr_recheck_workers"] = MAX_WORKERS
    stats["tr_recheck_at_utc"] = now
    stats["core_channels_total"] = coverage["core_channels_total"]
    stats["core_channels_verified"] = coverage["core_channels_verified"]
    stats["core_channels_not_verified"] = coverage["core_channels_not_verified"]

    stats["categories_verified"] = {
        category: categories_verified.get(category, 0)
        for category in CATEGORY_ORDER
    }
    stats["categories_restricted"] = {
        category: categories_restricted.get(category, 0)
        for category in CATEGORY_ORDER
    }
    stats["categories_unknown"] = {
        category: categories_unknown.get(category, 0)
        for category in CATEGORY_ORDER
    }

    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "tr_recheck_candidates": len(restricted),
        "tr_verified_entries": len(tr_verified),
        "tr_still_restricted_entries": len(still_restricted),
        "core_channels_verified": coverage["core_channels_verified"],
        "core_channels_not_verified": coverage["core_channels_not_verified"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
