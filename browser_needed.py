#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from official_sources import OFFICIAL_SOURCES

ROOT = Path(__file__).resolve().parent
OFFICIAL_STATS = ROOT / "official_stats.json"
TURKUVAZ_STATS = ROOT / "turkuvaz_stats.json"


def main():
    if not OFFICIAL_STATS.exists():
        print("true")
        return

    try:
        official = json.loads(OFFICIAL_STATS.read_text(encoding="utf-8"))
    except Exception:
        print("true")
        return

    accepted = set()
    if TURKUVAZ_STATS.exists():
        try:
            turkuvaz = json.loads(
                TURKUVAZ_STATS.read_text(encoding="utf-8")
            )
            accepted = {
                row.get("name")
                for row in turkuvaz.get("sources", [])
                if row.get("status") == "accepted"
            }
        except Exception:
            accepted = set()

    configured = {row["name"] for row in OFFICIAL_SOURCES}
    unresolved = []

    for row in official.get("sources", []):
        name = row.get("name")
        if name not in configured:
            continue
        if row.get("candidates"):
            continue
        if name in accepted:
            continue
        unresolved.append(name)

    print("true" if unresolved else "false")


if __name__ == "__main__":
    main()
