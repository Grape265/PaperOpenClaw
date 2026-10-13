"""Desktop entry point: a native window (pywebview) hosting the web UI.

Starts the FastAPI server in a background thread and opens a native window that
loads the local URL — no external browser is launched. If no webview engine is
available, it falls back to opening the system browser.

When frozen, the data directory defaults to ``~/.paperopenclaw`` (see
:func:`paperopenclaw.core.paths.resolve_data_dir`).
"""

from __future__ import annotations

import os
import sys
import threading
import time


def _port() -> int:
    return int(os.environ.get("PAPEROPENCLAW_PORT") or 8787)


def _run_server() -> None:
    import uvicorn

    from .gateway.app import create_app

    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=_port(),
                log_level="warning", access_log=False)


def _wait_until_ready(url: str, timeout: float = 60.0) -> bool:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url + "/api/v1/health", timeout=1.0)
            return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.25)
    return False


def main() -> int:
    port = _port()
    url = f"http://127.0.0.1:{port}"

    server = threading.Thread(target=_run_server, name="paperopenclaw-server", daemon=True)
    server.start()

    if not _wait_until_ready(url):
        print("服务器启动超时", file=sys.stderr)
        return 1

    try:
        import webview

        webview.create_window(
            "PaperOpenClaw",
            url,
            width=1280,
            height=860,
            min_size=(960, 640),
            confirm_close=True,
        )
        webview.start()
        return 0
    except Exception as exc:  # noqa: BLE001 - no webview engine → fall back
        print(f"原生窗口不可用（{exc}），回退到浏览器", file=sys.stderr)
        import webbrowser

        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
