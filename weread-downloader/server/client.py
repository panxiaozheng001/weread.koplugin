"""
HTTP client for WeRead APIs.

Two authentication systems:
  1. Gateway API  — Bearer <api_key> at https://i.weread.qq.com/api/agent/gateway
  2. Web API      — Cookie auth at https://weread.qq.com/...
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from . import protocol

logger = logging.getLogger(__name__)

COOKIE_FILE = Path(__file__).resolve().parent.parent / "data" / "state.json"
WECHAT_UA = protocol.USER_AGENT


# ---------------------------------------------------------------------------
# Persistent state (cookies + API key + wr_ticket + wr_wrpa)
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path: Path = COOKIE_FILE) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    def update(self, data: dict[str, Any]) -> None:
        self._data.update(data)
        self.save()

    @property
    def cookies(self) -> dict[str, str]:
        return self._data.get("cookies", {})

    @property
    def api_key(self) -> str:
        return self._data.get("api_key", "")

    @property
    def is_logged_in(self) -> bool:
        return bool(self.api_key) and bool(self.cookies)


# ---------------------------------------------------------------------------
# Cookie header builder
# ---------------------------------------------------------------------------

def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def merge_set_cookie(cookies: dict[str, str], set_cookie_header: str | None) -> dict[str, str]:
    """Parse Set-Cookie header and merge into existing cookie dict."""
    if not set_cookie_header:
        return cookies
    result = dict(cookies)
    for part in set_cookie_header.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        name_value = part.split(";", 1)[0].strip()
        if "=" not in name_value:
            continue
        name, value = name_value.split("=", 1)
        result[name.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, state: State) -> None:
        self.state = state
        self._http = httpx.Client(
            timeout=15,
            follow_redirects=False,
            headers={
                "User-Agent": WECHAT_UA,
                "Accept": "application/json, text/plain, */*",
            },
        )

    def close(self) -> None:
        self._http.close()

    # -- low-level request --------------------------------------------------

    def request(self, method: str, url: str, *, json_body=None, headers=None,
                timeout=None, persist_cookies=True, skip_cookie=False,
                raw=False) -> str | dict:
        merged_headers: dict[str, str] = {}

        if not skip_cookie and "weread.qq.com" in url:
            cookie_header = cookies_to_header(self.state.cookies)
            if cookie_header:
                merged_headers["Cookie"] = cookie_header

        if json_body is not None:
            merged_headers["Content-Type"] = "application/json;charset=UTF-8"
            merged_headers["Origin"] = "https://weread.qq.com"
            if "Referer" not in (headers or {}):
                merged_headers["Referer"] = "https://weread.qq.com/"

        if headers:
            merged_headers.update(headers)

        kwargs: dict[str, Any] = {"method": method, "url": url, "headers": merged_headers}
        if json_body is not None:
            kwargs["content"] = json.dumps(json_body, ensure_ascii=False)
        if timeout:
            kwargs["timeout"] = timeout

        resp = self._http.request(**kwargs)

        # Persist Set-Cookie if applicable
        if persist_cookies and "weread.qq.com" in url:
            sc = resp.headers.get("set-cookie")
            if sc:
                new_cookies = merge_set_cookie(self.state.cookies, sc)
                self.state.update({"cookies": new_cookies})

        body = resp.text
        if resp.status_code >= 400:
            raise HTTPError(resp.status_code, body, url)

        if raw:
            return body

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"_raw": body, "_status": resp.status_code}

    def post_json(self, url: str, data: dict, **kwargs) -> dict:
        return self.request("POST", url, json_body=data, **kwargs)

    def get_text(self, url: str, **kwargs) -> str:
        result = self.request("GET", url, persist_cookies=False, **kwargs)
        return result.get("_raw", "") if isinstance(result, dict) and "_raw" in result else json.dumps(result)

    def get_json(self, url: str, **kwargs) -> dict:
        return self.request("GET", url, persist_cookies=False, **kwargs)

    # -- Gateway API --------------------------------------------------------

    def gateway(self, api_name: str, params: dict | None = None) -> dict:
        api_key = self.state.api_key
        if not api_key:
            raise RuntimeError("WeRead API key not configured. Please login first.")
        payload = {
            "api_name": api_name,
            **(params or {}),
            "skill_version": "1.0.5",
        }
        return self.post_json(
            f"https://i.weread.qq.com/api/agent/gateway",
            payload,
            headers={"Authorization": f"Bearer {api_key}"},
            skip_cookie=True,
        )

    def get_shelf(self) -> dict:
        return self.gateway("/shelf/sync", {})

    def get_book_info(self, book_id: str) -> dict:
        return self.gateway("/book/info", {"bookId": book_id})

    def get_progress(self, book_id: str) -> dict:
        return self.gateway("/book/getprogress", {"bookId": book_id})

    def search(self, keyword: str, count: int = 10) -> dict:
        return self.gateway("/store/search", {"keyword": keyword, "count": count})

    # -- Web API (cookie auth) ----------------------------------------------

    def get_reader_html(self, url: str) -> str:
        # Persist cookies: the reader page issues a fresh wr_skey/wr_rt Web
        # Reader session that every subsequent content request depends on.
        return self.request("GET", url, raw=True)

    def fetch_chapter_shard(self, book_id: str, chapter_uid, endpoint: str, psvts: str) -> str:
        params = protocol.make_content_params(
            book_id, chapter_uid, psvts,
            sc=1, style=("/e_2" in endpoint),
        )
        chapter_url = protocol.reader_url(book_id, chapter_uid)
        resp = self.post_json(
            f"https://weread.qq.com{endpoint}",
            params,
            headers={
                "Referer": chapter_url,
                "Origin": "https://weread.qq.com",
            },
        )
        # Response may be a raw string or JSON-wrapped; extract the string body
        if isinstance(resp, dict) and "_raw" in resp:
            return resp["_raw"]
        if isinstance(resp, str):
            return resp
        return json.dumps(resp, ensure_ascii=False)

    def download_binary(self, url: str) -> bytes:
        cookie_header = cookies_to_header(self.state.cookies)
        headers = {"User-Agent": WECHAT_UA, "Accept": "*/*", "Referer": "https://weread.qq.com/"}
        if cookie_header:
            headers["Cookie"] = cookie_header
        # Image/tar URLs redirect to a signed CDN host, so redirects must be followed.
        resp = self._http.get(url, headers=headers, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    # -- Cookie renewal -----------------------------------------------------

    def renew_cookie(self) -> dict:
        cookie_header = cookies_to_header(self.state.cookies)
        resp = self._http.post(
            "https://weread.qq.com/web/login/renewal",
            headers={
                "User-Agent": WECHAT_UA,
                "Cookie": cookie_header,
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://weread.qq.com",
                "Referer": "https://weread.qq.com/",
            },
            content=json.dumps({"rq": "%2Fweb%2Fbook%2Fread", "ql": False}),
        )
        try:
            result = resp.json()
        except json.JSONDecodeError:
            result = {}

        new_cookies = merge_set_cookie(self.state.cookies, resp.headers.get("set-cookie"))
        updates: dict[str, Any] = {"cookies": new_cookies}

        wr_ticket = resp.headers.get("x-wr-ticket")
        if wr_ticket:
            updates["wr_ticket"] = wr_ticket
        wrpa = resp.headers.get("x-wrpa-0")
        if wrpa:
            updates["wr_wrpa"] = wrpa

        self.state.update(updates)
        return result


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
