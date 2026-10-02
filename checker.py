#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from channel_policy import (
    CATEGORY_INDEX,
    CATEGORY_ORDER,
    CORE_CHANNELS,
    SOURCE_PRIORITY,
    build_missing_report,
    canonical_name_decision,
    category_decision,
    channel_key,
    channel_name,
    country_from_tvg_id,
    existing_group,
    fold,
    normalize_identity,
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
MISSING_DISCOVERED = ROOT / "missing_discovered.m3u"

VERIFIED_OUTPUT = ROOT / "a.m3u"
RESTRICTED_OUTPUT = ROOT / "r.m3u"
UNKNOWN_OUTPUT = ROOT / "u.m3u"
ALL_OUTPUT = ROOT / "all.m3u"
TURKEY_OUTPUT = ROOT / "tr.m3u"
STATS = ROOT / "stats.json"
VALIDATION = ROOT / "validation.json"
MISSING_OUTPUT = ROOT / "missing_channels.json"
DOMAIN_SCORES = ROOT / "domain_scores.json"
REVIEW_CANDIDATES = ROOT / "review_candidates.json"
FALLBACKS = ROOT / "fallbacks.json"

UA = "Mozilla/5.0 (EmirTV-M3U-Bot/3.0)"
PLAYLIST_TIMEOUT = 20
MAX_WORKERS = 24
RETRY_WORKERS = 12
UNKNOWN_RETRY_DELAY = 3

EXPLICIT_CREDENTIAL_PATH_RE = re.compile(
    r"/(?:user(?:name)?|pass(?:word)?|token|auth(?:orization)?|session|jwt|key)(?:=|/)[^/?#]+",
    re.I,
)
SENSITIVE_QUERY_KEYS: set[str] = set()

CORE_RETRY_KEYS = {
    normalize_identity(value)
    for row in CORE_CHANNELS
    for value in [row.get("id", ""), row.get("name", ""), *row.get("aliases", [])]
    if normalize_identity(value)
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
                "device_hint": (
                    "device-fallback" in fold(meta_value)
                    or "device fallback" in fold(meta_value)
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
    if EXPLICIT_CREDENTIAL_PATH_RE.search(p.path):
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


DOMAIN_PRIOR_SCORE = 70.0
DOMAIN_PRIOR_SAMPLES = 5.0


def stream_domain(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def build_domain_scores(items: list[dict]) -> dict[str, dict]:
    grouped: dict[str, Counter] = {}
    for item in items:
        host = stream_domain(item.get("url", ""))
        if not host:
            continue
        grouped.setdefault(host, Counter())[
            item.get("status", "unknown")
        ] += 1

    result = {}
    for host, counts in grouped.items():
        verified = int(counts.get("verified", 0))
        restricted = int(counts.get("restricted", 0))
        unknown = int(counts.get("unknown", 0))
        dead = int(counts.get("dead", 0))
        drm = int(counts.get("drm", 0))
        total = sum(counts.values())

        # Verified is full credit. Restricted/unknown receive partial credit
        # because the runner may be blocked even when a device can play them.
        observed_points = (
            verified
            + restricted * 0.55
            + unknown * 0.25
        )
        prior_points = (
            DOMAIN_PRIOR_SAMPLES * DOMAIN_PRIOR_SCORE / 100.0
        )
        score = 100.0 * (
            observed_points + prior_points
        ) / (total + DOMAIN_PRIOR_SAMPLES)

        result[host] = {
            "score": round(score, 2),
            "samples": total,
            "verified": verified,
            "restricted": restricted,
            "unknown": unknown,
            "dead": dead,
            "drm": drm,
            "verified_rate": round(
                (100.0 * verified / total) if total else 0.0,
                2,
            ),
        }

    return result


def attach_domain_scores(items: list[dict], scores: dict[str, dict]):
    for item in items:
        host = stream_domain(item.get("url", ""))
        info = scores.get(host, {})
        item["domain"] = host
        item["domain_score"] = float(
            info.get("score", DOMAIN_PRIOR_SCORE)
        )
        item["domain_samples"] = int(info.get("samples", 0))

VALIDATION_REPORT_META_KEYS = {
    "name", "original_name", "name_source", "geo_hint", "device_hint",
    "tvg_id", "channel_key", "category", "category_source",
    "category_votes", "category_evidence", "order_known", "order_score",
    "order_sources", "order_evidence", "url", "stream_source_kind",
    "stream_source_name", "http_user_agent", "http_referrer",
}


def prevalidation_candidate_decision(entry: dict) -> tuple[str, str]:
    meta = entry.get("meta", "")
    decision = category_decision(meta)
    category = decision.get("category", "Diğer")

    if category != "Diğer":
        return "accept", "catalog"

    source_kind = entry.get("source_kind", "unknown")
    if source_kind in {"priority", "official_html", "official_api", "official_browser"}:
        return "accept", "trusted-source"

    name = channel_name(meta)
    tvg = tvg_id(meta)
    folded = fold(name)

    if re.search(r"[\u0400-\u04ff]", name):
        return "reject", "non-turkish-script"

    if re.search(
        r"(?:^|\s)(?:test|vpn|backup|yedek)(?:$|\s)",
        folded,
        flags=re.I,
    ):
        return "reject", "test-or-backup"

    if re.search(
        r"\b(?:film|movie|polis|smackdown|fight pass)\b",
        folded,
        flags=re.I,
    ):
        return "reject", "vod-or-event-like"

    host = stream_domain(entry.get("url", ""))
    if host.endswith("prosto.tv") or host.endswith("europlayiptv.de"):
        return "reject", "noisy-provider"

    if re.search(r"\.tr(?:@|$)", tvg, flags=re.I):
        return "review", "uncatalogued-tr-identity"

    return "reject", "uncatalogued"


def validation_cache_key(url: str, user_agent=None, referrer=None) -> str:
    return "\n".join([
        canonical(url),
        user_agent or "",
        referrer or "",
    ])


def load_validation_cache():
    if os.environ.get("CHECKER_REUSE_VALIDATION") != "1":
        return {}
    if not VALIDATION.exists():
        return {}

    try:
        rows = json.loads(VALIDATION.read_text(encoding="utf-8"))
    except Exception:
        return {}

    cache = {}
    for row in rows if isinstance(rows, list) else []:
        url = row.get("url")
        status = row.get("status")
        if not url or status not in {"verified", "restricted", "unknown", "dead", "drm"}:
            continue

        # Validate-only mode may reuse signed URLs too. The scheduled full
        # scan still re-checks every stream and refreshes expired signatures.

        key = validation_cache_key(
            url,
            row.get("http_user_agent"),
            row.get("http_referrer"),
        )
        result = {
            k: v
            for k, v in row.items()
            if k not in VALIDATION_REPORT_META_KEYS
        }
        result["_cached_order_known"] = bool(row.get("order_known"))
        result["_cached_order_score"] = row.get("order_score", 999999999.0)
        result["_cached_order_sources"] = int(row.get("order_sources") or 0)
        result["_cached_order_evidence"] = row.get("order_evidence", [])
        cache[key] = result

    return cache


def is_ephemeral_signed_url(url: str) -> bool:
    try:
        keys = {
            key.lower()
            for key, _ in urllib.parse.parse_qsl(
                urllib.parse.urlsplit(url).query,
                keep_blank_values=True,
            )
        }
    except Exception:
        return False
    return "st" in keys and "e" in keys


def is_known_false_identity(entry: dict) -> bool:
    key = channel_key(entry.get("meta", ""))
    url = fold(entry.get("url", ""))
    # Known technically-working but semantically wrong mappings must never
    # win selection.
    if key == "vavtv" and "kltr-sanat-tv" in url:
        return True
    if key == "kanal7" and ("kanal7avr" in url or "kanal7avrupa" in url):
        return True
    return False


TURKEY_DEVICE_GROUPS = {
    "Ulusal", "Haber", "Spor", "Film & Dizi", "Çocuk",
    "Belgesel", "Dini", "Müzik", "Yerel", "Uluslararası",
}

JUNK_NAME_RE = re.compile(
    r"(?:^|[ _:/-])(?:test|vpn|backup|yedek|mac zamani|dusus?k kalite)(?:$|[ _:/-])",
    re.I,
)

NON_CHANNEL_NAME_RE = re.compile(
    r"(?:\b(?:film|movie|polis|smackdown|fight pass)\b|"
    r"^[0-9]+$|"
    r"[\u0400-\u04ff])",
    re.I,
)

PROVIDER_VARIANT_RE = re.compile(
    r"^\s*TR\s*[:|_-]|\b(?:HQ|UHD|FHD|50FPS)\b",
    re.I,
)


def turkey_device_decision(item: dict) -> tuple[bool, str]:
    """Decide whether a selected stream belongs in the curated Turkey device list."""
    category = item.get("category") or ""
    meta = item.get("meta") or ""
    name = item.get("name") or channel_name(meta)
    source_name = item.get("source_name") or ""
    source_kind = item.get("source_kind") or "unknown"
    original_group = item.get("source_group") or existing_group(meta)
    country = country_from_tvg_id(meta)

    # Curated catalog decisions are authoritative.
    if category in TURKEY_DEVICE_GROUPS and category != "Diğer":
        return True, "curated-category"

    folded_name = fold(name)
    if (
        JUNK_NAME_RE.search(name)
        or NON_CHANNEL_NAME_RE.search(name)
        or PROVIDER_VARIANT_RE.search(name)
    ):
        return False, "junk-or-provider-variant"

    # Discovery/provider dumps with no stable channel identity do not enter tr.m3u.
    if not tvg_id(meta):
        return False, "no-stable-tvg-id"

    # A .tr channel identity is a strong signal, but only for an actually
    # verified/restricted live HLS candidate. This recovers legitimate new/local
    # Turkish channels without accepting anonymous provider aliases.
    if country == "tr":
        host = stream_domain(item.get("url", ""))
        if host.endswith("europlayiptv.de") or host.endswith("prosto.tv"):
            return False, "noisy-provider"

        trusted_source = source_kind in {
            "priority", "official_html", "official_api",
            "official_browser", "iptv_org",
        }
        categorized_source = original_group in TURKEY_DEVICE_GROUPS

        # For broad GitHub/upstream discovery, .tr alone is not enough because
        # some provider dumps carry incorrect country suffixes.
        if not trusted_source and not categorized_source:
            return False, "weak-tr-identity"

        return True, "strong-tr-identity"

    # If an upstream explicitly supplied a known EmirTV group and the service
    # identity is Turkish-labelled by name/source metadata, allow it cautiously.
    if original_group in TURKEY_DEVICE_GROUPS and (
        "turk" in folded_name
        or "turkiye" in folded_name
        or "tr@" in fold(tvg_id(meta))
    ):
        return True, "strong-turkey-metadata"

    return False, "unreviewed"


def write_playlist(path: Path, entries):
    ordered = sorted(
        entries,
        key=lambda x: (
            CATEGORY_INDEX.get(x["category"], 999),
            0 if x.get("order_known") else 1,
            -int(x.get("order_sources") or 0),
            float(
                x.get("order_score")
                if x.get("order_score") is not None
                else 999999999.0
            ),
            fold(x.get("name", "")),
            normalize_identity(tvg_id(x.get("meta", "")) or x.get("name", "")),
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
    reuse_validation_mode = (
        os.environ.get("CHECKER_REUSE_VALIDATION") == "1"
    )
    source_urls = [] if reuse_validation_mode else [
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

    cached_output_entries = 0
    if reuse_validation_mode and ALL_OUTPUT.exists():
        try:
            parsed = parse_playlist(
                ALL_OUTPUT.read_text(encoding="utf-8"),
                source_kind="cached_output",
                source_name="all.m3u",
            )
            cached_output_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:all.m3u",
                "status": "cached",
                "entries": cached_output_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:all.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

    cached_turkey_entries = 0
    if reuse_validation_mode and TURKEY_OUTPUT.exists():
        try:
            parsed = parse_playlist(
                TURKEY_OUTPUT.read_text(encoding="utf-8"),
                source_kind="cached_output",
                source_name="tr.m3u",
            )
            # Validate-only updates must never drop a channel that is already
            # in the Turkey device playlist. Full scans still decide whether
            # these fallbacks remain valid.
            for row in parsed:
                row["device_hint"] = True
            cached_turkey_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:tr.m3u",
                "status": "cached",
                "entries": cached_turkey_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:tr.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

    official_entries = 0
    if not reuse_validation_mode and OFFICIAL.exists():
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
    if not reuse_validation_mode and TURKUVAZ_OFFICIAL.exists():
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
    if not reuse_validation_mode and BROWSER_OFFICIAL.exists():
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

    targeted_missing_entries = 0
    if not reuse_validation_mode and MISSING_DISCOVERED.exists():
        try:
            parsed = parse_playlist(
                MISSING_DISCOVERED.read_text(encoding="utf-8"),
                source_kind="github_discovery",
                source_name="missing_discovered.m3u",
            )
            targeted_missing_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({
                "url": "local:missing_discovered.m3u",
                "status": "ok",
                "entries": targeted_missing_entries,
            })
        except Exception as e:
            source_report.append({
                "url": "local:missing_discovered.m3u",
                "status": "error",
                "error": type(e).__name__,
            })

    discovered_entries = 0
    if not reuse_validation_mode and DISCOVERED.exists():
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
    filtered_false_identity = 0
    header_aware_entries = 0

    for entry in entries:
        url = entry["url"]
        if is_known_false_identity(entry):
            filtered_false_identity += 1
            continue
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
                merged["device_hint"] = bool(
                    merged.get("device_hint") or existing.get("device_hint")
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
                existing["device_hint"] = bool(
                    existing.get("device_hint") or entry.get("device_hint")
                )

    raw_candidates = list(unique.values())
    candidates = []
    prevalidation_rejected = Counter()
    prevalidation_review = []

    for entry in raw_candidates:
        decision, reason = prevalidation_candidate_decision(entry)
        if decision == "accept":
            candidates.append(entry)
            continue

        if decision == "review":
            prevalidation_review.append({
                "name": channel_name(entry.get("meta", "")),
                "tvg_id": tvg_id(entry.get("meta", "")),
                "url": entry.get("url", ""),
                "source_kind": entry.get("source_kind", ""),
                "source_name": entry.get("source_name", ""),
                "reason": reason,
            })
            continue

        prevalidation_rejected[reason] += 1

    # Merge review candidates produced by discover.py with candidates found in
    # configured upstream lists. Review items are never promoted automatically.
    existing_review = []
    if REVIEW_CANDIDATES.exists():
        try:
            payload = json.loads(REVIEW_CANDIDATES.read_text(encoding="utf-8"))
            existing_review = payload.get("candidates", []) if isinstance(payload, dict) else []
        except Exception:
            existing_review = []

    review_by_url = {
        row.get("url", ""): row
        for row in existing_review + prevalidation_review
        if row.get("url")
    }
    REVIEW_CANDIDATES.write_text(
        json.dumps(
            {
                "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "count": len(review_by_url),
                "candidates": sorted(
                    review_by_url.values(),
                    key=lambda row: (
                        fold(row.get("name", "")),
                        row.get("url", ""),
                    ),
                ),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    header_aware_entries = sum(
        1
        for entry in candidates
        if entry.get("user_agent") or entry.get("referrer")
    )

    validation_cache = load_validation_cache()
    validation_results = {}
    validation_cache_hits = 0
    validation_fresh_entries = []

    for entry in candidates:
        cache_key = validation_cache_key(
            entry["url"],
            entry.get("user_agent"),
            entry.get("referrer"),
        )
        cached = validation_cache.get(cache_key)
        if cached is not None:
            validation_results[canonical(entry["url"])] = dict(cached)
            validation_cache_hits += 1
        else:
            validation_fresh_entries.append(entry)

    fresh_validation_keys = {
        canonical(entry["url"])
        for entry in validation_fresh_entries
    }

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
            for entry in validation_fresh_entries
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
            canonical(entry["url"]) in fresh_validation_keys
            and channel_key(entry["meta"]) in CORE_RETRY_KEYS
            and channel_key(entry["meta"]) not in verified_channel_keys
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
        result = dict(validation_results.get(
            canonical(url),
            {"status": "unknown", "reason": "missing result"},
        ))
        cached_order_known = bool(result.pop("_cached_order_known", False))
        cached_order_score = result.pop("_cached_order_score", 999999999.0)
        cached_order_sources = int(result.pop("_cached_order_sources", 0) or 0)
        cached_order_evidence = result.pop("_cached_order_evidence", [])
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
        device_hint = bool(entry.get("device_hint")) or (
            "device-fallback" in fold(raw_name)
            or "device fallback" in fold(raw_name)
        )
        normalized_meta, category, name = normalize_meta(meta)
        if reuse_validation_mode:
            ordering = {
                "known": cached_order_known,
                "score": cached_order_score,
                "sources": cached_order_sources,
                "evidence": cached_order_evidence,
            }
        else:
            ordering = order_decision(normalized_meta, category)
        key = channel_key(normalized_meta, name)

        item = {
            "meta": normalized_meta,
            "source_meta": meta,
            "source_group": existing_group(meta),
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
            "device_hint": device_hint,
            "order_known": ordering.get("known", False),
            "order_score": ordering.get("score", 999999999.0),
            "order_sources": ordering.get("sources", 0),
            "order_evidence": ordering.get("evidence", []),
            **result,
        }
        item["status"] = status
        variant_items.append(item)

        report.append({
            "name": name,
            "original_name": naming.get("original_name"),
            "name_source": naming.get("source"),
            "geo_hint": geo_hint,
            "device_hint": device_hint,
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

    # Build a reliability reputation for each stream domain from this run.
    # Bayesian smoothing prevents a 1/1 domain from instantly scoring 100.
    domain_scores = build_domain_scores(variant_items)
    attach_domain_scores(variant_items, domain_scores)

    report_by_url = {
        canonical(row.get("url", "")): row
        for row in report
        if row.get("url")
    }
    for item in variant_items:
        row = report_by_url.get(canonical(item.get("url", "")))
        if row is not None:
            row["stream_domain"] = item.get("domain", "")
            row["domain_score"] = item.get("domain_score")
            row["domain_samples"] = item.get("domain_samples", 0)

    DOMAIN_SCORES.write_text(
        json.dumps(
            {
                "updated_at_utc": dt.datetime.now(
                    dt.timezone.utc
                ).isoformat(),
                "method": (
                    "Bayesian-smoothed reliability: verified=1.0, "
                    "restricted=0.55, unknown=0.25, dead/drm=0; "
                    "prior=70 over 5 samples"
                ),
                "domains": dict(
                    sorted(
                        domain_scores.items(),
                        key=lambda pair: (
                            -pair[1]["score"],
                            -pair[1]["samples"],
                            pair[0],
                        ),
                    )
                ),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    # Build one ready-to-use fallback per semantic channel. The fallback
    # is not published as a duplicate row in tr.m3u; it is stored separately
    # for fast health-check failover.
    fallback_map = {}
    variants_by_key = {}
    for item in variant_items:
        key = item.get("channel_key") or ""
        if key:
            variants_by_key.setdefault(key, []).append(item)

    for key, variants in variants_by_key.items():
        usable = [
            row for row in variants
            if row.get("status") in {"verified", "restricted"}
        ]
        if len(usable) < 2:
            continue

        ranked = sorted(
            usable,
            key=representative_score,
            reverse=True,
        )
        primary = ranked[0]
        backup = next(
            (
                row for row in ranked[1:]
                if canonical(row.get("url", "")) != canonical(primary.get("url", ""))
            ),
            None,
        )
        if backup is None:
            continue

        fallback_map[key] = {
            "channel_key": key,
            "name": primary.get("name", ""),
            "primary_url": primary.get("url", ""),
            "fallback_url": backup.get("url", ""),
            "fallback_status": backup.get("status", "unknown"),
            "fallback_source_kind": backup.get("source_kind", "unknown"),
            "fallback_source_name": backup.get("source_name", ""),
            "fallback_user_agent": backup.get("user_agent"),
            "fallback_referrer": backup.get("referrer"),
        }

    FALLBACKS.write_text(
        json.dumps(
            {
                "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "count": len(fallback_map),
                "channels": dict(sorted(fallback_map.items())),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    # Collapse multiple URLs/qualities of the same channel after validation.
    # Selection order: status -> source trust -> domain reliability/confidence
    # -> resolution -> bitrate -> segment stability -> CDN quality -> latency.
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

    # Türkiye cihaz listesi: strict verified yayınlara ek olarak
    # geo-restricted ve açıkça işaretlenmiş resmî device-fallback yayınları
    # dahil eder. Süreli st/e URL seçilmişse aynı kanalın tokensiz resmî
    # device-fallback'i cihaz listesinde tercih edilir.
    variants_by_channel = {}
    for item in variant_items:
        variants_by_channel.setdefault(item.get("channel_key", ""), []).append(item)

    turkey_candidates = []
    turkey_ephemeral_overrides = 0
    for item in selected:
        status = item.get("status", "unknown")
        device_variants = [
            row
            for row in variants_by_channel.get(item.get("channel_key", ""), [])
            if (
                row.get("device_hint")
                and row.get("status") in {"verified", "restricted", "unknown"}
            )
        ]
        has_device_fallback = bool(device_variants)

        include = (
            status == "verified"
            or status == "restricted"
            or (
                status in {"restricted", "unknown"}
                and has_device_fallback
            )
        )

        device_ok, device_reason = turkey_device_decision(item)
        item["turkey_device_reason"] = device_reason
        if not device_ok:
            include = False

        if not include:
            continue

        chosen = item

        # If any variant of this channel is explicitly marked as a
        # device-fallback, retain that curated public variant for Turkey
        # devices when the GitHub runner can only classify the channel as
        # restricted/unknown.
        if status in {"restricted", "unknown"} and has_device_fallback:
            chosen = max(
                device_variants,
                key=lambda row: (
                    2 if row.get("status") == "verified"
                    else 1 if row.get("status") == "restricted"
                    else 0,
                    SOURCE_PRIORITY.get(
                        row.get("source_kind", "unknown"),
                        SOURCE_PRIORITY["unknown"],
                    ),
                    1 if row.get("url", "").startswith("https://") else 0,
                ),
            )

        if status == "verified" and is_ephemeral_signed_url(item.get("url", "")):
            fallbacks = [
                row
                for row in variants_by_channel.get(item.get("channel_key", ""), [])
                if (
                    row.get("device_hint")
                    and row.get("status") in {"verified", "restricted"}
                    and not is_ephemeral_signed_url(row.get("url", ""))
                )
            ]
            if fallbacks:
                chosen = max(
                    fallbacks,
                    key=lambda row: (
                        1 if row.get("status") == "verified" else 0,
                        SOURCE_PRIORITY.get(
                            row.get("source_kind", "unknown"),
                            SOURCE_PRIORITY["unknown"],
                        ),
                        1 if row.get("url", "").startswith("https://") else 0,
                    ),
                )
                turkey_ephemeral_overrides += 1

        turkey_candidates.append(chosen)

    write_playlist(TURKEY_OUTPUT, turkey_candidates)

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
        "reuse_validation_mode": reuse_validation_mode,
        "cached_output_entries": cached_output_entries,
        "cached_turkey_entries": cached_turkey_entries,
        "official_entries": official_entries,
        "turkuvaz_official_entries": turkuvaz_official_entries,
        "browser_official_entries": browser_official_entries,
        "discovered_entries": discovered_entries,
        "targeted_missing_entries": targeted_missing_entries,
        "unique_entries": len(candidates),
        "unique_stream_candidates": len(candidates),
        "prevalidation_raw_candidates": len(raw_candidates),
        "prevalidation_rejected": dict(prevalidation_rejected),
        "prevalidation_review_candidates": len(prevalidation_review),
        "semantic_channels": len(selected),
        "fallback_channels": len(fallback_map),
        "turkey_device_channels": len(turkey_candidates),
        "turkey_device_reasons": dict(Counter(
            item.get("turkey_device_reason", "curated-category")
            for item in turkey_candidates
        )),
        "duplicate_stream_variants_collapsed": max(
            0, len(candidates) - len(selected)
        ),
        "filtered_private_style_entries": filtered_private_style,
        "filtered_false_identity_entries": filtered_false_identity,
        "header_aware_entries": header_aware_entries,
        "validation_cache_enabled": bool(validation_cache),
        "validation_cache_hits": validation_cache_hits,
        "validation_fresh_checks": len(validation_fresh_entries),
        "selected_stream_sources": dict(selected_source_counts),
        "domain_score_count": len(domain_scores),
        "top_domain_scores": [
            {
                "domain": host,
                **info,
            }
            for host, info in sorted(
                domain_scores.items(),
                key=lambda pair: (
                    -pair[1]["score"],
                    -pair[1]["samples"],
                    pair[0],
                ),
            )[:20]
        ],
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
        "turkey_device_entries": len(turkey_candidates),
        "turkey_device_geo_fallbacks": sum(
            1 for item in turkey_candidates if item.get("status") == "restricted" or item.get("geo_restricted")
        ),
        "turkey_device_restricted_fallbacks": sum(
            1
            for item in turkey_candidates
            if item.get("device_hint") or item.get("device_fallback")
        ),
        "turkey_device_ephemeral_overrides": turkey_ephemeral_overrides,
        "core_channels_total": coverage["core_channels_total"],
        "core_channels_verified": coverage["core_channels_verified"],
        "core_channels_geo_restricted": coverage.get(
            "core_channels_geo_restricted", 0
        ),
        "core_channels_device_restricted": coverage.get(
            "core_channels_device_restricted", 0
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
