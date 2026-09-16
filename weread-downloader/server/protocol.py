"""
WeRead protocol utilities — signature, encoding, URL helpers.

Ported from weread/lib/protocol.lua (Lua bit operations → Python ints).
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import time

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
)

DEFAULT_READER_TOKEN = "3c5c8717f3daf09iop3423zafeqoi"


def md5_hex(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.md5(text).hexdigest()


def sha256_hex(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


# ---------------------------------------------------------------------------
# Query signing — weread/lib/protocol.lua:56
# ---------------------------------------------------------------------------

def sign(query: str) -> str:
    """WeRead query-signing algorithm (bitwise XOR hash)."""
    a = 0x15051505
    b = 0x15051505
    length = len(query)
    i = length

    while i > 1:
        # Lua: bit.band(bit.bxor(a, bit.lshift(query:byte(i), ((length-i+1)%30))), 0x7fffffff)
        byte_val = ord(query[i - 1])
        shift = (length - i + 1) % 30
        a = ((a ^ (byte_val << shift)) & 0x7FFFFFFF)

        byte_val2 = ord(query[i - 2])
        shift2 = (i - 1) % 30
        b = ((b ^ (byte_val2 << shift2)) & 0x7FFFFFFF)

        i -= 2

    return format((a + b) & 0xFFFFFFFF, "x")


# ---------------------------------------------------------------------------
# URL encoding (JS-compatible) — weread/lib/protocol.lua:33
# ---------------------------------------------------------------------------

def urlencode(value) -> str:
    """URL-encode matching JavaScript's encodeURIComponent."""
    s = _js_string(value)
    out: list[str] = []
    for ch in s:
        byte_val = ord(ch) if len(ch) == 1 else ch.encode("utf-8")
        if isinstance(byte_val, int):
            byte_val = bytes([byte_val])
        if ch.isalnum() or ch in "-_.~":
            out.append(ch)
        else:
            for b in byte_val:
                out.append(f"%{b:02X}")
    return "".join(out)


def _js_string(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


# ---------------------------------------------------------------------------
# Sorted query string (without 's') — weread/lib/protocol.lua:40
# ---------------------------------------------------------------------------

def sorted_query(params: dict) -> str:
    """Sorted query string for signing — values NOT URL-encoded (matches Lua)."""
    keys = sorted(k for k in params if k != "s")
    parts = [f"{k}={_js_string(params[k])}" for k in keys]
    return "&".join(parts)


# ---------------------------------------------------------------------------
# Parameter encoding — weread/lib/protocol.lua:79
# ---------------------------------------------------------------------------

def _is_digit_string(s: str) -> bool:
    return s.isdigit()


def _byte_hex(value: bytes) -> str:
    return "".join(f"{b:02x}" for b in value)


def e(value) -> str:
    """WeRead parameter encoder (MD5 + type flag + hex chunks)."""
    s = str(value)
    h = md5_hex(s)
    result = h[:3]

    if _is_digit_string(s):
        type_flag = "3"
        # Split into 9-digit groups (matches the Lua `s:sub(i, i + 8)` window).
        chunks: list[str] = [format(int(s[i : i + 9]), "x") for i in range(0, len(s), 9)]
    else:
        type_flag = "4"
        chunks = [_byte_hex(s.encode("utf-8"))]

    result += type_flag + "2" + h[-2:]
    for idx, chunk in enumerate(chunks):
        result += f"{len(chunk):02x}{chunk}"
        if idx < len(chunks) - 1:
            result += "g"

    if len(result) < 20:
        result += h[: 20 - len(result)]

    result += md5_hex(result)[:3]
    return result


# ---------------------------------------------------------------------------
# web_app_id — weread/lib/protocol.lua:115
# ---------------------------------------------------------------------------

def web_app_id(user_agent: str = USER_AGENT) -> str:
    parts = user_agent.split()
    prefix: list[str] = []
    count = 0
    for part in parts:
        count += 1
        if count > 12:
            break
        prefix.append(str(len(part) % 10))

    hash_val = 0
    for ch in user_agent:
        hash_val = (0x83 * hash_val + ord(ch)) & 0x7FFFFFFF

    return "wb" + "".join(prefix) + "h" + str(hash_val)


# ---------------------------------------------------------------------------
# Content shard params — weread/lib/protocol.lua:196
# ---------------------------------------------------------------------------

def make_content_params(
    book_id: str,
    chapter_uid,
    psvts: str,
    *,
    ct: int | None = None,
    sc: int = 1,
    style: bool = False,
) -> dict:
    now = ct or int(time.time())
    if e(now) == psvts:
        now += 1

    params = {
        "b": e(book_id),
        "c": e(str(chapter_uid)),
        "r": str(random.randint(0, 9999) ** 2),
        "ct": str(now),
        "ps": psvts,
        "pc": e(now),
        "sc": sc,
        "prevChapter": False,
        "st": 1 if style else 0,
    }
    params["s"] = sign(sorted_query(params))
    return params


# ---------------------------------------------------------------------------
# URL construction — weread/lib/protocol.lua:269
# ---------------------------------------------------------------------------

def reader_url(book_id: str, chapter_uid=None) -> str:
    url = "https://weread.qq.com/web/reader/" + e(book_id)
    if chapter_uid is not None:
        url += "k" + e(str(chapter_uid))
    return url


def is_mp_book(book_id: str | None) -> bool:
    return str(book_id or "").startswith("MP_WXS_")


# ---------------------------------------------------------------------------
# Cover URL normalization — weread/lib/protocol.lua:282
# ---------------------------------------------------------------------------

def normalize_cover_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return url
    import re
    return re.sub(r"/t\d+_/", "/t9_/", url)
