"""
FastAPI application — WeRead local downloader.

Routes:
  Auth:   GET /api/auth/status, GET /api/auth/qr, GET /api/auth/qr/status
  Shelf:  GET /api/shelf, GET /api/search, GET /api/book/{book_id}
  Chapter:GET /api/book/{book_id}/chapters
  Download: POST /api/download/full, POST /api/download/chapters,
            GET /api/download/{task_id}, GET /api/download/{task_id}/events (SSE),
            POST /api/download/{task_id}/cancel, GET /api/epub/{filename}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import protocol
from .auth import LoginSession, start_login
from .client import Client, State
from .content import extract_reader_state
from .tasks import DOWNLOAD_DIR, TaskManager

logger = logging.getLogger(__name__)

app = FastAPI(title="WeRead Downloader", version="0.1.0")

# --- Global state (single-user app) ---
state = State()
client = Client(state)
task_manager = TaskManager()
active_login: LoginSession | None = None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/api/auth/status")
def auth_status():
    return {
        "logged_in": state.is_logged_in,
        "account_name": state.get("account_name", ""),
    }


@app.get("/api/auth/qr")
async def auth_qr():
    """Start QR login flow, return QR code info."""
    global active_login
    session, qr_info = start_login(state)
    active_login = session
    return qr_info


@app.get("/api/auth/qr/status")
async def auth_qr_status(otp: str = ""):
    """
    Poll QR login status.
    Query params: otp=<4-digit-code> (when awaiting OTP).
    Returns {status, account_name?, error?}
    """
    global active_login
    if not active_login:
        raise HTTPException(400, "No active login session. Call /api/auth/qr first.")

    try:
        result = active_login.poll(otp=otp)
    except Exception as exc:
        active_login.close()
        active_login = None
        return {"status": "error", "error": str(exc)}

    if result["status"] == "confirmed":
        try:
            finish_result = active_login.finish(result["data"])
            active_login.close()
            active_login = None
            return {"status": "confirmed", "account_name": finish_result["account_name"]}
        except Exception as exc:
            active_login.close()
            active_login = None
            return {"status": "error", "error": str(exc)}

    if result["status"] in ("error", "timeout"):
        active_login.close()
        active_login = None

    return result


@app.post("/api/auth/renew")
def auth_renew():
    """Refresh cookies."""
    if not state.is_logged_in:
        raise HTTPException(401, "Not logged in")
    try:
        client.renew_cookie()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Shelf & search routes
# ---------------------------------------------------------------------------

@app.get("/api/shelf")
def get_shelf():
    """Get user's bookshelf."""
    if not state.is_logged_in:
        raise HTTPException(401, "Not logged in")
    try:
        result = client.get_shelf()
        books = result.get("books", [])
        return {"books": books, "count": len(books)}
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch shelf: {exc}")


@app.get("/api/search")
def search_books(q: str = "", count: int = 10):
    """Search WeRead books."""
    if not state.is_logged_in:
        raise HTTPException(401, "Not logged in")
    if not q:
        return {"results": []}
    try:
        result = client.search(q, count=count)
        books = []
        for group in result.get("results", []):
            for entry in group.get("books", []):
                book = entry.get("bookInfo", entry)
                books.append({
                    "book_id": book.get("bookId") or book.get("book_id", ""),
                    "title": book.get("title", ""),
                    "author": book.get("author", ""),
                    "cover": book.get("cover", ""),
                    "category": book.get("category", ""),
                })
        return {"books": books}
    except Exception as exc:
        raise HTTPException(502, f"Search failed: {exc}")


@app.get("/api/book/{book_id}")
def get_book(book_id: str):
    """Get book info + progress."""
    if not state.is_logged_in:
        raise HTTPException(401, "Not logged in")
    try:
        info = client.get_book_info(book_id)
        progress = client.get_progress(book_id)
        return {"book": info, "progress": progress.get("book", {})}
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch book info: {exc}")


# ---------------------------------------------------------------------------
# Chapter routes
# ---------------------------------------------------------------------------

@app.get("/api/book/{book_id}/chapters")
async def get_chapters(book_id: str):
    """Get chapter list for a book."""
    if not state.is_logged_in:
        raise HTTPException(401, "Not logged in")
    try:
        # Visit reader page to get catalog
        reader_url = protocol.reader_url(book_id)
        reader_html = await asyncio.to_thread(client.get_reader_html, reader_url)
        state_data = extract_reader_state(reader_html)
        psvts = state_data.get("psvts", "")

        # Fetch chapter catalog via Web API
        from .client import HTTPError
        try:
            result = client.post_json(
                "https://weread.qq.com/web/book/chapterInfos",
                {"bookIds": [book_id]},
                headers={"Referer": reader_url},
            )
        except Exception:
            result = {}

        # Extract chapters from response
        chapters: list[dict] = []

        def _extract_chapters(payload: dict) -> list[dict]:
            data = payload.get("data", [])
            if not isinstance(data, list):
                return []
            for item in data:
                if str(item.get("bookId", "")) == book_id:
                    return item.get("updated") or item.get("chapterInfos") or item.get("chapters") or []
            return []

        chapters = _extract_chapters(result)

        # errCode -2012 means the session expired; renew and retry once.
        if not chapters and result.get("errCode") in (-2012, -2011):
            try:
                await asyncio.to_thread(client.renew_cookie)
                await asyncio.to_thread(client.get_reader_html, reader_url)
                result = await asyncio.to_thread(
                    client.post_json,
                    "https://weread.qq.com/web/book/chapterInfos",
                    {"bookIds": [book_id]},
                    headers={"Referer": reader_url},
                )
                chapters = _extract_chapters(result)
            except Exception as exc:
                logger.warning("chapter catalog retry failed: %s", exc)

        # Filter: only chapters with wordCount > 0 and not "封面"
        readable = [
            ch for ch in chapters
            if isinstance(ch, dict)
            and ch.get("chapterUid")
            and int(ch.get("wordCount", 0) or 0) > 0
            and ch.get("title") != "封面"
        ]

        return {
            "chapters": readable,
            "total": len(readable),
            "psvts": psvts,
        }
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch chapters: {exc}")


# ---------------------------------------------------------------------------
# Download routes
# ---------------------------------------------------------------------------

@app.post("/api/download/full")
async def download_full(request: Request):
    """Start a full-book download."""
    if not state.is_logged_in:
        raise HTTPException(401, "Not logged in")
    body = await request.json()
    book_id = body.get("book_id", "")
    chapters = body.get("chapters", [])
    book_title = body.get("title", book_id)

    if not book_id or not chapters:
        raise HTTPException(400, "book_id and chapters are required")

    task = await task_manager.start_full_download(client, book_id, book_title, chapters)
    return {"task_id": task.task_id, "status": "running"}


@app.post("/api/download/chapters")
async def download_chapters(request: Request):
    """Start a multi-chapter download."""
    if not state.is_logged_in:
        raise HTTPException(401, "Not logged in")
    body = await request.json()
    book_id = body.get("book_id", "")
    chapters = body.get("chapters", [])
    book_title = body.get("title", book_id)

    if not book_id or not chapters:
        raise HTTPException(400, "book_id and chapters are required")

    task = await task_manager.start_chapter_download(client, book_id, book_title, chapters)
    return {"task_id": task.task_id, "status": "running"}


@app.get("/api/download/{task_id}")
def get_download(task_id: str):
    """Get download task status."""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task.to_dict()


@app.get("/api/download/{task_id}/events")
async def download_events(task_id: str):
    """SSE endpoint for real-time download progress."""
    queue = task_manager.subscribe(task_id)

    async def event_stream():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    if data.get("status") in ("completed", "failed", "cancelled"):
                        break
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        finally:
            task_manager.unsubscribe(task_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/download/{task_id}/cancel")
async def cancel_download(task_id: str):
    ok = await task_manager.cancel(task_id)
    return {"ok": ok}


@app.get("/api/epub/{filename}")
def download_epub(filename: str):
    """Serve a downloaded EPUB file."""
    # Sanitize filename to prevent path traversal
    safe = re.sub(r"[^a-zA-Z0-9_\-. ]", "", filename)
    epub_path = DOWNLOAD_DIR / safe
    if not epub_path.exists():
        raise HTTPException(404, "EPUB not found")
    return FileResponse(epub_path, media_type="application/epub+zip", filename=safe)


@app.get("/favicon.ico")
def favicon():
    from fastapi.responses import Response
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Static files (frontend) — must be last
# ---------------------------------------------------------------------------

static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def index():
    return FileResponse(static_dir / "index.html")
