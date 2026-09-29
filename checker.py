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
    SOURCE_PRIORITY,
    build_missing_report,
    canonical_name_decision,
    category_decision,
    channel_key,
    fold,
    normalize_meta,
    select_representatives,
    tvg_id,
)
from category_engine import channel_sort_key, order_decision
from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources.txt"
PRIORITY = ROOT / "priority_sources.m3u"
OFFICIAL = ROOT / "official_discovered.m3u"
TURKUVAZ_OFFICIAL = ROOT / "turkuvaz_discovered.m3u"
BROWSER_OFFICIAL = ROOT / "browser_discovered.m3u"
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


def _clean_option_value(value: str | None):
    if value is None:
        return None
    value = value.strip()
    if not value or "\r" in value or "\n" in value:
        return None
    return value


def parse_playlist(
    text: str,
    source_kind: str = "unknown",
    source_name: str = "",
):
    lines = [x.strip() for x in text.splitlines()]
    out = []
    meta = None
    options = {}

    for line in lines:
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
            value = _clean_option_value(value)

            if not value:
                continue
            if key == "http-user-agent":
                options["user_agent"] = value
            elif key in {"http-referrer", "http-referer"}:
                options["referrer"] = value
            continue

        if line.startswith("#"):
            continue

        if line.startswith(("http://", "https://")):
            meta_value = meta or "#EXTINF:-1,Unknown"
            out.append({
                "meta": meta_value,
                "url": line,
                "user_agent": options.get("user_agent"),
                "referrer": options.get("referrer"),
                "source_kind": source_kind,
                "source_name": source_name,
                "geo_hint": (
                    "geo-blocked" in fold(meta_value)
                    or "geo blocked" in fold(meta_value)
                ),
            })
            meta = None
            options = {}

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
            *channel_sort_key(
                x["meta"],
                x["category"],
                x["name"],
            ),
            canonical(x["url"]),
        ),
    )
    lines = ["#EXTM3U"]
    for item in ordered:
        lines.append(item["meta"])
        if item.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={item["referrer"]}')
        if item.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={item["user_agent"]}')
        lines.append(item["url"])
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
            parsed = parse_playlist(
                PRIORITY.read_text(encoding="utf-8"),
                source_kind="priority",
                source_name="priority_sources.m3u",
            )
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

    official_entries = 0
    if OFFICIAL.exists():
        try:
            parsed = parse_playlist(
                OFFICIAL.read_text(encoding="utf-8"),
                source_kind="official_html",
                source_name="official_discovered.m3u",
            )
            official_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:official_discovered.m3u",
                "status": "ok",
                "entries": official_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:official_discovered.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

    turkuvaz_official_entries = 0
    if TURKUVAZ_OFFICIAL.exists():
        try:
            parsed = parse_playlist(
                TURKUVAZ_OFFICIAL.read_text(encoding="utf-8"),
                source_kind="official_api",
                source_name="turkuvaz_discovered.m3u",
            )
            turkuvaz_official_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:turkuvaz_discovered.m3u",
                "status": "ok",
                "entries": turkuvaz_official_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:turkuvaz_discovered.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

    browser_official_entries = 0
    if BROWSER_OFFICIAL.exists():
        try:
            parsed = parse_playlist(
                BROWSER_OFFICIAL.read_text(encoding="utf-8"),
                source_kind="official_browser",
                source_name="browser_discovered.m3u",
            )
            browser_official_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:browser_discovered.m3u",
                "status": "ok",
                "entries": browser_official_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:browser_discovered.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

    for src in source_urls:
        try:
            source_kind = (
                "iptv_org"
                if "iptv-org.github.io" in src
                else "upstream"
            )
            parsed = parse_playlist(
                fetch_text(src),
                source_kind=source_kind,
                source_name=src,
            )
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
                DISCOVERED.read_text(encoding="utf-8"),
                source_kind="github_discovery",
                source_name="discovered.m3u",
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
    header_aware_entries = 0

    for entry in entries:
        url = entry["url"]
        if not safe_public_candidate(url):
            filtered_private_style += 1
            continue

        key = canonical(url)
        existing = unique.get(key)

        if existing is None:
            unique[key] = dict(entry)
        else:
            existing_score = SOURCE_PRIORITY.get(
                existing.get("source_kind", "unknown"),
                SOURCE_PRIORITY["unknown"],
            )
            incoming_score = SOURCE_PRIORITY.get(
                entry.get("source_kind", "unknown"),
                SOURCE_PRIORITY["unknown"],
            )

            if incoming_score > existing_score:
                merged = dict(entry)
                if not merged.get("user_agent"):
                    merged["user_agent"] = existing.get("user_agent")
                if not merged.get("referrer"):
                    merged["referrer"] = existing.get("referrer")
                merged["geo_hint"] = bool(
                    merged.get("geo_hint") or existing.get("geo_hint")
                )
                unique[key] = merged
                existing = merged
            else:
                if not existing.get("user_agent") and entry.get("user_agent"):
                    existing["user_agent"] = entry["user_agent"]
                if not existing.get("referrer") and entry.get("referrer"):
                    existing["referrer"] = entry["referrer"]
                existing["geo_hint"] = bool(
                    existing.get("geo_hint") or entry.get("geo_hint")
                )

    candidates = list(unique.values())
    header_aware_entries = sum(
        1
        for entry in candidates
        if entry.get("user_agent") or entry.get("referrer")
    )

    validation_results = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as ex:
        future_map = {
            ex.submit(
                validate_hls,
                entry["url"],
                user_agent=entry.get("user_agent"),
                referrer=entry.get("referrer"),
            ): entry
            for entry in candidates
        }
        for fut in concurrent.futures.as_completed(future_map):
            entry = future_map[fut]
            url = entry["url"]
            try:
                validation_results[canonical(url)] = fut.result()
            except Exception as e:
                validation_results[canonical(url)] = {
                    "status": "unknown",
                    "reason": type(e).__name__,
                }

    initial_unknown_entries = [
        entry
        for entry in candidates
        if validation_results.get(
            canonical(entry["url"]),
            {"status": "unknown"},
        ).get("status") == "unknown"
    ]

    verified_channel_keys = {
        channel_key(entry["meta"])
        for entry in candidates
        if (
            validation_results.get(
                canonical(entry["url"]),
                {"status": "unknown"},
            ).get("status") == "verified"
            and channel_key(entry["meta"])
        )
    }

    retry_entries = [
        entry
        for entry in initial_unknown_entries
        if (
            not channel_key(entry["meta"])
            or channel_key(entry["meta"]) not in verified_channel_keys
        )
    ]
    unknown_retry_skipped_verified_sibling = (
        len(initial_unknown_entries) - len(retry_entries)
    )
    retry_recovered = 0

    if retry_entries:
        time.sleep(UNKNOWN_RETRY_DELAY)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=RETRY_WORKERS
        ) as ex:
            retry_map = {
                ex.submit(
                    validate_hls,
                    entry["url"],
                    user_agent=entry.get("user_agent"),
                    referrer=entry.get("referrer"),
                ): entry
                for entry in retry_entries
            }
            for fut in concurrent.futures.as_completed(retry_map):
                entry = retry_map[fut]
                url = entry["url"]
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

    for entry in candidates:
        meta = entry["meta"]
        url = entry["url"]
        result = validation_results.get(
            canonical(url),
            {"status": "unknown", "reason": "missing result"},
        )
        status = result.get("status", "unknown")
        if status not in {"verified", "restricted", "unknown", "dead", "drm"}:
            status = "unknown"

        decision = category_decision(meta)
        naming = canonical_name_decision(meta)
        raw_name = naming.get("original_name") or ""
        geo_hint = bool(entry.get("geo_hint")) or (
            "geo-blocked" in fold(raw_name)
            or "geo blocked" in fold(raw_name)
        )
        normalized_meta, category, name = normalize_meta(meta)
        ordering = order_decision(normalized_meta, category)
        key = channel_key(normalized_meta, name)

        item = {
            "meta": normalized_meta,
            "url": url,
            "user_agent": entry.get("user_agent"),
            "referrer": entry.get("referrer"),
            "source_kind": entry.get("source_kind", "unknown"),
            "source_name": entry.get("source_name", ""),
            "category": category,
            "name": name,
            "channel_key": key,
            "status": status,
            "geo_hint": geo_hint,
            **result,
        }
        item["status"] = status
        variant_items.append(item)

        report.append({
            "name": name,
            "original_name": naming.get("original_name"),
            "name_source": naming.get("source"),
            "geo_hint": geo_hint,
            "tvg_id": tvg_id(normalized_meta),
            "channel_key": key,
            "category": category,
            "category_source": decision.get("source"),
            "category_votes": decision.get("votes", {}),
            "category_evidence": decision.get("evidence", []),
            "order_known": ordering.get("known", False),
            "order_score": ordering.get("score"),
            "order_sources": ordering.get("sources", 0),
            "order_evidence": ordering.get("evidence", []),
            "url": url,
            "stream_source_kind": entry.get("source_kind", "unknown"),
            "stream_source_name": entry.get("source_name", ""),
            "http_user_agent": entry.get("user_agent"),
            "http_referrer": entry.get("referrer"),
            **result,
            "status": status,
        })

    # Collapse multiple URLs/qualities of the same channel after validation.
    # Selection order: status -> official/source trust -> resolution -> bitrate
    # -> segment stability -> CDN quality -> latency.
    selected = select_representatives(variant_items)
    selected_source_counts = Counter(
        item.get("source_kind", "unknown")
        for item in selected
    )
    name_source_counts = Counter(
        row.get("name_source", "unknown")
        for row in report
    )

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
            0 if x.get("order_known") else 1,
            x.get("order_score", 999.0),
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
        "validation_mode": "Header-aware HLS manifest + variant + live-safe real media segment probes",
        "sources": source_report,
        "raw_entries": len(entries),
        "priority_entries": priority_entries,
        "official_entries": official_entries,
        "turkuvaz_official_entries": turkuvaz_official_entries,
        "browser_official_entries": browser_official_entries,
        "discovered_entries": discovered_entries,
        "unique_entries": len(candidates),
        "unique_stream_candidates": len(candidates),
        "semantic_channels": len(selected),
        "duplicate_stream_variants_collapsed": max(
            0, len(candidates) - len(selected)
        ),
        "filtered_private_style_entries": filtered_private_style,
        "header_aware_entries": header_aware_entries,
        "selected_stream_sources": dict(selected_source_counts),
        "channel_name_sources": dict(name_source_counts),
        "stream_validation_statuses": dict(raw_status_counts),
        "verified_entries": counts["verified"],
        "restricted_entries": counts["restricted"],
        "unknown_entries": counts["unknown"],
        "unknown_retry_candidates": len(retry_entries),
        "unknown_retry_skipped_verified_sibling": (
            unknown_retry_skipped_verified_sibling
        ),
        "unknown_retry_recovered": retry_recovered,
        "unknown_retry_delay_seconds": UNKNOWN_RETRY_DELAY,
        "unknown_retry_workers": RETRY_WORKERS,
        "dead_entries": counts["dead"],
        "drm_entries": counts["drm"],
        "all_non_dead_non_drm_entries": len(all_candidates),
        "core_channels_total": coverage["core_channels_total"],
        "core_channels_verified": coverage["core_channels_verified"],
        "core_channels_geo_restricted": coverage.get(
            "core_channels_geo_restricted", 0
        ),
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
