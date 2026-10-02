#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from collections import Counter
from pathlib import Path

from channel_policy import SOURCE_PRIORITY, channel_key, split_extinf
from checker import is_known_false_identity
from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
TR = ROOT / "tr.m3u"
OUT = ROOT / "deep_probe.json"

LOCAL_SOURCES = [
    ("priority_sources.m3u", "priority"),
    ("official_discovered.m3u", "official_html"),
    ("turkuvaz_discovered.m3u", "official_api"),
    ("browser_discovered.m3u", "official_browser"),
    ("repair_discovered.m3u", "github_discovery"),
    ("discovered.m3u", "github_discovery"),
    ("missing_discovered.m3u", "github_discovery"),
    ("a.m3u", "unknown"),
    ("all.m3u", "unknown"),
]

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
VLC_UA = "VLC/3.0.21 LibVLC/3.0.21"

MAX_INITIAL_WORKERS = 24
MAX_DEEP_WORKERS = 8
ROUNDS = 3
ROUND_DELAY = 3


def canonical(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def parse_playlist(path: Path, source_kind="current"):
    if not path.exists():
        return []

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
                "source_kind": source_kind,
                "source_name": path.name,
            })
            meta = None
            options = {}

    return rows


def run_probe(row: dict, ua=None, referrer_marker="keep"):
    user_agent = row.get("user_agent") if ua is None else ua
    if referrer_marker == "keep":
        referrer = row.get("referrer")
    elif referrer_marker == "none":
        referrer = None
    else:
        referrer = referrer_marker

    try:
        result = validate_hls(
            row["url"],
            user_agent=user_agent,
            referrer=referrer,
        )
    except Exception as exc:
        result = {"status": "unknown", "reason": type(exc).__name__}

    return {
        **result,
        "tested_user_agent": user_agent,
        "tested_referrer": referrer,
    }


def initial_probe(row):
    result = run_probe(row)
    return {**row, **result, "status": result.get("status", "unknown")}


def deep_probe_current(row):
    attempts = []

    profiles = [
        ("original", None, "keep"),
        ("chrome", CHROME_UA, "keep"),
        ("vlc", VLC_UA, "keep"),
    ]

    # If no explicit referrer exists, a conservative same-origin referrer
    # can satisfy public CDN hotlink protection without inventing a third-party site.
    if not row.get("referrer"):
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit(row["url"])
            if parsed.scheme and parsed.netloc:
                profiles.append(
                    ("chrome_same_origin", CHROME_UA, f"{parsed.scheme}://{parsed.netloc}/")
                )
        except Exception:
            pass

    for round_no in range(1, ROUNDS + 1):
        if round_no > 1:
            time.sleep(ROUND_DELAY)

        for profile_name, ua, ref in profiles:
            result = run_probe(row, ua=ua, referrer_marker=ref)
            attempts.append({
                "round": round_no,
                "profile": profile_name,
                **result,
            })
            if result.get("status") == "verified":
                return {
                    "verified": True,
                    "final_status": "verified",
                    "attempts": attempts,
                    "verified_profile": profile_name,
                    "verified_round": round_no,
                }

    statuses = Counter(x.get("status", "unknown") for x in attempts)
    if statuses.get("restricted") == len(attempts):
        final = "restricted"
    elif statuses.get("dead") == len(attempts):
        final = "dead"
    elif statuses.get("restricted", 0) > statuses.get("unknown", 0):
        final = "restricted"
    else:
        final = "unknown"

    return {
        "verified": False,
        "final_status": final,
        "attempts": attempts,
    }


def collect_alternatives(targets):
    wanted = {row["channel_key"] for row in targets}
    current_urls = {
        (row["channel_key"], canonical(row["url"]))
        for row in targets
    }
    unique = {}

    for filename, kind in LOCAL_SOURCES:
        for row in parse_playlist(ROOT / filename, kind):
            if is_known_false_identity(row):
                continue
            if row["channel_key"] not in wanted:
                continue
            if (row["channel_key"], canonical(row["url"])) in current_urls:
                continue

            key = (
                row["channel_key"],
                canonical(row["url"]),
                row.get("user_agent") or "",
                row.get("referrer") or "",
            )
            prev = unique.get(key)
            if prev is None or SOURCE_PRIORITY.get(kind, 0) > SOURCE_PRIORITY.get(prev["source_kind"], 0):
                unique[key] = row

    by_channel = {}
    for row in unique.values():
        by_channel.setdefault(row["channel_key"], []).append(row)

    for key, rows in by_channel.items():
        rows.sort(
            key=lambda r: SOURCE_PRIORITY.get(r["source_kind"], 0),
            reverse=True,
        )
        by_channel[key] = rows[:6]

    return by_channel


def probe_alternative(row):
    result = run_probe(row)
    return {**row, **result, "status": result.get("status", "unknown")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", default=str(TR))
    parser.add_argument("--targets", default="")
    parser.add_argument("--output", default=str(OUT))
    args = parser.parse_args()

    rows = parse_playlist(Path(args.playlist), "current")

    if args.targets:
        target_path = Path(args.targets)
        targets = json.loads(target_path.read_text(encoding="utf-8"))
        wanted = {
            (row.get("channel_key", ""), canonical(row.get("old_url", "")))
            for row in targets
            if row.get("channel_key") and row.get("old_url")
        }
        rows = [
            row for row in rows
            if (row["channel_key"], canonical(row["url"])) in wanted
        ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_INITIAL_WORKERS) as ex:
        initial = list(ex.map(initial_probe, rows))

    ambiguous = [
        row for row in initial
        if row["status"] != "verified"
    ]

    deep_by_url = {}
    if ambiguous:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_DEEP_WORKERS, len(ambiguous))
        ) as ex:
            futures = {ex.submit(deep_probe_current, row): row for row in ambiguous}
            for fut in concurrent.futures.as_completed(futures):
                row = futures[fut]
                deep_by_url[row["url"]] = fut.result()

    alternatives = collect_alternatives(ambiguous)
    alternative_results = {}

    all_alt_rows = [r for rows2 in alternatives.values() for r in rows2]
    if all_alt_rows:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DEEP_WORKERS) as ex:
            probed = list(ex.map(probe_alternative, all_alt_rows))
        for row in probed:
            alternative_results.setdefault(row["channel_key"], []).append(row)

    results = []
    current_verified_after_deep = 0
    verified_alternative_count = 0

    for row in initial:
        if row["status"] == "verified":
            current_verified_after_deep += 1
            results.append({
                "name": row["name"],
                "channel_key": row["channel_key"],
                "url": row["url"],
                "initial_status": "verified",
                "final_current_status": "verified",
                "current_link_verified": True,
                "alternative_verified": False,
                "best_alternative": None,
            })
            continue

        deep = deep_by_url.get(row["url"], {})
        final_status = deep.get("final_status", row["status"])
        current_ok = bool(deep.get("verified"))

        if current_ok:
            current_verified_after_deep += 1

        alts = alternative_results.get(row["channel_key"], [])
        verified_alts = [x for x in alts if x.get("status") == "verified"]
        verified_alts.sort(
            key=lambda x: (
                SOURCE_PRIORITY.get(x.get("source_kind", "unknown"), 0),
                int(x.get("height") or 0),
                int(x.get("bandwidth") or 0),
                -int(x.get("manifest_latency_ms") or 999999),
            ),
            reverse=True,
        )
        best_alt = verified_alts[0] if verified_alts else None
        if best_alt:
            verified_alternative_count += 1

        results.append({
            "name": row["name"],
            "channel_key": row["channel_key"],
            "url": row["url"],
            "initial_status": row["status"],
            "initial_reason": row.get("reason"),
            "final_current_status": final_status,
            "current_link_verified": current_ok,
            "current_attempts": deep.get("attempts", []),
            "alternative_candidates_tested": len(alts),
            "alternative_verified": bool(best_alt),
            "best_alternative": ({
                "url": best_alt["url"],
                "source_kind": best_alt.get("source_kind"),
                "source_name": best_alt.get("source_name"),
                "resolution": best_alt.get("resolution"),
                "bandwidth": best_alt.get("bandwidth"),
                "manifest_latency_ms": best_alt.get("manifest_latency_ms"),
                "segment_latency_ms": best_alt.get("segment_latency_ms"),
            } if best_alt else None),
        })

    final_counts = Counter(r["final_current_status"] for r in results)

    payload = {
        "mode": "deep-current-link-verification",
        "channels_checked": len(rows),
        "initial_verified": sum(1 for r in initial if r["status"] == "verified"),
        "initial_ambiguous": len(ambiguous),
        "current_links_verified_after_deep": current_verified_after_deep,
        "current_links_still_unverified": len(rows) - current_verified_after_deep,
        "verified_alternative_available_for_unverified": verified_alternative_count,
        "final_current_status_counts": dict(final_counts),
        "results": results,
    }

    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        k: payload[k]
        for k in (
            "channels_checked",
            "initial_verified",
            "initial_ambiguous",
            "current_links_verified_after_deep",
            "current_links_still_unverified",
            "verified_alternative_available_for_unverified",
            "final_current_status_counts",
        )
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
