#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from hls_validator import validate_hls
from official_sources import BROWSER_UA, OFFICIAL_SOURCES

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "turkuvaz_discovered.m3u"
STATS = ROOT / "turkuvaz_stats.json"

HTTP_TIMEOUT = 15
MAX_PAGE_BYTES = 3_000_000
LIVE_VIDEO_ID = "00000000-0000-0000-0000-000000000000"

# Current live mappings used by the public Turkuvaz player implementation.
# The remaining channels use VideoSmilUrl from the official player API.
LIVE_HLS_OVERRIDES = {
    "9BBE055A-4CF6-4BC3-A675-D40E89B55B91":
        "https://trkvz.daioncdn.net/aspor/aspor.m3u8?ce=3&app=45f847c4-04e8-419a-a561-2ebf87084765",
    "0C1BC8FF-C3B1-45BE-A95B-F7BB9C8B03ED":
        "https://trkvz.daioncdn.net/a2tv/a2tv.m3u8?ce=3&app=59363a60-be96-4f73-9eff-355d0ff2c758",
    "AAE2E325-4EAE-45B7-B017-26FD7DDB6CE4":
        "https://trkvz.daioncdn.net/minikago/minikago.m3u8?app=web&ce=3",
    "01ED59F2-4067-4945-8204-45F6C6DB4045":
        "https://trkvz.daioncdn.net/minikago_cocuk/minikago_cocuk.m3u8?app=web&ce=3",
}

TARGET_NAMES = {
    "ATV",
    "ATV Avrupa",
    "A Haber",
    "A Spor",
    "A Para",
    "A News",
    "Vav TV",
}

ID_PAIR_RE = re.compile(
    r'data-videoid=["\']([^"\']+)["\'][^>]*'
    r'data-websiteid=["\']([^"\']+)["\']',
    re.I | re.S,
)
ID_PAIR_RE_REVERSED = re.compile(
    r'data-websiteid=["\']([^"\']+)["\'][^>]*'
    r'data-videoid=["\']([^"\']+)["\']',
    re.I | re.S,
)
TMD_PLAYER_RE = re.compile(
    r"""var\s+tmdPlayer\s*=\s*(?P<q>["'])(.*?)(?P=q)""",
    re.I | re.S,
)


def request_text(url: str, referer: str | None = None):
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        raw = response.read(MAX_PAGE_BYTES + 1)
        final_url = response.geturl()

    if len(raw) > MAX_PAGE_BYTES:
        raise ValueError("response too large")

    return raw.decode("utf-8", errors="replace"), final_url


def request_json(url: str, referer: str | None = None):
    text, final_url = request_text(url, referer=referer)
    return json.loads(text), final_url


def extract_ids(text: str):
    normalized = html.unescape(text)

    match = ID_PAIR_RE.search(normalized)
    if match:
        return match.group(1), match.group(2)

    match = ID_PAIR_RE_REVERSED.search(normalized)
    if match:
        return match.group(2), match.group(1)

    tmd = TMD_PLAYER_RE.search(normalized)
    if tmd:
        fragment = html.unescape(tmd.group(2))
        fragment = fragment.replace(chr(92) + '"', '"').replace(chr(92) + "'", "'")
        match = ID_PAIR_RE.search(fragment)
        if match:
            return match.group(1), match.group(2)
        match = ID_PAIR_RE_REVERSED.search(fragment)
        if match:
            return match.group(2), match.group(1)

    return None


def player_video(website_id: str, video_id: str):
    url = (
        "https://videojs.tmgrup.com.tr/getvideo/"
        f"{urllib.parse.quote(website_id, safe='')}/"
        f"{urllib.parse.quote(video_id, safe='')}"
    )
    data, _ = request_json(url)

    if not data.get("success"):
        raise ValueError("player api unsuccessful")

    video = data.get("video") or {}
    hls_url = str(video.get("VideoSmilUrl") or "").strip()
    resolved_video_id = str(video.get("VideoId") or video_id).strip()

    if not hls_url:
        raise ValueError("missing VideoSmilUrl")

    if resolved_video_id == LIVE_VIDEO_ID or video_id == LIVE_VIDEO_ID:
        hls_url = LIVE_HLS_OVERRIDES.get(
            website_id.upper(),
            hls_url,
        )

    return {
        "video_id": resolved_video_id,
        "title": str(video.get("Title") or ""),
        "hls_url": hls_url,
    }


def secure_hls(hls_url: str, referer: str):
    query = urllib.parse.urlencode({"url": hls_url})
    url = (
        "https://securevideotoken.tmgrup.com.tr/webtv/secure?"
        + query
    )
    data, _ = request_json(url, referer=referer)

    if not data.get("Success"):
        raise ValueError("secure token api unsuccessful")

    secure_url = str(data.get("Url") or "").strip()
    if not secure_url.startswith(("http://", "https://")):
        raise ValueError("missing secure HLS URL")

    return secure_url


def resolve_source(source: dict):
    row = {
        "name": source["name"],
        "page": source["page"],
        "status": "ok",
        "video_id": None,
        "website_id": None,
        "candidate": None,
        "validation": None,
    }

    try:
        page_text, final_page = request_text(source["page"])
        row["final_page"] = final_page

        ids = extract_ids(page_text)
        if not ids:
            row["status"] = "ids_not_found"
            return row

        video_id, website_id = ids
        row["video_id"] = video_id
        row["website_id"] = website_id

        video = player_video(website_id, video_id)
        secure_url = secure_hls(
            video["hls_url"],
            referer=final_page,
        )

        row["candidate"] = secure_url
        row["title"] = video["title"]

        validation = validate_hls(
            secure_url,
            user_agent=BROWSER_UA,
            referrer=final_page,
        )
        row["validation"] = validation

        if validation.get("status") in {"dead", "drm"}:
            row["status"] = validation.get("status")
        else:
            row["status"] = "accepted"

    except Exception as exc:
        row["status"] = "error"
        row["error"] = type(exc).__name__

    return row


def main():
    now = dt.datetime.now(dt.timezone.utc)
    targets = [
        source
        for source in OFFICIAL_SOURCES
        if source["name"] in TARGET_NAMES
    ]

    rows = [resolve_source(source) for source in targets]

    by_name = {
        source["name"]: source
        for source in OFFICIAL_SOURCES
    }

    lines = ["#EXTM3U"]
    accepted = 0

    for row in rows:
        if row.get("status") != "accepted":
            continue

        source = by_name[row["name"]]
        validation = row.get("validation") or {}
        reason = str(validation.get("reason") or "").replace("\n", " ")

        lines.append(
            f'# SOURCE: official-player-api:{source["page"]}'
        )
        lines.append(
            f'# PROBE: {validation.get("status", "unknown")} {reason}'.rstrip()
        )
        lines.append(
            f'#EXTINF:-1 group-title="{source["category"]}",{source["name"]}'
        )
        lines.append(
            f'#EXTVLCOPT:http-referrer={row.get("final_page") or source["page"]}'
        )
        lines.append(
            f'#EXTVLCOPT:http-user-agent={BROWSER_UA}'
        )
        lines.append(row["candidate"])
        accepted += 1

    OUTPUT.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    stats = {
        "updated_at_utc": now.isoformat(),
        "targets": len(targets),
        "accepted": accepted,
        "sources": rows,
    }
    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
