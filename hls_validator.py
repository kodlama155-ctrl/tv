#!/usr/bin/env python3
from __future__ import annotations

import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MANIFEST_TIMEOUT = 9
SEGMENT_TIMEOUT = 9
MAX_MANIFEST_BYTES = 800_000
MAX_SEGMENT_BYTES = 128_000

RESTRICTED_CODES = {401, 403, 451}
DEAD_CODES = {404, 410}

def _safe_header_value(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value or "\r" in value or "\n" in value:
        return None
    return value


def _request(
    url: str,
    timeout: int,
    max_bytes: int,
    use_range: bool = False,
    request_headers: dict | None = None,
    opener=None,
):
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Cache-Control": "no-cache",
    }

    request_headers = request_headers or {}
    custom_ua = _safe_header_value(request_headers.get("user_agent"))
    referrer = _safe_header_value(request_headers.get("referrer"))

    if custom_ua:
        headers["User-Agent"] = custom_ua
    if referrer:
        headers["Referer"] = referrer
        try:
            ref = urllib.parse.urlsplit(referrer)
            if ref.scheme and ref.netloc:
                headers["Origin"] = f"{ref.scheme}://{ref.netloc}"
        except Exception:
            pass
    if use_range:
        headers["Range"] = f"bytes=0-{max_bytes - 1}"

    req = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    client = opener.open if opener is not None else urllib.request.urlopen
    with client(req, timeout=timeout) as r:
        status = getattr(r, "status", 200)
        data = r.read(max_bytes)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return status, data, r.geturl(), (r.headers.get("Content-Type") or "").lower(), elapsed_ms

def _failure_for_http(code: int) -> str:
    if code in RESTRICTED_CODES:
        return "restricted"
    if code in DEAD_CODES:
        return "dead"
    return "unknown"

def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").lstrip("\ufeff")

def _attrs(raw: str) -> dict[str, str]:
    parts, current, quoted = [], "", False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
            current += ch
        elif ch == "," and not quoted:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current:
        parts.append(current)

    out = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key.strip().upper()] = value.strip().strip('"')
    return out

def _variants(text: str, base_url: str):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    found = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue

        attrs = _attrs(line.split(":", 1)[1])
        uri = None
        for nxt in lines[i + 1:]:
            if not nxt.startswith("#"):
                uri = nxt
                break
        if not uri:
            continue

        bandwidth = 0
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            pass

        width = height = 0
        resolution = attrs.get("RESOLUTION")
        if resolution:
            m = re.match(r"(\d+)x(\d+)", resolution)
            if m:
                width, height = int(m.group(1)), int(m.group(2))

        found.append({
            "url": urllib.parse.urljoin(base_url, uri),
            "bandwidth": bandwidth,
            "width": width,
            "height": height,
            "resolution": resolution,
        })

    found.sort(key=lambda x: (x["height"], x["bandwidth"]), reverse=True)
    return found

def _media_details(text: str, base_url: str):
    segments = []
    init_url = None
    key_url = None
    encrypted = False
    drm = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-MAP:"):
            attrs = _attrs(line.split(":", 1)[1])
            if attrs.get("URI"):
                init_url = urllib.parse.urljoin(base_url, attrs["URI"])

        elif line.startswith("#EXT-X-KEY:"):
            attrs = _attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            keyformat = attrs.get("KEYFORMAT", "identity").lower()
            uri = attrs.get("URI")

            if method not in ("NONE", ""):
                encrypted = True
            if method.startswith("SAMPLE-AES") or keyformat not in ("identity", ""):
                drm = True
            elif method == "AES-128" and uri:
                key_url = urllib.parse.urljoin(base_url, uri)

        elif not line.startswith("#"):
            segments.append(urllib.parse.urljoin(base_url, line))

    return {
        "segments": segments,
        "init_url": init_url,
        "key_url": key_url,
        "encrypted": encrypted,
        "drm": drm,
        "vod": "#EXT-X-ENDLIST" in text,
    }

def _aux_reachable(
    url: str,
    request_headers: dict | None = None,
    opener=None,
):
    try:
        code, data, _, _, _ = _request(
            url,
            SEGMENT_TIMEOUT,
            32_000,
            use_range=True,
            request_headers=request_headers,
            opener=opener,
        )
        return (200 <= code < 400 and len(data) > 0), None
    except urllib.error.HTTPError as e:
        return False, _failure_for_http(e.code)
    except Exception:
        return False, "unknown"

def _payload_looks_media(url: str, data: bytes, ctype: str, encrypted: bool) -> bool:
    if len(data) < 64:
        return False
    if encrypted:
        return True

    path = urllib.parse.urlsplit(url).path.lower()

    if path.endswith((".ts", ".mts")) or "mp2t" in ctype:
        if data[0] == 0x47 and (len(data) < 377 or data[188] == 0x47 or data[376] == 0x47):
            return True
        limit = min(512, max(0, len(data) - 377))
        for offset in range(limit):
            if data[offset] == 0x47 and data[offset + 188] == 0x47:
                return True
        return False

    if path.endswith((".m4s", ".mp4", ".cmfv", ".cmfa")) or "mp4" in ctype:
        head = data[:4096]
        return any(atom in head for atom in (b"ftyp", b"styp", b"moof", b"mdat"))

    if path.endswith((".aac", ".adts")) or "aac" in ctype:
        return data[0] == 0xFF and (data[1] & 0xF0) == 0xF0

    # Extensionless CDN segments are common. At this point the parent media
    # playlist is valid HLS, so a non-trivial binary body is acceptable.
    return len(data) >= 1024

def _verify_media(
    media_url: str,
    text: str,
    manifest_latency_ms: int,
    variant: dict | None,
    request_headers: dict | None = None,
    opener=None,
):
    info = _media_details(text, media_url)

    base = {
        "resolution": variant.get("resolution") if variant else None,
        "bandwidth": variant.get("bandwidth") if variant else None,
        "manifest_latency_ms": manifest_latency_ms,
    }

    if info["drm"]:
        return {**base, "status": "drm", "reason": "DRM/SAMPLE-AES playlist"}

    if info["key_url"]:
        ok, failure = _aux_reachable(
            info["key_url"],
            request_headers,
            opener=opener,
        )
        if not ok:
            return {**base, "status": failure or "unknown", "reason": "AES-128 key unreachable"}

    if info["init_url"]:
        ok, failure = _aux_reachable(
            info["init_url"],
            request_headers,
            opener=opener,
        )
        if not ok:
            return {**base, "status": failure or "unknown", "reason": "init segment unreachable"}

    segments = info["segments"]
    if not segments:
        return {**base, "status": "unknown", "reason": "playlist has no media segments"}

    # Canlı HLS oynatıcıları canlı kenarın hemen dibindeki segmentten
    # başlamamalıdır. Bu yüzden mümkün olduğunda biraz daha gerideki
    # segmentleri deneriz ve tek bir geçici segment hatasında kanalı düşürmeyiz.
    if info["vod"]:
        candidates = segments[: min(3, len(segments))]
    elif len(segments) >= 5:
        candidates = [segments[-4], segments[-3], segments[-2]]
    elif len(segments) >= 3:
        candidates = [segments[-3], segments[-2]]
    elif len(segments) == 2:
        candidates = [segments[-2], segments[-1]]
    else:
        candidates = [segments[-1]]

    failures = []

    for seg_url in candidates:
        try:
            code, data, final_url, ctype, latency = _request(
                seg_url,
                SEGMENT_TIMEOUT,
                MAX_SEGMENT_BYTES,
                use_range=True,
                request_headers=request_headers,
                opener=opener,
            )
            if not (200 <= code < 400):
                failures.append(("unknown", f"segment HTTP {code}"))
                continue
            if not _payload_looks_media(final_url, data, ctype, info["encrypted"]):
                failures.append(("unknown", "segment payload invalid"))
                continue

            return {
                **base,
                "status": "verified",
                "reason": "real media segment verified",
                "segment_latency_ms": latency,
                "segment_probes": len(failures) + 1,
                "encrypted": info["encrypted"],
                "vod": info["vod"],
            }

        except urllib.error.HTTPError as e:
            failures.append((_failure_for_http(e.code), f"segment HTTP {e.code}"))
        except Exception as e:
            failures.append(("unknown", f"segment {type(e).__name__}"))

    failure_statuses = [status for status, _ in failures]
    if failure_statuses and all(status == "restricted" for status in failure_statuses):
        final_status = "restricted"
    else:
        # Manifest erişilebildiği halde yalnız segmentler 404/410 veriyorsa
        # bunu doğrudan "dead" saymıyoruz; canlı playlist/CDN yarışı olabilir.
        final_status = "unknown"

    return {
        **base,
        "status": final_status,
        "reason": "; ".join(reason for _, reason in failures[:3]) or "no playable segment",
        "segment_probes": len(failures),
        "encrypted": info["encrypted"],
        "vod": info["vod"],
    }

def _validate_hls_attempt(
    url: str,
    user_agent: str | None = None,
    referrer: str | None = None,
):
    request_headers = {
        "user_agent": user_agent,
        "referrer": referrer,
    }

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )

    try:
        code, data, final_url, _, latency = _request(
            url,
            MANIFEST_TIMEOUT,
            MAX_MANIFEST_BYTES,
            request_headers=request_headers,
            opener=opener,
        )
    except urllib.error.HTTPError as e:
        return {
            "status": _failure_for_http(e.code),
            "reason": f"manifest HTTP {e.code}",
            "http_code": e.code,
        }
    except Exception as e:
        return {"status": "unknown", "reason": f"manifest {type(e).__name__}"}

    text = _text(data)
    if "#EXTM3U" not in text[:2048]:
        return {
            "status": "unknown",
            "reason": "response is not HLS",
            "http_code": code,
            "manifest_latency_ms": latency,
        }

    variants = _variants(text, final_url)
    if not variants:
        result = _verify_media(
            final_url,
            text,
            latency,
            None,
            request_headers=request_headers,
            opener=opener,
        )
        result["http_code"] = code
        return result

    failures = []
    # Önce en iyi üç varyantı dene. Bunlar bozuksa en düşük kaliteli
    # varyantı son çare olarak da doğrula; böylece yalnız üst kalite
    # bozuk diye çalışan bir kanalı tamamen kaybetmeyiz.
    candidates = variants[:3]
    if len(variants) > 3:
        lowest = variants[-1]
        if all(item["url"] != lowest["url"] for item in candidates):
            candidates.append(lowest)

    for variant in candidates:
        try:
            vcode, vdata, vfinal, _, vlatency = _request(
                variant["url"],
                MANIFEST_TIMEOUT,
                MAX_MANIFEST_BYTES,
                request_headers=request_headers,
                opener=opener,
            )
            vtext = _text(vdata)
            if "#EXTM3U" not in vtext[:2048]:
                failures.append("variant not HLS")
                continue

            result = _verify_media(
                vfinal,
                vtext,
                vlatency,
                variant,
                request_headers=request_headers,
                opener=opener,
            )
            result["http_code"] = vcode
            result["variants"] = len(variants)

            if result["status"] == "verified":
                return result

            failures.append((
                result.get("status", "unknown"),
                result.get("reason", result.get("status", "unknown")),
            ))

        except urllib.error.HTTPError as e:
            failures.append((
                _failure_for_http(e.code),
                f"variant HTTP {e.code}",
            ))
        except Exception as e:
            failures.append(("unknown", type(e).__name__))

    statuses = [status for status, _ in failures]
    if statuses and all(status == "restricted" for status in statuses):
        final_status = "restricted"
    elif statuses and all(status == "drm" for status in statuses):
        final_status = "drm"
    else:
        final_status = "unknown"

    return {
        "status": final_status,
        "reason": "; ".join(reason for _, reason in failures[:3]) or "no playable variant",
        "variants": len(variants),
    }


VLC_FALLBACK_UA = "VLC/3.0.21 LibVLC/3.0.21"


def validate_hls(
    url: str,
    user_agent: str | None = None,
    referrer: str | None = None,
):
    result = _validate_hls_attempt(url, user_agent=user_agent, referrer=referrer)
    if result.get("status") == "unknown" and not user_agent:
        # Paced retry with VLC player UA for IPTV/streaming servers that throttle standard browser headers
        vlc_result = _validate_hls_attempt(url, user_agent=VLC_FALLBACK_UA, referrer=referrer)
        if vlc_result.get("status") == "verified":
            vlc_result["fallback_user_agent"] = VLC_FALLBACK_UA
            return vlc_result
    return result
