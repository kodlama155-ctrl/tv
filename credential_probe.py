#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE_RAW = (
    "https://raw.githubusercontent.com/"
    "omerdenizhan/IPTV-M3U/main/m3u/turkiye.m3u"
)
RESULT = Path("credential_probe_result.json")
UA = "Mozilla/5.0 (EmirTV-Credential-Probe/1.0)"

TARGET_LABEL = "TR:TRT 1 HD"
CRED_RE = re.compile(
    r"^https?://([^/]+)/live/([^/]+)/([^/]+)/([^/?#]+)",
    re.I,
)

def fetch(url: str, max_bytes: int = 16384):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Range": f"bytes=0-{max_bytes-1}",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(max_bytes)
        return {
            "code": getattr(r, "status", 200),
            "content_type": (r.headers.get("Content-Type") or "").lower(),
            "data": data,
            "final_url": r.geturl(),
        }

def source_text():
    req = urllib.request.Request(
        SOURCE_RAW,
        headers={"User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(4_000_000).decode("utf-8", errors="replace")

def find_target(text: str):
    lines = [x.strip() for x in text.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        if TARGET_LABEL not in line:
            continue
        for nxt in lines[i+1:i+5]:
            if nxt.startswith(("http://", "https://")):
                return nxt
    return None

def classify_payload(data: bytes, ctype: str):
    head = data[:4096]
    if b"#EXTM3U" in head:
        return "hls_playlist"
    if any(x in ctype for x in ("mpegurl", "application/vnd.apple.mpegurl")):
        return "hls_playlist"
    if len(data) >= 1024:
        return "media_or_binary"
    if data:
        return "small_response"
    return "empty"

def main():
    result = {
        "source_repo": "omerdenizhan/IPTV-M3U",
        "source_path": "m3u/turkiye.m3u",
        "channel": TARGET_LABEL,
        "credentials_embedded_in_url": False,
        "probe_status": "not_run",
    }

    try:
        text = source_text()
        url = find_target(text)
        if not url:
            result["probe_status"] = "target_not_found"
            RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")
            return

        m = CRED_RE.match(url)
        result["credentials_embedded_in_url"] = bool(m)
        if m:
            result["host"] = m.group(1)
            result["url_shape"] = "/live/<user>/<password>/<channel-id>"
        else:
            result["host"] = urllib.parse.urlsplit(url).hostname
            result["url_shape"] = "other"

        try:
            r = fetch(url)
            result.update({
                "probe_status": "reachable",
                "http_code": r["code"],
                "content_type": r["content_type"],
                "response_kind": classify_payload(
                    r["data"], r["content_type"]
                ),
                "bytes_sampled": len(r["data"]),
                "redirected": (
                    urllib.parse.urlsplit(r["final_url"]).netloc
                    != urllib.parse.urlsplit(url).netloc
                ),
            })
        except urllib.error.HTTPError as e:
            result.update({
                "probe_status": "http_error",
                "http_code": e.code,
                "content_type": (
                    e.headers.get("Content-Type") or ""
                ).lower(),
            })
        except Exception as e:
            result.update({
                "probe_status": "network_error",
                "error_type": type(e).__name__,
            })

    except Exception as e:
        result.update({
            "probe_status": "source_error",
            "error_type": type(e).__name__,
        })

    RESULT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

if __name__ == "__main__":
    main()
