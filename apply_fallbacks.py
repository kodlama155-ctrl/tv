#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from channel_policy import channel_key, split_extinf

ROOT = Path(__file__).resolve().parent
PLAYLIST = ROOT / "tr.m3u"
REPAIRS = ROOT / "fallback_repairs.json"
REPORT = ROOT / "fallback_apply_result.json"


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
        if meta and line.startswith("#EXTVLCOPT:"):
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
                "name": label or "Unknown",
                "channel_key": channel_key(meta, label),
                "url": line,
                "user_agent": options.get("user_agent"),
                "referrer": options.get("referrer"),
            })
            meta = None
            options = {}

    return rows


def write_playlist(path: Path, rows):
    lines = ["#EXTM3U"]
    for row in rows:
        lines.append(row["meta"])
        if row.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={row["referrer"]}')
        if row.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={row["user_agent"]}')
        lines.append(row["url"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    repairs = []
    if REPAIRS.exists():
        repairs = json.loads(REPAIRS.read_text(encoding="utf-8"))

    by_key = {
        row.get("channel_key"): row
        for row in repairs
        if row.get("channel_key") and row.get("new_url")
    }

    if not by_key:
        payload = {"requested": 0, "applied": 0, "replacements": []}
        REPORT.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    rows = parse_playlist(PLAYLIST)
    applied = []

    for row in rows:
        repair = by_key.get(row["channel_key"])
        if not repair:
            continue

        old_url = row["url"]
        row["url"] = repair["new_url"]
        row["user_agent"] = repair.get("user_agent")
        row["referrer"] = repair.get("referrer")
        applied.append({
            "name": row["name"],
            "channel_key": row["channel_key"],
            "old_url": old_url,
            "new_url": row["url"],
            "source_kind": repair.get("source_kind"),
            "source_name": repair.get("source_name"),
        })

    if applied:
        write_playlist(PLAYLIST, rows)

    payload = {
        "requested": len(by_key),
        "applied": len(applied),
        "replacements": applied,
    }
    REPORT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
