"""
QR login flow for WeRead — session establishment, QR generation, polling, OTP, credential extraction.

Flow (from scripts/verify_qr_login.py):
  1. GET /r/weread-skills         → establish session cookies
  2. GET /api/auth/getLoginUid    → obtain UID
  3. GET /api/auth/getLoginInfo   → poll until succeed=True (handle OTP)
  4. Extract wr_vid / wr_skey / wr_rt → install as cookies
  5. GET /api/userInfo            → verify + get user name
  6. GET /api/skills/apikeyGet    → obtain API key (wrk-...)
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from .client import State, cookies_to_header, merge_set_cookie

logger = logging.getLogger(__name__)

BASE_URL = "https://weread.qq.com"
SKILLS_PAGE_URL = f"{BASE_URL}/r/weread-skills"
LOGIN_UID_URL = f"{BASE_URL}/api/auth/getLoginUid"
LOGIN_INFO_URL = f"{BASE_URL}/api/auth/getLoginInfo"
USER_INFO_URL = f"{BASE_URL}/api/userInfo"
API_KEY_URL = f"{BASE_URL}/api/skills/apikeyGet?only_show=1"

# QR image generation via third-party API (no local QR lib needed)
QR_IMAGE_API = "https://api.qrserver.com/v1/create-qr-code/?size=256x256&data="

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
)


def _headers(referer: str | None = None, vid: str = "", skey: str = "") -> dict[str, str]:
    h: dict[str, str] = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    if referer:
        h["Referer"] = referer
    if vid:
        h["X-Vid"] = vid
    if skey:
        h["X-Skey"] = skey
    return h


# ---------------------------------------------------------------------------
# Step 1: Establish session + get login UID
# ---------------------------------------------------------------------------

class LoginSession:
    """Manages an in-progress QR login session."""

    def __init__(self, state: State) -> None:
        self.state = state
        self._http = httpx.Client(timeout=20, follow_redirects=True)
        self.uid: str = ""
        self.confirm_url: str = ""
        self.qr_image_url: str = ""
        self.status: str = "pending"  # pending | awaiting_otp | confirmed | error
        self.error: str = ""
        self.account_name: str = ""
        # For internal tracking across OTP retries
        self._last_login_info: dict[str, Any] = {}
        # Manual cookie accumulation (httpx auto-cookie may miss domain cookies)
        self._accumulated_cookies: dict[str, str] = {}

    def close(self) -> None:
        self._http.close()

    def _capture_cookies(self, resp: httpx.Response) -> None:
        """Manually extract Set-Cookie values from response."""
        for value in resp.headers.get_list("set-cookie"):
            # Parse "name=value; ..." format
            part = value.split(";", 1)[0].strip()
            if "=" in part:
                name, val = part.split("=", 1)
                self._accumulated_cookies[name.strip()] = val.strip()

    def start(self) -> dict[str, Any]:
        """Step 1+2: Establish session and get UID. Returns {uid, confirm_url, qr_image_url}."""
        # Visit skills page to establish session cookies
        resp = self._http.get(SKILLS_PAGE_URL, headers=_headers(referer=f"{BASE_URL}/"), timeout=20)
        resp.raise_for_status()
        self._capture_cookies(resp)
        logger.info("skills page: status=%d, captured %d cookies", resp.status_code, len(self._accumulated_cookies))

        # Get login UID
        resp2 = self._http.get(
            LOGIN_UID_URL,
            headers=_headers(referer=SKILLS_PAGE_URL),
            timeout=20,
        )
        resp2.raise_for_status()
        self._capture_cookies(resp2)
        data = resp2.json()
        uid = data.get("uid", "")
        if not uid or not isinstance(uid, str):
            raise RuntimeError(f"getLoginUid did not return a valid UID: {data}")

        self.uid = uid
        self.confirm_url = f"{BASE_URL}/web/confirm?uid={quote(uid, safe='')}"
        self.qr_image_url = QR_IMAGE_API + quote(self.confirm_url, safe="")
        self.status = "pending"
        return {
            "uid": uid,
            "confirm_url": self.confirm_url,
            "qr_image_url": self.qr_image_url,
        }

    # ------------------------------------------------------------------
    # Step 3: Poll login status
    # ------------------------------------------------------------------

    def poll(self, otp: str = "") -> dict[str, Any]:
        """
        Poll /api/auth/getLoginInfo.
        Returns {status, data} where status is:
          "pending"       — still waiting for scan
          "awaiting_otp"  — 4-digit OTP needed
          "otp_not_match" — OTP was wrong, try again
          "confirmed"     — login succeeded
          "timeout"       — login timed out
          "error"         — unexpected error
        """
        url = f"{LOGIN_INFO_URL}?uid={quote(self.uid, safe='')}&otp"
        if otp:
            url += f"={quote(otp, safe='')}"

        resp = self._http.get(url, headers=_headers(referer=SKILLS_PAGE_URL), timeout=70)
        resp.raise_for_status()
        self._capture_cookies(resp)
        data = resp.json()

        self._last_login_info = data

        if data.get("succeed") is True:
            self.status = "confirmed"
            return {"status": "confirmed", "data": data}

        logic_code = str(data.get("logicCode", ""))

        if logic_code == "NEED_OTP":
            self.status = "awaiting_otp"
            return {"status": "awaiting_otp"}

        if logic_code == "OTP_NOT_MATCH":
            self.status = "awaiting_otp"
            return {"status": "otp_not_match"}

        if logic_code in ("LOGIN_TIMEOUT", "OTP_EXPIRED"):
            self.status = "error"
            self.error = logic_code
            return {"status": "timeout", "error": logic_code}

        # Still waiting
        self.status = "pending"
        return {"status": "pending", "logicCode": logic_code}

    # ------------------------------------------------------------------
    # Steps 4–6: Install credentials + verify
    # ------------------------------------------------------------------

    def finish(self, login_data: dict[str, Any]) -> dict[str, Any]:
        """
        After successful login, extract cookies, fetch user info + API key.
        Persists everything to State. Returns {account_name, user_info, api_key}.
        """
        web_login_vid = str(login_data.get("webLoginVid", ""))
        access_token = str(login_data.get("accessToken", ""))
        refresh_token = str(login_data.get("refreshToken", ""))

        if not web_login_vid or not access_token:
            raise RuntimeError("Login response is missing webLoginVid or accessToken")

        # Merge auth cookies with accumulated session cookies
        all_cookies = dict(self._accumulated_cookies)
        all_cookies.update({
            "wr_vid": web_login_vid,
            "wr_skey": access_token,
            "wr_ql": "0",
        })
        if refresh_token:
            all_cookies["wr_rt"] = quote(refresh_token, safe="")
        self.state.update({"cookies": all_cookies})

        # Fetch user info
        user_info = self._get_json(
            f"{USER_INFO_URL}?userVid={quote(web_login_vid, safe='')}",
            headers=_headers(referer=SKILLS_PAGE_URL, vid=web_login_vid, skey=access_token),
        )
        account_name = str(user_info.get("name", ""))

        # Fetch API key
        api_result = self._get_json(
            API_KEY_URL,
            headers=_headers(referer=SKILLS_PAGE_URL, vid=web_login_vid, skey=access_token),
        )
        api_key = api_result.get("apikey", "")
        if not api_key:
            raise RuntimeError(
                "WeRead did not return an API key. "
                "Enable WeRead Skill in the WeRead app (Me > Settings > WeRead Skill > Get API Key), "
                "then scan again."
            )

        # Persist API key and user info
        self.state.update({
            "api_key": api_key,
            "account_name": account_name,
            "user_info": {k: v for k, v in user_info.items() if k in ("name", "vid", "avatar")},
        })

        self.account_name = account_name
        return {
            "account_name": account_name,
            "api_key": bool(api_key),
        }

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> dict:
        resp = self._http.get(url, headers=headers or {}, timeout=20)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Convenience: one-shot login flow (for API endpoint)
# ---------------------------------------------------------------------------

def start_login(state: State) -> tuple[LoginSession, dict]:
    """Create a LoginSession and start the QR flow. Returns (session, qr_info)."""
    session = LoginSession(state)
    qr_info = session.start()
    return session, qr_info
