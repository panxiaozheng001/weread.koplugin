"""
Download task manager — async background downloads with SSE progress events.

Replaces the Lua downloader.lua state machine with asyncio tasks.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import Client
from .content import (
    ChapterResult,
    EpubAsset,
    extract_reader_state,
    fetch_chapter,
    write_epub,
)
from . import protocol

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "downloads"


@dataclass
class TaskProgress:
    stage: str = ""       # source | images | footnotes | epub | done | error
    current: int = 0
    total: int = 0
    message: str = ""


@dataclass
class DownloadTask:
    task_id: str
    book_id: str
    book_title: str
    chapters: list[dict[str, Any]]
    suffix: str = "full"
    status: str = "pending"   # pending | running | completed | failed | cancelled
    progress: TaskProgress = field(default_factory=TaskProgress)
    epub_path: str = ""
    error: str = ""
    started_at: float = 0
    completed_at: float = 0
    # SSE subscribers (list of asyncio.Queue)
    subscribers: list[asyncio.Queue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "book_id": self.book_id,
            "book_title": self.book_title,
            "status": self.status,
            "stage": self.progress.stage,
            "current": self.progress.current,
            "total": self.progress.total,
            "message": self.progress.message,
            "epub_path": self.epub_path,
            "error": self.error,
        }


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, DownloadTask] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get_task(self, task_id: str) -> DownloadTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[dict]:
        return [t.to_dict() for t in self._tasks.values()]

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """Subscribe to SSE progress events for a task. Returns a Queue."""
        task = self._tasks.get(task_id)
        if not task:
            q: asyncio.Queue = asyncio.Queue()
            q.put_nowait({"stage": "error", "message": "task not found"})
            return q
        q = asyncio.Queue()
        task.subscribers.append(q)
        # Send current state immediately
        q.put_nowait(task.to_dict())
        return q

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        task = self._tasks.get(task_id)
        if task and queue in task.subscribers:
            task.subscribers.remove(queue)

    async def _broadcast(self, task: DownloadTask) -> None:
        data = task.to_dict()
        for q in list(task.subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def start_full_download(
        self,
        client: Client,
        book_id: str,
        book_title: str,
        chapters: list[dict[str, Any]],
    ) -> DownloadTask:
        """Start a full-book download as a background asyncio task."""
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(
            task_id=task_id,
            book_id=book_id,
            book_title=book_title,
            chapters=chapters,
            suffix="full",
        )
        self._tasks[task_id] = task
        asyncio.create_task(self._run_download(task, client))
        return task

    async def start_chapter_download(
        self,
        client: Client,
        book_id: str,
        book_title: str,
        chapters: list[dict[str, Any]],
    ) -> DownloadTask:
        """Start a single/multi chapter download."""
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(
            task_id=task_id,
            book_id=book_id,
            book_title=book_title,
            chapters=chapters,
            suffix="chapter",
        )
        self._tasks[task_id] = task
        asyncio.create_task(self._run_download(task, client))
        return task

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False
        task.status = "cancelled"
        task.progress.stage = "cancelled"
        await self._broadcast(task)
        return True

    async def _run_download(self, task: DownloadTask, client: Client) -> None:
        """Core download loop — iterates chapters, decodes, builds EPUB."""
        task.status = "running"
        task.started_at = time.time()
        task.progress.total = len(task.chapters)
        await self._broadcast(task)

        try:
            # Obtain psvts by visiting the reader page for the first chapter.
            # This also refreshes the Web Reader session credentials (wr_skey),
            # which every content request depends on.
            first_chapter = task.chapters[0]
            first_uid = first_chapter.get("chapterUid") or first_chapter.get("chapterId", "")
            reader_html = client.get_reader_html(protocol.reader_url(task.book_id, first_uid))
            state = extract_reader_state(reader_html)
            psvts = state.get("psvts", "")

            fetched: list[tuple[str, str, list[EpubAsset]]] = []
            css = ""

            for idx, chapter in enumerate(task.chapters):
                if task.status == "cancelled":
                    task.progress.stage = "cancelled"
                    await self._broadcast(task)
                    return

                task.progress.current = idx
                task.progress.stage = "source"
                chapter_title = chapter.get("title", f"Chapter {idx + 1}")
                task.progress.message = f"下载章节 {idx + 1}/{len(task.chapters)}: {chapter_title}"
                await self._broadcast(task)

                try:
                    result: ChapterResult = await asyncio.to_thread(
                        fetch_chapter, client, task.book_id, chapter, psvts=psvts
                    )
                    if result.css and not css:
                        css = result.css
                    fetched.append((result.title, result.xhtml, result.assets))
                except Exception as exc:
                    logger.warning("Chapter %d failed: %s", idx + 1, exc)
                    continue

                task.progress.stage = "images"
                task.progress.message = f"处理资源 · 章节 {idx + 1}/{len(task.chapters)}"
                await self._broadcast(task)

            if not fetched:
                raise RuntimeError("No chapters were successfully downloaded")

            # Build EPUB
            task.progress.stage = "epub"
            task.progress.message = "生成 EPUB..."
            task.progress.current = task.progress.total
            await self._broadcast(task)

            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            safe_title = "".join(c if c.isalnum() or c in "- _" else "_" for c in task.book_title)
            epub_path = DOWNLOAD_DIR / f"{safe_title}.epub"

            await asyncio.to_thread(
                write_epub,
                epub_path,
                title=task.book_title,
                author="",
                chapters=fetched,
                css=css,
            )

            task.epub_path = str(epub_path)
            task.status = "completed"
            task.progress.stage = "done"
            task.progress.message = f"下载完成：{len(fetched)} 章"
            task.completed_at = time.time()
            await self._broadcast(task)
            logger.info("Download completed: %s (%d chapters)", task.book_title, len(fetched))

        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.progress.stage = "error"
            task.progress.message = f"下载失败：{exc}"
            task.completed_at = time.time()
            await self._broadcast(task)
            logger.error("Download failed: %s", exc)
