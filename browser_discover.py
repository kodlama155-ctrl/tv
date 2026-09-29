#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

from hls_validator import validate_hls
from official_sources import (
    BROWSER_UA,
    OFFICIAL_SOURCES,
    candidate_matches_source,
    canonical,
    extract_m3u8,
    safe_candidate,
)

ROOT = Path(__file__).resolve().parent
OFFICIAL_STATS = ROOT / "official_stats.json"
OUTPUT = ROOT / "browser_discovered.m3u"
STATS = ROOT / "browser_stats.json"

PAGE_TIMEOUT_MS = 30_000
SETTLE_MS = 10_000
MAX_RESPONSE_TEXT = 2_000_000
MAX_CANDIDATES_PER_SOURCE = 8

BAD_URL_TOKENS = (
    "doubleclick",
    "googlesyndication",
    "googleads",
    "/ads/",
    "advert",
    "preroll",
    "promo",
    "trailer",
    "fragman",
    "archive",
    "arsiv",
    "/vod/",
)

CONSENT_TEXTS = (
    "Tümünü Kabul Et",
    "Kabul Et",
    "Hepsini Kabul Et",
    "Accept All",
    "Accept",
)

PLAY_TEXTS = (
    "Oynat",
    "Play",
    "Canlı Yayın",
)


def _source_map():
    return {source["name"]: source for source in OFFICIAL_SOURCES}


def load_targets():
    if not OFFICIAL_STATS.exists():
        return list(OFFICIAL_SOURCES)

    try:
        data = json.loads(OFFICIAL_STATS.read_text(encoding="utf-8"))
    except Exception:
        return list(OFFICIAL_SOURCES)

    by_name = _source_map()
    targets = []

    for row in data.get("sources", []):
        source = by_name.get(row.get("name"))
        if not source:
            continue

        if row.get("candidates"):
            continue

        targets.append(source)

    return targets


def looks_like_stream_url(url: str) -> bool:
    low = url.lower()
    if ".m3u8" not in low:
        return False
    if any(token in low for token in BAD_URL_TOKENS):
        return False
    return safe_candidate(url)


def source_accepts_url(url: str, source: dict) -> bool:
    if not looks_like_stream_url(url):
        return False
    return candidate_matches_source(url, source)


def dismiss_common_ui(page):
    for text in CONSENT_TEXTS:
        try:
            locator = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if locator.count() > 0:
                locator.first.click(timeout=1200)
                break
        except Exception:
            pass

    for text in PLAY_TEXTS:
        try:
            locator = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if locator.count() > 0:
                locator.first.click(timeout=1200)
                break
        except Exception:
            pass


def scan_source(browser, source: dict):
    context = browser.new_context(
        user_agent=BROWSER_UA,
        locale="tr-TR",
        service_workers="block",
        viewport={"width": 1365, "height": 768},
        java_script_enabled=True,
    )
    page = context.new_page()

    found = {}
    network_events = 0
    response_bodies_scanned = 0

    def remember(url: str, referer: str | None = None, via: str = "network"):
        nonlocal network_events

        if not source_accepts_url(url, source):
            return

        network_events += 1
        key = canonical(url)
        existing = found.get(key)

        item = {
            "url": url,
            "referer": referer or source["page"],
            "user_agent": BROWSER_UA,
            "via": via,
        }

        if existing is None:
            found[key] = item
        elif not existing.get("referer") and item.get("referer"):
            existing["referer"] = item["referer"]

    def on_request(request):
        try:
            referer = request.headers.get("referer")
            remember(request.url, referer=referer, via="request")
        except Exception:
            pass

    def on_response(response):
        nonlocal response_bodies_scanned

        try:
            request = response.request
            referer = request.headers.get("referer")
            remember(response.url, referer=referer, via="response")
        except Exception:
            pass

        try:
            resource_type = response.request.resource_type
            content_type = (response.headers.get("content-type") or "").lower()

            if resource_type not in {"xhr", "fetch"}:
                return

            if not any(
                token in content_type
                for token in (
                    "json",
                    "text",
                    "javascript",
                    "xml",
                    "mpegurl",
                    "octet-stream",
                )
            ):
                return

            body = response.text()
            if not body or len(body) > MAX_RESPONSE_TEXT:
                return

            response_bodies_scanned += 1

            for url in extract_m3u8(body, response.url):
                remember(
                    url,
                    referer=response.request.headers.get("referer") or source["page"],
                    via="xhr-body",
                )
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    result = {
        "name": source["name"],
        "page": source["page"],
        "status": "ok",
        "network_events": 0,
        "response_bodies_scanned": 0,
        "candidates": [],
    }

    try:
        page.goto(
            source["page"],
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        dismiss_common_ui(page)

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        page.wait_for_timeout(SETTLE_MS)

        # Some players only start requesting HLS after an interaction.
        dismiss_common_ui(page)
        page.wait_for_timeout(2500)

    except Exception as exc:
        result["status"] = "page_error"
        result["error"] = type(exc).__name__

    finally:
        result["network_events"] = network_events
        result["response_bodies_scanned"] = response_bodies_scanned
        result["candidates"] = list(found.values())[:MAX_CANDIDATES_PER_SOURCE]
        context.close()

    return result


def probe(source: dict, candidate: dict):
    try:
        validation = validate_hls(
            candidate["url"],
            user_agent=candidate.get("user_agent") or BROWSER_UA,
            referrer=candidate.get("referer") or source["page"],
        )
    except Exception as exc:
        validation = {
            "status": "unknown",
            "reason": type(exc).__name__,
        }

    return {
        "source": source,
        "candidate": candidate,
        "validation": validation,
    }


def main():
    now = dt.datetime.now(dt.timezone.utc)
    targets = load_targets()

    scans = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        try:
            for source in targets:
                scans.append(scan_source(browser, source))
        finally:
            browser.close()

    source_by_name = _source_map()
    probed = []

    for row in scans:
        source = source_by_name.get(row["name"])
        if not source:
            continue

        for candidate in row.get("candidates", []):
            probed.append(probe(source, candidate))

    statuses = Counter()
    accepted = []

    for item in probed:
        validation = item["validation"]
        status = validation.get("status", "unknown")
        statuses[status] += 1

        if status in {"dead", "drm"}:
            continue

        if status == "verified" and validation.get("vod"):
            continue

        accepted.append(item)

    unique = {}
    for item in accepted:
        unique.setdefault(
            canonical(item["candidate"]["url"]),
            item,
        )

    lines = ["#EXTM3U"]

    for item in sorted(
        unique.values(),
        key=lambda row: (
            row["source"]["category"],
            row["source"]["name"],
            row["candidate"]["url"],
        ),
    ):
        source = item["source"]
        candidate = item["candidate"]
        validation = item["validation"]
        reason = str(validation.get("reason") or "").replace("\n", " ")

        lines.append(f'# SOURCE: official-browser:{source["page"]}')
        lines.append(
            f'# DISCOVERY: {candidate.get("via", "network")}'
        )
        lines.append(
            f'# PROBE: {validation.get("status", "unknown")} {reason}'.rstrip()
        )
        lines.append(
            f'#EXTINF:-1 group-title="{source["category"]}",{source["name"]}'
        )
        lines.append(
            f'#EXTVLCOPT:http-referrer={candidate.get("referer") or source["page"]}'
        )
        lines.append(
            f'#EXTVLCOPT:http-user-agent={candidate.get("user_agent") or BROWSER_UA}'
        )
        lines.append(candidate["url"])

    OUTPUT.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    stats = {
        "updated_at_utc": now.isoformat(),
        "fallback_targets": len(targets),
        "pages_scanned": len(scans),
        "pages_with_candidates": sum(
            1 for row in scans if row.get("candidates")
        ),
        "raw_candidates": sum(
            len(row.get("candidates", []))
            for row in scans
        ),
        "accepted_unique_candidates": len(unique),
        "probe_statuses": dict(statuses),
        "sources": scans,
    }

    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
