"""Tiny stdlib HTTP helpers (no `requests` dependency)."""
import json, time, urllib.request, urllib.parse, urllib.error

UA = "ryby-fish-catalog/1.0 (contact: kupco.patrik.16@gmail.com)"

def get_json(url, params=None, timeout=20, retries=2):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return _do(url, None, {"Accept": "application/json"}, timeout, retries, parse="json")

def post_json(url, payload, headers=None, timeout=60, retries=2):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    return _do(url, data, h, timeout, retries, parse="json")

def get_bytes(url, timeout=30, retries=2):
    return _do(url, None, {}, timeout, retries, parse="bytes")

def _do(url, data, headers, timeout, retries, parse):
    h = {"User-Agent": UA}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw.decode("utf-8")) if parse == "json" else raw
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code in (400, 401, 403, 404):
                break          # not worth retrying
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"request failed for {url[:90]} -> {last}")
