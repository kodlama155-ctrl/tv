#!/usr/bin/env python3
# Manual run marker: health scan trigger; no behavior change.
from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from collections import Counter
from pathlib import Path

from channel_policy import channel_key, split_extinf
from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
DEFAULT_PLAYLIST = ROOT / "tr.m3u"
DEFAULT_REPORT = ROOT / "health_status.json"
DEFAULT_TARGETS = ROOT / "repair_targets.json"
DEFAULT_DEEP_TARGETS = ROOT / "deep_targets.json"
DEFAULT_FALLBACKS = ROOT / "fallbacks.json"
DEFAULT_FALLBACK_REPAIRS = ROOT / "fallback_repairs.json"

MAX_WORKERS = 24
RETRY_WORKERS = 8
DEAD_RETRY_DELAY_SECONDS = 4


def parse_playlist(path: Path):
    rows = []
    meta = None
    options = {}

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTINF"):
            meta = line
            options = {}
            continue

        if line.startswith("#EXTVLCOPT:") and meta:
            payload = line.split(":", 1)[1]
            if "=" in payload:
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

        if meta and line.startswith(("http://", "https://")):
            _, label = split_extinf(meta)
            rows.append({
                "name": label or "Unknown",
                "meta": meta,
                "url": line,
                "user_agent": options.get("user_agent"),
                "referrer": options.get("referrer"),
                "channel_key": channel_key(meta, label),
            })
            meta = None
            options = {}

    return rows


def probe(row: dict):
    try:
        result = validate_hls(
            row["url"],
            user_agent=row.get("user_agent"),
            referrer=row.get("referrer"),
        )
    except Exception as exc:
        result = {
            "status": "unknown",
            "reason": type(exc).__name__,
        }

    out = dict(row)
    out.update(result)
    out["status"] = result.get("status", "unknown")
    return out


def probe_many(rows, workers):
    if not rows:
        return []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(rows)))
    ) as executor:
        return list(executor.map(probe, rows))


def load_fallbacks(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    channels = payload.get("channels", {}) if isinstance(payload, dict) else {}
    return channels if isinstance(channels, dict) else {}


def try_ready_fallback(target: dict, fallback: dict) -> dict | None:
    url = fallback.get("fallback_url")
    if not url:
        return None
    try:
        result = validate_hls(
            url,
            user_agent=fallback.get("fallback_user_agent"),
            referrer=fallback.get("fallback_referrer"),
        )
    except Exception as exc:
        result = {
            "status": "unknown",
            "reason": type(exc).__name__,
        }
    if result.get("status") != "verified":
        return None

    return {
        "name": target.get("name"),
        "channel_key": target.get("channel_key"),
        "old_url": target.get("old_url"),
        "new_url": url,
        "user_agent": fallback.get("fallback_user_agent"),
        "referrer": fallback.get("fallback_referrer"),
        "source_kind": fallback.get("fallback_source_kind"),
        "source_name": fallback.get("fallback_source_name"),
        "reason": "precomputed fallback verified",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", default=str(DEFAULT_PLAYLIST))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS))
    parser.add_argument("--deep-targets", default=str(DEFAULT_DEEP_TARGETS))
    parser.add_argument("--fallbacks", default=str(DEFAULT_FALLBACKS))
    parser.add_argument("--fallback-repairs", default=str(DEFAULT_FALLBACK_REPAIRS))
    args = parser.parse_args()

    rows = parse_playlist(Path(args.playlist))
    initial = probe_many(rows, MAX_WORKERS)

    initial_dead = [row for row in initial if row["status"] == "dead"]
    initial_unknown = [row for row in initial if row["status"] == "unknown"]
    retry_by_url = {}

    if initial_dead:
        time.sleep(DEAD_RETRY_DELAY_SECONDS)
        retried = probe_many(initial_dead, RETRY_WORKERS)
        retry_by_url.update({row["url"]: row for row in retried})

    if initial_unknown:
        time.sleep(1)
        # Calm, paced retry (workers=4) for servers that rate-limit parallel bursts
        unknown_retried = probe_many(initial_unknown, workers=4)
        for row in unknown_retried:
            if row.get("status") == "verified":
                retry_by_url[row["url"]] = row

    final_rows = []
    targets = []
    deep_targets = []

    for row in initial:
        retry = retry_by_url.get(row["url"])
        confirmed_dead = bool(
            row["status"] == "dead"
            and retry is not None
            and retry.get("status") == "dead"
        )

        final = dict(row)
        final["initial_status"] = row["status"]
        final["dead_retried"] = retry is not None
        final["confirmed_dead"] = confirmed_dead

        if retry is not None:
            final["retry_status"] = retry.get("status")
            final["retry_reason"] = retry.get("reason")
            if not confirmed_dead:
                final["status"] = retry.get("status", row["status"])
                final["reason"] = retry.get("reason", row.get("reason"))

        final_rows.append(final)

        should_deep_probe = (
            final["status"] == "unknown"
            or (
                final["status"] == "restricted"
                and final.get("restriction_kind") == "forbidden"
            )
        )

        if should_deep_probe and not confirmed_dead:
            deep_targets.append({
                "name": row["name"],
                "channel_key": row["channel_key"],
                "meta": row["meta"],
                "old_url": row["url"],
                "user_agent": row.get("user_agent"),
                "referrer": row.get("referrer"),
                "health_status": final["status"],
                "health_reason": final.get("reason"),
                "restriction_kind": final.get("restriction_kind"),
            })

        if confirmed_dead:
            targets.append({
                "name": row["name"],
                "channel_key": row["channel_key"],
                "meta": row["meta"],
                "old_url": row["url"],
                "user_agent": row.get("user_agent"),
                "referrer": row.get("referrer"),
                "initial_reason": row.get("reason"),
                "retry_reason": retry.get("reason") if retry else None,
            })

    fallbacks = load_fallbacks(Path(args.fallbacks))
    fallback_repairs = []
    remaining_targets = []

    for target in targets:
        fallback = fallbacks.get(target.get("channel_key", ""))
        repair = try_ready_fallback(target, fallback or {})
        if repair is not None:
            fallback_repairs.append(repair)
        else:
            remaining_targets.append(target)

    targets = remaining_targets

    counts = Counter(row["status"] for row in final_rows)
    restriction_counts = Counter(
        row.get("restriction_kind")
        for row in final_rows
        if row.get("status") == "restricted" and row.get("restriction_kind")
    )
    payload = {
        "mode": "current-playlist-health",
        "playlist": str(Path(args.playlist).name),
        "channels_checked": len(rows),
        "status_counts": dict(counts),
        "restriction_counts": dict(restriction_counts),
        "initial_dead": len(initial_dead),
        "confirmed_dead": len(targets) + len(fallback_repairs),
        "ready_fallback_repairs": len(fallback_repairs),
        "dead_retry_delay_seconds": DEAD_RETRY_DELAY_SECONDS,
        "repair_target_names": [row["name"] for row in targets],
        "fallback_repair_names": [row["name"] for row in fallback_repairs],
        "deep_probe_targets": len(deep_targets),
        "deep_probe_target_names": [row["name"] for row in deep_targets],
        "results": final_rows,
    }

    Path(args.report).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.targets).write_text(
        json.dumps(targets, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.fallback_repairs).write_text(
        json.dumps(fallback_repairs, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.deep_targets).write_text(
        json.dumps(deep_targets, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "channels_checked": len(rows),
        "status_counts": dict(counts),
        "restriction_counts": dict(restriction_counts),
        "initial_dead": len(initial_dead),
        "confirmed_dead": len(targets) + len(fallback_repairs),
        "ready_fallback_repairs": len(fallback_repairs),
        "repair_target_names": [row["name"] for row in targets],
        "deep_probe_targets": len(deep_targets),
        "deep_probe_target_names": [row["name"] for row in deep_targets],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
