"""
Content decoding, chapter fetching, and EPUB generation.

Core algorithms ported from:
  - scripts/fetch_weread_epub.py (decode + EPUB)
  - weread/lib/content.lua (multi-body handling + workspace)
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import logging
import posixpath
import re
import tarfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import protocol
from .client import Client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content decoding — scripts/fetch_weread_epub.py:366–464
# ---------------------------------------------------------------------------

def checked_body(response_text: str) -> str:
    """Verify MD5 checksum of a content shard."""
    if len(response_text) <= 32:
        return ""
    expected = response_text[:32]
    body = response_text[32:]
    actual = hashlib.md5(body.encode("utf-8")).hexdigest().upper()
    if actual != expected:
        raise ValueError(f"Shard MD5 mismatch: expected={expected}, actual={actual}")
    return body


def swap_positions(encoded: str) -> list[int]:
    """Compute positions to swap from trailing bytes of the encoded string."""
    length = len(encoded)
    if length < 4:
        return []
    if length < 11:
        return [0, 2]

    n = min(4, (length + 9) // 10)
    tmp = ""
    for i in range(length - 1, length - n - 1, -1):
        tmp += str(int(bin(ord(encoded[i]))[2:], 4))

    result: list[int] = []
    m = length - n - 2
    step = len(str(m))
    i = 0
    while len(result) < 10 and i + step < len(tmp):
        result.append(int(tmp[i : i + step]) % m)
        if i + 1 < len(tmp):
            end2 = min(i + 1 + step, len(tmp))
            result.append(int(tmp[i + 1 : end2]) % m)
        i += step
    return result


def reverse_swaps(encoded: str, positions: list[int]) -> str:
    """Reverse the character swaps at the given positions."""
    chars = list(encoded)
    for i in range(len(positions) - 1, -1, -2):
        for k in (1, 0):
            left = positions[i] + k
            right = positions[i - 1] + k
            if 0 <= left < len(chars) and 0 <= right < len(chars):
                chars[left], chars[right] = chars[right], chars[left]
    return "".join(chars)


def repair_utf8(binary_text: str) -> str:
    """Reconstruct valid UTF-8 from a latin-1 decoded byte string."""
    out: list[str] = []
    i = 0
    while i < len(binary_text):
        b0 = ord(binary_text[i])
        if 0xC0 <= b0 <= 0xDF and i + 1 < len(binary_text):
            b1 = ord(binary_text[i + 1])
            if 0x80 <= b1 <= 0xBF:
                out.append(chr(((b0 & 0x1F) << 6) | (b1 & 0x3F)))
                i += 2
                continue
        if 0xE0 <= b0 <= 0xEF and i + 2 < len(binary_text):
            b1 = ord(binary_text[i + 1])
            b2 = ord(binary_text[i + 2])
            if 0x80 <= b1 <= 0xBF and 0x80 <= b2 <= 0xBF:
                out.append(chr(((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F)))
                i += 3
                continue
        if 0xF0 <= b0 <= 0xF7 and i + 3 < len(binary_text):
            b1 = ord(binary_text[i + 1])
            b2 = ord(binary_text[i + 2])
            b3 = ord(binary_text[i + 3])
            if 0x80 <= b1 <= 0xBF and 0x80 <= b2 <= 0xBF and 0x80 <= b3 <= 0xBF:
                codepoint = ((b0 & 0x07) << 18) | ((b1 & 0x3F) << 12) | ((b2 & 0x3F) << 6) | (b3 & 0x3F)
                out.append(chr(codepoint))
                i += 4
                continue
        out.append(binary_text[i])
        i += 1
    return "".join(out)


def decode_encoded_payload(encoded_payload: str) -> str:
    """Decode a single WeRead encoded shard (strip header, reverse swaps, base64 decode)."""
    if not encoded_payload:
        return ""
    encoded_payload = encoded_payload[1:]  # drop first byte
    reordered = reverse_swaps(encoded_payload, swap_positions(encoded_payload))
    b64 = re.sub(r"[^A-Za-z0-9+/]", "", reordered.replace("-", "+").replace("_", "/"))
    padding = "=" * (-len(b64) % 4)
    binary = base64.b64decode(b64 + padding).decode("latin-1")
    return repair_utf8(binary)


def decode_content_shards(e0: str, e1: str, e3: str) -> str:
    """Decode and concatenate three content shards (e_0 + e_1 + e_3) into HTML."""
    payload = checked_body(e0) + checked_body(e1) + checked_body(e3)
    return decode_encoded_payload(payload)


def decode_style_shard(e2: str) -> str:
    """Decode the CSS style shard (e_2)."""
    return decode_encoded_payload(checked_body(e2))


# ---------------------------------------------------------------------------
# HTML body extraction — weread/lib/content.lua:677
# ---------------------------------------------------------------------------

def body_fragment(xhtml: str) -> str:
    """Extract all <body>...</body> fragments from possibly multi-document HTML."""
    bodies: list[str] = []
    remaining = xhtml or ""
    while remaining:
        body_start = remaining.find("<body")
        if body_start == -1:
            break
        body_open_end = remaining.find(">", body_start)
        if body_open_end == -1:
            break
        body_close = remaining.find("</body>", body_open_end)
        if body_close == -1:
            bodies.append(remaining[body_open_end + 1:])
            break
        bodies.append(remaining[body_open_end + 1:body_close])
        remaining = remaining[body_close + 7:]
    if bodies:
        return "\n".join(bodies)
    # Fallback: strip xml/doctype
    xhtml = re.sub(r"<\?xml.*?\?>", "", xhtml, flags=re.S)
    xhtml = re.sub(r"<!DOCTYPE.*?>", "", xhtml, flags=re.S | re.I)
    return xhtml


# ---------------------------------------------------------------------------
# Reader state extraction — scripts/fetch_weread_epub.py:310
# ---------------------------------------------------------------------------

def extract_reader_state(reader_html: str) -> dict[str, Any]:
    """Extract psvts/pclts/token from reader HTML __INITIAL_STATE__."""
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*\(function",
        reader_html,
        flags=re.S,
    )
    if not match:
        # Fallback: try to extract individual fields
        psvts_m = re.search(r'"psvts"\s*:\s*"([^"]+)"', reader_html)
        pclts_m = re.search(r'"pclts"\s*:\s*"([^"]+)"', reader_html)
        token_m = re.search(r'"token"\s*:\s*"([^"]+)"', reader_html)
        return {
            "psvts": psvts_m.group(1) if psvts_m else "",
            "pclts": pclts_m.group(1) if pclts_m else "",
            "token": token_m.group(1) if token_m else "",
        }
    import json
    state = json.loads(match.group(1))
    reader = state.get("reader", {})
    return {
        "psvts": reader.get("psvts", ""),
        "pclts": reader.get("pclts", ""),
        "token": reader.get("token", ""),
        "book_info": reader.get("bookInfo", {}),
    }


# ---------------------------------------------------------------------------
# Chapter fetching
# ---------------------------------------------------------------------------

@dataclass
class EpubAsset:
    href: str
    media_type: str
    data: bytes


@dataclass
class ChapterResult:
    title: str
    xhtml: str
    css: str
    assets: list[EpubAsset]
    is_txt: bool


def fetch_chapter(
    client: Client,
    book_id: str,
    chapter: dict[str, Any],
    *,
    psvts: str = "",
    sleep_seconds: float = 0.1,
) -> ChapterResult:
    """
    Fetch and decode a single chapter. Returns decoded XHTML + CSS + assets.
    If psvts is empty, visits the reader page first to obtain it.
    """
    chapter_uid = chapter.get("chapterUid") or chapter.get("chapterId") or ""
    title = chapter.get("title") or f"Chapter {chapter_uid}"

    # Ensure we have psvts
    if not psvts:
        reader_html = client.get_reader_html(protocol.reader_url(book_id, chapter_uid))
        state = extract_reader_state(reader_html)
        psvts = state.get("psvts", "")

    if not psvts:
        raise RuntimeError(f"Missing psvts for chapterUid={chapter_uid}")

    referer = protocol.reader_url(book_id, chapter_uid)
    content_format = ["auto"]

    def post_shard(endpoint: str, *, style: bool = False) -> str:
        params = protocol.make_content_params(book_id, chapter_uid, psvts, style=style, sc=1)
        from .client import HTTPError
        try:
            text = client.request(
                "POST",
                f"https://weread.qq.com{endpoint}",
                json_body=params,
                headers={"Referer": referer, "Origin": "https://weread.qq.com"},
                raw=True,
            )
            if text == "{}":
                raise ValueError(f"{endpoint} returned empty object")
            if sleep_seconds:
                time.sleep(sleep_seconds)
            return text
        except HTTPError as exc:
            raise RuntimeError(f"{endpoint} failed: {exc}") from exc

    # --- TXT format detection ---
    e0 = post_shard("/web/book/chapter/e_0")
    if e0.startswith("{") and '"bookId"' in e0:
        content_format[0] = "txt"

    if content_format[0] == "txt":
        t0 = post_shard("/web/book/chapter/t_0")
        t1_text = ""
        try:
            t1_text = post_shard("/web/book/chapter/t_1")
        except Exception:
            pass
        plain = decode_content_shards(t0, t1_text, "")
        return ChapterResult(
            title=title,
            xhtml=txt_to_xhtml(plain),
            css="",
            assets=[],
            is_txt=True,
        )

    # --- EPUB format ---
    content_format[0] = "epub"
    e1 = post_shard("/web/book/chapter/e_1")
    e3 = post_shard("/web/book/chapter/e_3")
    content = decode_content_shards(e0, e1, e3)

    css = ""
    try:
        e2 = post_shard("/web/book/chapter/e_2", style=True)
        css = decode_style_shard(e2)
    except Exception:
        pass

    # Image assets: two independent sources, mirroring the KOReader plugin.
    #   1. chapter.tar — a packaged archive of the chapter's images
    #   2. inline remote URLs — <img src="https://..."> left in the body
    assets: list[EpubAsset] = []
    used_names: dict[str, bool] = {}
    tar_url = chapter.get("tar")
    if tar_url:
        try:
            tar_assets, src_map = _download_chapter_assets(client, tar_url, referer)
            content = _rewrite_image_sources(content, src_map)
            for asset in tar_assets:
                used_names[posixpath.basename(asset.href)] = True
            assets.extend(tar_assets)
        except Exception as exc:
            logger.warning("Failed to download chapter assets: %s", exc)

    try:
        content, remote_assets = _download_remote_images(client, content, used_names)
        assets.extend(remote_assets)
    except Exception as exc:
        logger.warning("Failed to download inline remote images: %s", exc)

    return ChapterResult(
        title=title,
        xhtml=content,
        css=css,
        assets=assets,
        is_txt=False,
    )


def _download_remote_images(
    client: Client, xhtml: str, used_names: dict[str, bool]
) -> tuple[str, list[EpubAsset]]:
    """
    Download remote <img src="http(s)://..."> references and inline them.
    Ported from weread/lib/content.lua:1162 (download_remote_images).
    """
    pattern = re.compile(r"""src=(['"])(.*?)\1""")
    remote = [m.group(2) for m in pattern.finditer(xhtml) if _remote_url(m.group(2))]
    if not remote:
        return xhtml, []

    assets: list[EpubAsset] = []
    cache: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        quote = match.group(1)
        src = match.group(2)
        url = _remote_url(src)
        if not url:
            return match.group(0)
        if url in cache:
            return f"src={quote}../{cache[url]}{quote}"
        try:
            data = client.download_binary(url)
        except Exception:
            return match.group(0)
        if not data:
            return match.group(0)
        ext, media_type = _image_type(data)
        if not media_type.startswith("image/"):
            return match.group(0)
        filename = _unique_name(used_names, _url_basename(url), ext)
        href = "images/" + filename
        cache[url] = href
        assets.append(EpubAsset(href=href, media_type=media_type, data=data))
        return f"src={quote}../{href}{quote}"

    return pattern.sub(replace, xhtml), assets


def _url_basename(url: str) -> str:
    """
    Filename portion of a URL.

    WeRead appends decoy query data without a "?" separator
    (e.g. "...cover1.jpg&xxxxxxxxxxxxxxxxxxx"). The CDN ignores it, so take the
    filename from the path component and stop at the first "&" or "?" so the
    decoy never leaks into the EPUB asset name.
    """
    path = urlparse(url).path
    name = posixpath.basename(path)
    return re.split(r"[&?#]", name, maxsplit=1)[0]


def _remote_url(src: str) -> str | None:
    """Return an absolute remote URL for a src value, or None if not remote."""
    # HTML entities must be decoded first: sources arrive as
    # "...jpg&amp;xxxx" and the "&" would otherwise leak into the request URL
    # and the derived filename (matches content.lua rewrite_image_sources).
    url = html.unescape(src or "")
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def _unique_name(used_names: dict[str, bool], stem: str, ext: str) -> str:
    """Derive a collision-free asset filename."""
    base = stem or "image"
    if not base.lower().endswith(ext.lower()):
        base += ext
    name = base
    counter = 1
    while used_names.get(name):
        stem_part, _, ext_part = name.rpartition(".")
        name = f"{stem_part}_{counter}.{ext_part}" if stem_part else f"image_{counter}{ext}"
        counter += 1
    used_names[name] = True
    return name


def _download_chapter_assets(
    client: Client, tar_url: str, referer: str
) -> tuple[list[EpubAsset], dict[str, str]]:
    """Download tar archive of chapter images, extract image files."""
    from .client import cookies_to_header
    headers = {
        "User-Agent": protocol.USER_AGENT,
        "Accept": "*/*",
        "Referer": referer,
        "Cookie": cookies_to_header(client.state.cookies),
    }
    resp = client._http.get(tar_url, headers=headers, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    raw = resp.content

    assets: list[EpubAsset] = []
    src_map: dict[str, str] = {}

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            ext, media_type = _image_type(data)
            if not media_type.startswith("image/"):
                continue
            stem = posixpath.basename(member.name)
            filename = stem if stem.endswith(ext) else stem + ext
            href = "images/" + filename
            epub_relative = "../" + href
            assets.append(EpubAsset(href=href, media_type=media_type, data=data))
            src_map[stem] = epub_relative
            src_map[filename] = epub_relative

    return assets, src_map


def _image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif", "image/gif"
    if data.startswith(b"RIFF") and len(data) > 11 and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".bin", "application/octet-stream"


def _rewrite_image_sources(source: str, src_map: dict[str, str]) -> str:
    if not src_map:
        return source

    def replace_src(match: re.Match) -> str:
        quote = match.group(1)
        src = html.unescape(match.group(2))
        key = posixpath.basename(urlparse(src).path)
        href = src_map.get(key)
        if not href:
            return match.group(0)
        return f"src={quote}{href}{quote}"

    return re.sub(r"src=(['\"])(.*?)\1", replace_src, source)


def txt_to_xhtml(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraphs = [f"<p>{html.escape(line.rstrip())}</p>" for line in lines if line.strip()]
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title></title></head>\n'
        "<body>\n" + "\n".join(paragraphs) + "\n</body></html>"
    )


# ---------------------------------------------------------------------------
# EPUB generation — scripts/fetch_weread_epub.py:561
# ---------------------------------------------------------------------------

def write_epub(
    output: Path,
    *,
    title: str,
    author: str,
    chapters: list[tuple[str, str, list[EpubAsset]]],
    css: str,
) -> Path:
    """Write a standard EPUB3 file from decoded chapter content."""
    output.parent.mkdir(parents=True, exist_ok=True)
    book_uuid = f"urn:uuid:{uuid.uuid4()}"

    nav_items = "\n".join(
        f'      <li><a href="text/chapter_{i:04d}.xhtml">{html.escape(ch_title)}</a></li>'
        for i, (ch_title, _, _) in enumerate(chapters, start=1)
    )
    nav = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">
<head><meta charset="utf-8"/><title>Table of Contents</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>目录</h1>
    <ol>
{nav_items}
    </ol>
  </nav>
</body>
</html>'''

    manifest_chapters = "\n".join(
        f'    <item id="chapter_{i:04d}" href="text/chapter_{i:04d}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(1, len(chapters) + 1)
    )
    all_assets: dict[str, EpubAsset] = {}
    for _, _, assets in chapters:
        for asset in assets:
            all_assets[asset.href] = asset
    manifest_assets = "\n".join(
        f'    <item id="asset_{i:04d}" href="{html.escape(asset.href)}" media-type="{html.escape(asset.media_type)}"/>'
        for i, asset in enumerate(all_assets.values(), start=1)
    )
    spine_chapters = "\n".join(
        f'    <itemref idref="chapter_{i:04d}"/>'
        for i in range(1, len(chapters) + 1)
    )

    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{html.escape(book_uuid)}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>zh-CN</dc:language>
    <meta property="dcterms:modified">{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="style" href="styles/weread.css" media-type="text/css"/>
{manifest_chapters}
{manifest_assets}
  </manifest>
  <spine toc="toc">
{spine_chapters}
  </spine>
</package>'''

    ncx_points = "\n".join(
        f'''    <navPoint id="navPoint-{i}" playOrder="{i}">
      <navLabel><text>{html.escape(ch_title)}</text></navLabel>
      <content src="text/chapter_{i:04d}.xhtml"/>
    </navPoint>'''
        for i, (ch_title, _, _) in enumerate(chapters, start=1)
    )
    ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{html.escape(book_uuid)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(title)}</text></docTitle>
  <navMap>
{ncx_points}
  </navMap>
</ncx>'''

    container = '''<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''

    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/styles/weread.css", css or "body { line-height: 1.6; }\n")
        for asset in all_assets.values():
            zf.writestr("OEBPS/" + asset.href, asset.data)
        for i, (ch_title, ch_source, _) in enumerate(chapters, start=1):
            zf.writestr(
                f"OEBPS/text/chapter_{i:04d}.xhtml",
                _make_chapter_xhtml(ch_title, ch_source),
            )

    return output


def _make_chapter_xhtml(title: str, source: str) -> str:
    body = body_fragment(source)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../styles/weread.css"/>
</head>
<body>
{body}
</body>
</html>'''
