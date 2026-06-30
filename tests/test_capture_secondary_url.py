from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SCRIPT = Path("skill/scripts/capture-secondary-url.mjs")


class MockCdpServer(ThreadingHTTPServer):
    eval_count: int
    mode: str


class MockCdpHandler(BaseHTTPRequestHandler):
    server: MockCdpServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/new?"):
            self._send_json({"targetId": "mock-target"})
            return
        if self.path.startswith("/close?"):
            self._send_json({"closed": True})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if not self.path.startswith("/eval?"):
            self._send_json({"error": "not found"}, status=404)
            return

        _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.eval_count += 1
        if self.server.mode == "transient_eval_400" and self.server.eval_count == 1:
            self._send_json({"error": "transient bad request"}, status=400)
            return
        if self.server.mode == "persistent_eval_400":
            self._send_json({"error": "persistent bad request"}, status=400)
            return
        if self.server.mode == "timeout" or self.server.eval_count < 3:
            data = {
                "title": "",
                "description": "",
                "finalUrl": "about:blank",
                "readyState": "complete",
                "text": "",
            }
        else:
            data = {
                "title": "Delayed WeChat Article",
                "description": "Delayed description",
                "finalUrl": "https://mp.weixin.qq.com/s/delayed",
                "readyState": "complete",
                "text": "正文已经加载出来，用于交叉检查。",
            }
        self._send_json({"value": json.dumps(data)})


def run_mock_cdp(mode: str = "delayed") -> tuple[MockCdpServer, str]:
    server = MockCdpServer(("127.0.0.1", 0), MockCdpHandler)
    server.eval_count = 0
    server.mode = mode
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def run_capture(tmp_path: Path, base_url: str, *extra_args: str) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "secondary_context.md"
    env = os.environ.copy()
    env["ZOTERO_PAPERREAD_CDP_BASE_URL"] = base_url
    return subprocess.run(
        [
            "node",
            str(SCRIPT),
            "https://mp.weixin.qq.com/s/delayed",
            "--output",
            str(output),
            *extra_args,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_capture_secondary_url_waits_for_delayed_navigation(tmp_path: Path) -> None:
    server, base_url = run_mock_cdp(mode="delayed")
    try:
        result = run_capture(tmp_path, base_url, "--timeout-ms", "2000", "--poll-ms", "10")
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    assert server.eval_count >= 3
    captured = (tmp_path / "secondary_context.md").read_text(encoding="utf-8")
    assert "source_status: secondary_context" in captured
    assert "final_url: https://mp.weixin.qq.com/s/delayed" in captured
    assert "Delayed WeChat Article" in captured
    assert "正文已经加载出来" in captured


def test_capture_secondary_url_reports_navigation_timeout(tmp_path: Path) -> None:
    server, base_url = run_mock_cdp(mode="timeout")
    try:
        result = run_capture(tmp_path, base_url, "--timeout-ms", "80", "--poll-ms", "10")
    finally:
        server.shutdown()

    assert result.returncode == 1
    captured = (tmp_path / "secondary_context.md").read_text(encoding="utf-8")
    assert "source_status: secondary_context_unavailable" in captured
    assert "capture_warning: navigation_timeout" in captured
    assert "final_url: about:blank" in captured


def test_capture_secondary_url_recovers_from_transient_eval_400(tmp_path: Path) -> None:
    server, base_url = run_mock_cdp(mode="transient_eval_400")
    try:
        result = run_capture(
            tmp_path,
            base_url,
            "--timeout-ms",
            "2000",
            "--poll-ms",
            "10",
            "--request-retries",
            "1",
            "--request-retry-ms",
            "1",
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    captured = (tmp_path / "secondary_context.md").read_text(encoding="utf-8")
    assert "source_status: secondary_context" in captured
    assert "capture_warning: transient_cdp_request_recovered:400 Bad Request" in captured
    assert "正文已经加载出来" in captured


def test_capture_secondary_url_writes_unavailable_file_for_persistent_eval_400(tmp_path: Path) -> None:
    server, base_url = run_mock_cdp(mode="persistent_eval_400")
    try:
        result = run_capture(
            tmp_path,
            base_url,
            "--timeout-ms",
            "80",
            "--poll-ms",
            "10",
            "--request-retries",
            "1",
            "--request-retry-ms",
            "1",
        )
    finally:
        server.shutdown()

    assert result.returncode == 1
    assert "Error:" not in result.stderr
    captured = (tmp_path / "secondary_context.md").read_text(encoding="utf-8")
    assert "source_status: secondary_context_unavailable" in captured
    assert "capture_warning: cdp_request_failed:400 Bad Request" in captured
