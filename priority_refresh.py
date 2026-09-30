#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
from pathlib import Path

from channel_policy import (
    channel_key,
    normalize_meta,
    split_extinf,
)
from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
PRIORITY = ROOT / "priority_sources.m3u"
TR = ROOT / "tr.m3u"
REPORT = ROOT / "priority_validation.json"


def parse_playlist_text(text: str):
    rows = []
    meta = None
    options = {}

    for raw in text.splitlines():
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
                "meta": meta,
                "url": line,
                "user_agent": options.get("user_agent"),
                "referrer": options.get("referrer"),
                "channel_key": channel_key(meta, label),
                "device_hint": "device-fallback" in label.lower(),
            })
            meta = None
            options = {}

    return rows


def parse_file(path: Path):
    if not path.exists():
        return []
    return parse_playlist_text(path.read_text(encoding="utf-8"))


def previous_priority_text():
    before = os.environ.get("PRIORITY_BEFORE_SHA", "").strip()
    if not before or set(before) == {"0"}:
        return ""

    try:
        return subprocess.check_output(
            ["git", "show", f"{before}:priority_sources.m3u"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return ""


def signatures(rows):
    out = {}
    for row in rows:
        key = row["channel_key"]
        if not key:
            continue
        out.setdefault(key, set()).add((
            row["url"],
            row.get("user_agent") or "",
            row.get("referrer") or "",
        ))
    return out


def probe(row):
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

    item = dict(row)
    item.update(result)
    item["status"] = result.get("status", "unknown")
    return item


def choose_variant(rows):
    verified = [r for r in rows if r["status"] == "verified"]
    if verified:
        return max(
            verified,
            key=lambda r: (
                int(r.get("height") or 0),
                int(r.get("bandwidth") or 0),
                1 if r["url"].startswith("https://") else 0,
            ),
        )

    fallbacks = [
        r for r in rows
        if r.get("device_hint")
        and r["status"] in {"restricted", "unknown"}
    ]
    if fallbacks:
        return max(
            fallbacks,
            key=lambda r: (
                1 if r["status"] == "restricted" else 0,
                1 if r["url"].startswith("https://") else 0,
            ),
        )

    return None


def normalized_output_row(row):
    meta, category, name = normalize_meta(row["meta"])
    return {
        "meta": meta,
        "url": row["url"],
        "user_agent": row.get("user_agent"),
        "referrer": row.get("referrer"),
        "category": category,
        "name": name,
        "channel_key": channel_key(meta, name),
    }


def write_tr(rows):
    # Preserve the existing Turkey playlist order. Priority-only edits are
    # stream replacements, not a reason to re-fetch platform ordering.
    normalized = []
    for row in rows:
        meta, category, name = normalize_meta(row["meta"])
        normalized.append({
            **row,
            "meta": meta,
            "category": category,
            "name": name,
            "channel_key": channel_key(meta, name),
        })

    lines = ["#EXTM3U"]
    for row in normalized:
        lines.append(row["meta"])
        if row.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={row["referrer"]}')
        if row.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={row["user_agent"]}')
        lines.append(row["url"])

    TR.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    current = parse_file(PRIORITY)
    previous = parse_playlist_text(previous_priority_text())
    current_sig = signatures(current)
    previous_sig = signatures(previous)

    changed_keys = sorted({
        key
        for key in set(current_sig) | set(previous_sig)
        if current_sig.get(key, set()) != previous_sig.get(key, set())
    })

    changed_candidates = [
        row for row in current
        if row["channel_key"] in changed_keys
    ]

    if not changed_keys:
        payload = {
            "mode": "priority-delta",
            "changed_channels": 0,
            "candidates_tested": 0,
            "applied_channels": 0,
            "applied": [],
            "kept_existing_channels": [],
            "results": [],
        }
        REPORT.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if changed_candidates:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(changed_candidates))
        ) as executor:
            results = list(executor.map(probe, changed_candidates))
    else:
        results = []

    results_by_channel = {}
    for row in results:
        results_by_channel.setdefault(row["channel_key"], []).append(row)

    existing = parse_file(TR)
    existing_by_key = {
        row["channel_key"]: row
        for row in existing
        if row["channel_key"]
    }

    applied = []
    unchanged = []

    for key in changed_keys:
        variants = results_by_channel.get(key, [])
        chosen = choose_variant(variants)
        if chosen is None:
            unchanged.append(key)
            continue

        existing_by_key[key] = normalized_output_row(chosen)
        applied.append({
            "channel_key": key,
            "status": chosen["status"],
            "url": chosen["url"],
            "reason": chosen.get("reason"),
        })

    write_tr(list(existing_by_key.values()))

    payload = {
        "mode": "priority-delta",
        "changed_channels": len(changed_keys),
        "candidates_tested": len(results),
        "applied_channels": len(applied),
        "applied": applied,
        "kept_existing_channels": unchanged,
        "results": results,
    }
    REPORT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
