"""Tiny JSON-over-HTTP helper with retries (stdlib only)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "predictions-arb/0.1"


def get_json(url: str, params: dict | None = None, retries: int = 4, timeout: float = 30):
    if params:
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # Retry on rate limits and server errors only.
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2
