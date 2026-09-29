#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from official_sources import OFFICIAL_SOURCES

ROOT = Path(__file__).resolve().parent
OFFICIAL_STATS = ROOT / "official_stats.json"
TURKUVAZ_STATS = ROOT / "turkuvaz_stats.json"
BROWSER_STATS = ROOT / "browser_stats.json"
BROWSER_CACHE_HOURS = 12


def main():
    if not OFFICIAL_STATS.exists():
        print("run")
        return

    try:
        official = json.loads(OFFICIAL_STATS.read_text(encoding="utf-8"))
    except Exception:
        print("run")
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

    if not unresolved:
        print("clear")
        return

    if BROWSER_STATS.exists():
        try:
            cached = json.loads(
                BROWSER_STATS.read_text(encoding="utf-8")
            )
            cached_names = {
                row.get("name")
                for row in cached.get("sources", [])
                if row.get("name")
            }
            updated_raw = str(cached.get("updated_at_utc") or "")
            updated = dt.datetime.fromisoformat(
                updated_raw.replace("Z", "+00:00")
            )
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=dt.timezone.utc)

            age = dt.datetime.now(dt.timezone.utc) - updated
            if (
                cached_names == set(unresolved)
                and age <= dt.timedelta(hours=BROWSER_CACHE_HOURS)
            ):
                print("reuse")
                return
        except Exception:
            pass

    print("run")


if __name__ == "__main__":
    main()
