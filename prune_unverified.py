#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from channel_policy import channel_key, split_extinf

ROOT = Path(__file__).resolve().parent
PLAYLIST = ROOT / "tr.m3u"
UNKNOWN_TARGETS = ROOT / "unknown_targets.json"
UNKNOWN_REPAIR = ROOT / "unknown_repair_result.json"
REPORT = ROOT / "prune_unverified_result.json"


def parse_playlist(path: Path):
    rows = []
    meta = None
    options = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            meta = line
            options = []
            continue
        if meta and line.startswith("#EXTVLCOPT:"):
            options.append(line)
            continue
        if line.startswith("#"):
            continue
        if meta and line.startswith(("http://", "https://")):
            _, label = split_extinf(meta)
            rows.append({
                "meta": meta,
                "options": list(options),
                "url": line,
                "name": label or "Unknown",
                "channel_key": channel_key(meta, label),
            })
            meta = None
            options = []
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", default=str(PLAYLIST))
    parser.add_argument("--targets", default=str(UNKNOWN_TARGETS))
    parser.add_argument("--repair-report", default=str(UNKNOWN_REPAIR))
    parser.add_argument("--report", default=str(REPORT))
    args = parser.parse_args()

    playlist_path = Path(args.playlist)
    targets_path = Path(args.targets)
    repair_path = Path(args.repair_report)
    report_path = Path(args.report)

    if not playlist_path.exists() or not targets_path.exists():
        payload = {"targets": 0, "removed": 0, "removed_names": []}
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    targets = json.loads(targets_path.read_text(encoding="utf-8"))
    target_keys = {row.get("channel_key") for row in targets if row.get("channel_key")}

    repaired_keys = set()
    if repair_path.exists():
        try:
            repair = json.loads(repair_path.read_text(encoding="utf-8"))
            for row in repair.get("replacements", []):
                if row.get("channel_key"):
                    repaired_keys.add(row["channel_key"])
        except Exception:
            pass

    unresolved = target_keys - repaired_keys
    rows = parse_playlist(playlist_path)
    kept = []
    removed = []

    for row in rows:
        if row["channel_key"] in unresolved:
            removed.append(row)
        else:
            kept.append(row)

    if removed:
        lines = ["#EXTM3U"]
        for row in kept:
            lines.append(row["meta"])
            lines.extend(row["options"])
            lines.append(row["url"])
        playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "targets": len(target_keys),
        "repaired": len(repaired_keys),
        "removed": len(removed),
        "removed_names": [row["name"] for row in removed],
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
