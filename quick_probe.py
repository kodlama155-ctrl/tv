#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import time
from collections import Counter
from pathlib import Path

from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
DEFAULT_PLAYLIST = ROOT / "priority_sources.m3u"
DEFAULT_OUTPUT = ROOT / "quick_validation.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def parse_playlist(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    meta = None
    options = {}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            meta = line
            options = {}
            continue
        if line.startswith("#EXTVLCOPT:"):
            payload = line.split(":", 1)[1]
            if "=" not in payload:
                continue
            key, value = payload.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "http-user-agent":
                options["user_agent"] = value
            elif key in {"http-referrer", "http-referer"}:
                options["referrer"] = value
            continue
        if line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")):
            continue

        name = "Unknown"
        if meta and "," in meta:
            name = meta.split(",", 1)[1].strip() or "Unknown"

        out.append({
            "name": name,
            "meta": meta or "#EXTINF:-1,Unknown",
            "url": line,
            "user_agent": options.get("user_agent"),
            "referrer": options.get("referrer"),
        })
        meta = None
        options = {}

    return out


def probe(entry: dict):
    try:
        result = validate_hls(
            entry["url"],
            user_agent=entry.get("user_agent") or UA,
            referrer=entry.get("referrer"),
        )
    except Exception as exc:
        result = {"status": "unknown", "reason": type(exc).__name__}

    row = dict(entry)
    row.update(result)
    row["status"] = result.get("status", "unknown")
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", default=str(DEFAULT_PLAYLIST))
    parser.add_argument("--channel", default="")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    entries = parse_playlist(Path(args.playlist))

    if args.channel.strip():
        pattern = re.compile(re.escape(args.channel.strip()), re.I)
        entries = [
            row for row in entries
            if pattern.search(row["name"]) or pattern.search(row["meta"])
        ]

    if not entries:
        raise SystemExit("No matching candidates")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(args.workers, 16))
    ) as executor:
        rows = list(executor.map(probe, entries))

    unknown = [row for row in rows if row["status"] == "unknown"]
    if unknown:
        time.sleep(2)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(args.workers, 8))
        ) as executor:
            retried = list(executor.map(probe, unknown))
        by_url = {row["url"]: row for row in rows}
        for row in retried:
            row["retry_attempted"] = True
            by_url[row["url"]] = row
        rows = list(by_url.values())

    counts = Counter(row["status"] for row in rows)
    by_channel = {}
    for row in rows:
        by_channel.setdefault(row["name"], []).append(row)

    channel_summary = {}
    for name, variants in sorted(by_channel.items()):
        statuses = Counter(row["status"] for row in variants)
        channel_summary[name] = {
            "candidates": len(variants),
            "verified": statuses.get("verified", 0),
            "restricted": statuses.get("restricted", 0),
            "unknown": statuses.get("unknown", 0),
            "dead": statuses.get("dead", 0),
            "best_status": (
                "verified" if statuses.get("verified")
                else "restricted" if statuses.get("restricted")
                else "unknown" if statuses.get("unknown")
                else "dead"
            ),
        }

    payload = {
        "mode": "quick-priority-probe",
        "channel_filter": args.channel or None,
        "candidate_count": len(rows),
        "status_counts": dict(counts),
        "channels": channel_summary,
        "results": sorted(rows, key=lambda x: (x["name"], x["url"])),
    }

    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
