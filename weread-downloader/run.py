#!/usr/bin/env python3
"""
WeRead Local Downloader — startup script.

Usage:
    python run.py [--port 8080] [--no-browser]
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path


def open_browser(port: int, delay: float = 1.5) -> None:
    time.sleep(delay)
    webbrowser.open(f"http://localhost:{port}")


def main() -> None:
    parser = argparse.ArgumentParser(description="WeRead Local Downloader")
    parser.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    if not args.no_browser:
        threading.Thread(target=open_browser, args=(args.port,), daemon=True).start()

    print(f"\n  WeRead Downloader running at http://localhost:{args.port}\n")
    uvicorn.run(
        "server.app:app",
        host="127.0.0.1",
        port=args.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
