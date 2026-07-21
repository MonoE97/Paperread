from __future__ import annotations

import json
import hashlib
import base64
import os
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from paper_reader.secondary_sources import SECONDARY_PLAN_MAX_BYTES


SCRIPT = Path("scripts/capture-secondary-url.mjs")
NETWORK_POLICY_NODE_TEST = Path("tests/js/test_secondary_network_policy.mjs")
EGRESS_PROXY_NODE_TEST = Path("tests/js/test_strict_egress_proxy.mjs")
RAW_CDP_NODE_TEST = Path("tests/js/test_raw_cdp_capture.mjs")


def test_python_producer_and_strict_capture_share_secondary_plan_byte_limit() -> None:
    assert SECONDARY_PLAN_MAX_BYTES == 2 * 1024 * 1024
    assert (
        "const SECONDARY_PLAN_MAX_BYTES = 2 * 1024 * 1024;"
        in SCRIPT.read_text(encoding="utf-8")
    )


class MockCdpServer(ThreadingHTTPServer):
    eval_count: int
    new_count: int
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
            self.server.new_count += 1
            if self.server.mode == "hang_new":
                time.sleep(5)
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
        if self.server.mode == "chrome_error_403":
            data = {
                "title": "mp.weixin.qq.com",
                "description": "",
                "finalUrl": "chrome-error://chromewebdata/",
                "readyState": "complete",
                "text": "访问 mp.weixin.qq.com 的请求遭到拒绝\nHTTP ERROR 403\n您未获授权，无法查看此网页。",
            }
            self._send_json({"value": json.dumps(data)})
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
            final_url = "https://mp.weixin.qq.com/s/delayed"
            text = "正文已经加载出来，用于交叉检查。" * 20
            if self.server.mode in {"strict_delayed", "oversized", "emoji"}:
                final_url = "https://93.184.216.34/final"
            if self.server.mode == "private_final":
                final_url = "http://127.0.0.1/private"
            if self.server.mode == "oversized":
                text = "文" * 100_001
            if self.server.mode == "emoji":
                text = "中😀e\u0301" * 60
            data = {
                "title": "Delayed WeChat Article",
                "description": "Delayed description",
                "publisher": "能源学人",
                "publishedAt": "2026-07-15",
                "finalUrl": final_url,
                "readyState": "complete",
                "text": text,
            }
        self._send_json({"value": json.dumps(data)})


class MockRawCdpServer:
    def __init__(self, *, mode: str = "captured") -> None:
        self.mode = mode
        self.commands: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.endpoint = f"ws://127.0.0.1:{self.port}/devtools/browser/test"
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @staticmethod
    def _recv_exact(connection: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = connection.recv(remaining)
            if not chunk:
                raise ConnectionError("websocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @classmethod
    def _recv_frame(cls, connection: socket.socket) -> tuple[int, bytes]:
        first, second = cls._recv_exact(connection, 2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", cls._recv_exact(connection, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", cls._recv_exact(connection, 8))[0]
        if length > 2 * 1024 * 1024:
            raise ValueError("oversized websocket frame")
        mask = cls._recv_exact(connection, 4) if masked else b""
        payload = cls._recv_exact(connection, length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload

    @staticmethod
    def _send_frame(connection: socket.socket, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes([first, length])
        elif length <= 0xFFFF:
            header = bytes([first, 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 127]) + struct.pack("!Q", length)
        connection.sendall(header + payload)

    @classmethod
    def _send_json(cls, connection: socket.socket, payload: dict[str, Any]) -> None:
        cls._send_frame(
            connection,
            0x1,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    @staticmethod
    def _read_handshake(connection: socket.socket) -> dict[str, str]:
        received = bytearray()
        while b"\r\n\r\n" not in received:
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake closed")
            received.extend(chunk)
            if len(received) > 64 * 1024:
                raise ValueError("oversized websocket handshake")
        lines = received.decode("latin-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        return headers

    def _respond(
        self,
        connection: socket.socket,
        command: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        response: dict[str, Any] = {"id": command["id"], "result": result or {}}
        if "sessionId" in command:
            response["sessionId"] = command["sessionId"]
        self._send_json(connection, response)

    @staticmethod
    def _send_blocked_background_connect(proxy_server: str) -> None:
        parsed = proxy_server.removeprefix("http://")
        host, raw_port = parsed.rsplit(":", 1)
        with socket.create_connection((host, int(raw_port)), timeout=2) as connection:
            connection.sendall(
                b"CONNECT www.google.com:443 HTTP/1.1\r\n"
                b"Host: www.google.com:443\r\n\r\n"
            )
            response = connection.recv(4096)
        if not response.startswith(b"HTTP/1.1 403"):
            raise AssertionError(f"unexpected proxy response: {response!r}")

    def _run(self) -> None:
        connection: socket.socket | None = None
        pending_navigate: dict[str, Any] | None = None
        continued_initial_request = False
        try:
            while not self._closed.is_set():
                try:
                    connection, _ = self._listener.accept()
                    break
                except TimeoutError:
                    continue
            if connection is None:
                return
            connection.settimeout(5)
            headers = self._read_handshake(connection)
            key = headers["sec-websocket-key"]
            accept = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()
            ).decode("ascii")
            connection.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
            )
            while not self._closed.is_set():
                opcode, payload = self._recv_frame(connection)
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    self._send_frame(connection, 0xA, payload)
                    continue
                if opcode != 0x1:
                    continue
                command = json.loads(payload.decode("utf-8"))
                self.commands.append(command)
                method = command["method"]
                if method == "Target.createBrowserContext":
                    if self.mode == "timeout_background_connect":
                        self._send_blocked_background_connect(
                            command["params"]["proxyServer"]
                        )
                    self._respond(connection, command, {"browserContextId": "context-1"})
                elif method == "Target.createTarget":
                    if self.mode != "hang_create_target":
                        self._respond(connection, command, {"targetId": "target-1"})
                elif method == "Target.attachToTarget":
                    self._respond(connection, command, {"sessionId": "session-1"})
                elif method == "Page.navigate":
                    pending_navigate = command
                    self._send_json(
                        connection,
                        {
                            "method": "Network.requestWillBeSent",
                            "sessionId": "session-1",
                            "params": {
                                "requestId": "network-initial",
                                "request": {
                                    "url": command["params"]["url"],
                                    "method": "GET",
                                    "headers": {},
                                },
                            },
                        },
                    )
                    self._send_json(
                        connection,
                        {
                            "method": "Fetch.requestPaused",
                            "sessionId": "session-1",
                            "params": {
                                "requestId": "fetch-initial",
                                "request": {
                                    "url": command["params"]["url"],
                                    "method": "GET",
                                    "headers": {},
                                },
                                "frameId": "frame-1",
                                "resourceType": "Document",
                                "networkId": "network-initial",
                            },
                        },
                    )
                elif method == "Fetch.continueRequest":
                    self._respond(connection, command)
                    if pending_navigate is not None:
                        if not continued_initial_request:
                            continued_initial_request = True
                            if self.mode == "invalid_fetch_url":
                                self._send_json(
                                    connection,
                                    {
                                        "method": "Fetch.requestPaused",
                                        "sessionId": "session-1",
                                        "params": {
                                            "requestId": "fetch-invalid-url",
                                            "request": {
                                                "url": (
                                                    "http://[paper-reader-secret.example/"
                                                    "private?token=do-not-log"
                                                ),
                                                "method": "GET",
                                                "headers": {},
                                            },
                                            "frameId": "frame-1",
                                            "resourceType": "Document",
                                            "networkId": "network-invalid-url",
                                        },
                                    },
                                )
                                continue
                            redirect_url = (
                                "http://127.0.0.1/private"
                                if self.mode == "private_redirect"
                                else "https://93.184.216.34/final"
                            )
                            self._send_json(
                                connection,
                                {
                                    "method": "Network.requestWillBeSent",
                                    "sessionId": "session-1",
                                    "params": {
                                        "requestId": "network-final",
                                        "request": {
                                            "url": redirect_url,
                                            "method": "GET",
                                            "headers": {},
                                        },
                                    },
                                },
                            )
                            self._send_json(
                                connection,
                                {
                                    "method": "Fetch.requestPaused",
                                    "sessionId": "session-1",
                                    "params": {
                                        "requestId": "fetch-final",
                                        "request": {
                                            "url": redirect_url,
                                            "method": "GET",
                                            "headers": {},
                                        },
                                        "frameId": "frame-1",
                                        "resourceType": "Document",
                                        "networkId": "network-final",
                                        "redirectedRequestId": "fetch-initial",
                                    },
                                },
                            )
                        else:
                            self._respond(
                                connection,
                                pending_navigate,
                                {"frameId": "frame-1", "loaderId": "loader-1"},
                            )
                            pending_navigate = None
                elif method == "Fetch.failRequest":
                    self._respond(connection, command)
                    if pending_navigate is not None:
                        self._respond(
                            connection,
                            pending_navigate,
                            {"frameId": "frame-1", "loaderId": "loader-1"},
                        )
                        pending_navigate = None
                elif method == "Runtime.evaluate":
                    if "__paperReaderStrictNetworkGuard" in command["params"]["expression"]:
                        self._respond(
                            connection,
                            command,
                            {"result": {"type": "boolean", "value": True}},
                        )
                        continue
                    text = "正文已经加载出来，用于交叉检查。" * 20
                    final_url = "https://93.184.216.34/final"
                    ready_state = "complete"
                    if self.mode in {"timeout", "timeout_background_connect"}:
                        text = ""
                        final_url = "about:blank"
                    elif self.mode == "oversized":
                        text = "文" * 100_001
                    elif self.mode == "emoji":
                        text = "中😀e\u0301" * 60
                    elif self.mode == "unicode_controls":
                        text = "\udc00正文\x00\u202e" + "用于交叉核对的正文。" * 30
                    data = {
                        "title": (
                            ""
                            if self.mode == "missing_title"
                            else (
                                "\ud800外部\x1b\u202e解读"
                                if self.mode == "unicode_controls"
                                else ("Delayed WeChat Article" if text else "")
                            )
                        ),
                        "description": "Delayed description" if text else "",
                        "publisher": "能源学人",
                        "publishedAt": "2026-07-15",
                        "finalUrl": final_url,
                        "readyState": ready_state,
                        "text": text,
                    }
                    self._respond(
                        connection,
                        command,
                        {"result": {"type": "string", "value": json.dumps(data)}},
                    )
                else:
                    self._respond(connection, command)
        except (ConnectionError, OSError, TimeoutError):
            if not self._closed.is_set():
                self.errors.append(ConnectionError("raw CDP server connection failed"))
        except BaseException as error:  # noqa: BLE001 - test server must report thread failures
            self.errors.append(error)
        finally:
            if connection is not None:
                connection.close()

    def close(self) -> None:
        self._closed.set()
        self._listener.close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("raw CDP server did not stop")
        if self.errors:
            raise self.errors[0]


def test_secondary_network_policy_node_suite() -> None:
    result = subprocess.run(
        ["node", "--test", str(NETWORK_POLICY_NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_strict_egress_proxy_node_suite() -> None:
    result = subprocess.run(
        ["node", "--test", str(EGRESS_PROXY_NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_raw_cdp_capture_node_suite() -> None:
    result = subprocess.run(
        ["node", "--test", str(RAW_CDP_NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def run_mock_cdp(mode: str = "delayed") -> tuple[MockCdpServer, str]:
    server = MockCdpServer(("127.0.0.1", 0), MockCdpHandler)
    server.eval_count = 0
    server.new_count = 0
    server.mode = mode
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def run_capture(tmp_path: Path, base_url: str, *extra_args: str) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "secondary_context.md"
    env = os.environ.copy()
    env["ZOTERO_PAPER_READER_CDP_BASE_URL"] = base_url
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


def _write_plan(
    tmp_path: Path,
    *,
    source_id: str = "secondary-001",
    source_url: str = "https://93.184.216.34/article?scene=334",
) -> Path:
    run_dir = tmp_path / "run"
    source_dir = run_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    normalized_source = b"{}"
    normalized_source_sha256 = hashlib.sha256(normalized_source).hexdigest()
    (source_dir / "source.json").write_bytes(normalized_source)
    plan = {
        "format": "paper_reader.secondary-plan.v2-internal",
        "item_key": "PARENT1",
        "source_snapshot_sha256": normalized_source_sha256,
        "usage_boundary": "cross-check only; must not be cited in evidence_summary",
        "eligible_source_count": 1,
        "sources": [
            {
                "source_id": source_id,
                "url": source_url,
                "source_field": "extra",
                "source_provenance": "zotero_parent_snapshot",
                "eligibility": "eligible",
                "rejection_reason": None,
            }
        ],
        "warnings": [],
    }
    path = source_dir / "secondary-plan.json"
    plan_bytes = json.dumps(plan, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path.write_bytes(plan_bytes)
    run = {
        "schema_version": "paper_reader.run.v2",
        "run_id": "run_capture_test",
        "source": {
            "source_type": "zotero",
            "item_key": "PARENT1",
            "normalized_source": {
                "role": "normalized_source",
                "path": "source/source.json",
                "sha256": normalized_source_sha256,
                "size_bytes": len(normalized_source),
                "media_type": "application/json",
            },
        },
        "artifacts": [
            {
                "role": "secondary_source_plan",
                "path": "source/secondary-plan.json",
                "sha256": hashlib.sha256(plan_bytes).hexdigest(),
                "size_bytes": len(plan_bytes),
                "media_type": "application/json",
            }
        ],
    }
    (run_dir / "run.json").write_text(
        json.dumps(run, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def _rewrite_plan_binding(plan_path: Path, plan: dict[str, object]) -> None:
    plan_bytes = json.dumps(plan, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _rewrite_plan_raw_binding(plan_path, plan_bytes)


def _rewrite_plan_raw_binding(plan_path: Path, plan_bytes: bytes) -> None:
    plan_path.write_bytes(plan_bytes)
    run_path = plan_path.parent.parent / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    plan_ref = next(
        item for item in run["artifacts"] if item["role"] == "secondary_source_plan"
    )
    plan_ref["sha256"] = hashlib.sha256(plan_bytes).hexdigest()
    plan_ref["size_bytes"] = len(plan_bytes)
    run_path.write_text(
        json.dumps(run, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


def run_strict_capture(
    tmp_path: Path,
    base_url: str,
    *,
    source_id: str = "secondary-001",
    output: Path | None = None,
    plan_path: Path | None = None,
    extra_args: tuple[str, ...] = (),
    env_extra: dict[str, str] | None = None,
    subprocess_timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    resolved_plan_path = plan_path or _write_plan(tmp_path)
    output_path = output or (tmp_path / "secondary-001.json")
    env = os.environ.copy()
    env["ZOTERO_PAPER_READER_CDP_BASE_URL"] = base_url
    if env_extra:
        env.update(env_extra)
    timing_args: list[str] = []
    if "--timeout-ms" not in extra_args:
        timing_args.extend(("--timeout-ms", "2000"))
    if "--poll-ms" not in extra_args:
        timing_args.extend(("--poll-ms", "10"))
    return subprocess.run(
        [
            "node",
            str(SCRIPT),
            "--plan",
            str(resolved_plan_path),
            "--source-id",
            source_id,
            "--output",
            str(output_path),
            *timing_args,
            *extra_args,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=subprocess_timeout,
    )


def assert_strict_machine_error(
    result: subprocess.CompletedProcess[str],
    warning: str = "capture_setup_failed",
) -> None:
    command_result = json.loads(result.stdout)
    assert command_result["status"] == "error"
    assert command_result["finalUrl"] == "about:blank"
    assert command_result["textLength"] == 0
    assert command_result["warnings"] == [warning]


def run_raw_strict_capture(
    tmp_path: Path,
    *,
    mode: str = "captured",
    env_extra: dict[str, str] | None = None,
    **kwargs: Any,
) -> tuple[subprocess.CompletedProcess[str], MockRawCdpServer]:
    raw_cdp = MockRawCdpServer(mode=mode)
    merged_env = dict(env_extra or {})
    merged_env["ZOTERO_PAPER_READER_CDP_WS_ENDPOINT"] = raw_cdp.endpoint
    try:
        result = run_strict_capture(
            tmp_path,
            "http://127.0.0.1:1",
            env_extra=merged_env,
            **kwargs,
        )
    finally:
        raw_cdp.close()
    return result, raw_cdp


def _write_doh_fetch_preload(tmp_path: Path, *, address: str) -> Path:
    preload = tmp_path / "mock-doh-fetch.mjs"
    preload.write_text(
        f"""
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, options) => {{
  const requested = String(input);
  if (requested.startsWith("https://cloudflare-dns.com/dns-query?")) {{
    throw new Error("hostname-based DoH egress is forbidden");
  }}
  if (requested.startsWith("https://1.1.1.1/dns-query?")) {{
    const queryType = new URL(requested).searchParams.get("type");
    const answer = queryType === "A"
      ? [{{ name: "public.example.", type: 1, data: {json.dumps(address)} }}]
      : [];
    return new Response(JSON.stringify({{ Status: 0, Answer: answer }}), {{
      status: 200,
      headers: {{ "content-type": "application/dns-json" }},
    }});
  }}
  return originalFetch(input, options);
}};
""",
        encoding="utf-8",
    )
    return preload


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


def test_capture_secondary_url_marks_chrome_error_403_unavailable(tmp_path: Path) -> None:
    server, base_url = run_mock_cdp(mode="chrome_error_403")
    try:
        result = run_capture(tmp_path, base_url, "--timeout-ms", "2000", "--poll-ms", "10")
    finally:
        server.shutdown()

    assert result.returncode == 1
    captured = (tmp_path / "secondary_context.md").read_text(encoding="utf-8")
    assert "source_status: secondary_context_unavailable" in captured
    assert "capture_warning: chrome_error_page" in captured
    assert "capture_warning: http_403_unauthorized" in captured
    assert "final_url: chrome-error://chromewebdata/" in captured


def test_plan_bound_capture_writes_strict_hashed_json_without_overwriting_template(
    tmp_path: Path,
) -> None:
    result, _ = run_raw_strict_capture(tmp_path)

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1
    command_result = json.loads(result.stdout)
    capture_path = tmp_path / "secondary-001.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    plan_path = tmp_path / "run" / "source" / "secondary-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert command_result["status"] == "captured"
    assert capture == {
        "format": "paper_reader.secondary-capture.v2-internal",
        "run_id": "run_capture_test",
        "item_key": "PARENT1",
        "source_snapshot_sha256": plan["source_snapshot_sha256"],
        "secondary_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "source_id": "secondary-001",
        "requested_url": "https://93.184.216.34/article?scene=334",
        "final_url": "https://93.184.216.34/final",
        "captured_at": capture["captured_at"],
        "capture_method": "chrome_cdp",
        "status": "captured",
        "title": "Delayed WeChat Article",
        "publisher": "能源学人",
        "published_at": "2026-07-15",
        "description": "Delayed description",
        "text": "正文已经加载出来，用于交叉检查。" * 20,
        "text_sha256": hashlib.sha256(
            ("正文已经加载出来，用于交叉检查。" * 20).encode("utf-8")
        ).hexdigest(),
        "text_length": len("正文已经加载出来，用于交叉检查。" * 20),
        "warnings": [],
    }


def test_plan_bound_capture_uses_raw_cdp_with_guards_before_navigation(
    tmp_path: Path,
) -> None:
    result, raw_cdp = run_raw_strict_capture(tmp_path)

    assert result.returncode == 0, result.stderr
    command_result = json.loads(result.stdout)
    assert command_result["warnings"] == [], command_result
    assert command_result["status"] == "captured", (
        command_result,
        result.stderr,
        raw_cdp.commands,
    )
    methods = [command["method"] for command in raw_cdp.commands]
    navigate_index = methods.index("Page.navigate")
    for required in (
        "Browser.setDownloadBehavior",
        "Fetch.enable",
        "Network.enable",
        "Network.setCacheDisabled",
        "Network.setBypassServiceWorker",
        "Network.setBlockedURLs",
        "Target.setAutoAttach",
    ):
        assert methods.index(required) < navigate_index
    assert methods[-2:] == ["Target.closeTarget", "Target.disposeBrowserContext"]


def test_plan_bound_capture_rejects_unknown_source_before_browser_navigation(
    tmp_path: Path,
) -> None:
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, source_id="secondary-999")
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert not (tmp_path / "secondary-001.json").exists()
    assert_strict_machine_error(result)


@pytest.mark.parametrize(
    "extra_args",
    [
        ("--timeout-ms", "invalid"),
        ("--timeout-ms", "60001"),
        ("--request-retries", "3"),
        ("--unexpected-option",),
        ("--source-id", "secondary-001"),
    ],
)
def test_plan_bound_capture_rejects_ambiguous_cli_options_before_browser(
    extra_args: tuple[str, ...],
    tmp_path: Path,
) -> None:
    result = run_strict_capture(
        tmp_path,
        "http://127.0.0.1:1",
        extra_args=extra_args,
        env_extra={
            "ZOTERO_PAPER_READER_CDP_WS_ENDPOINT": (
                "ws://127.0.0.1:1/devtools/browser/unreachable"
            )
        },
    )

    assert result.returncode == 2
    assert_strict_machine_error(result, "invalid_arguments")
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_rejects_non_strict_plan_before_browser_navigation(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["unexpected"] = True
    plan["sources"][0]["also_unexpected"] = True
    plan["eligible_source_count"] = 2
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, plan_path=plan_path)
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_rejects_invalid_v2_identity_before_browser_navigation(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    run_path = plan_path.parent.parent / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["run_id"] = "invalid run id"
    run_path.write_text(
        json.dumps(run, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, plan_path=plan_path)
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_accepts_largest_producer_bounded_plan(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    sources = []
    for index in range(1, 257):
        prefix = f"https://93.184.216.34/{index:03d}/"
        url = prefix + ("a" * (4000 - len(prefix)))
        eligible = index <= 8
        sources.append(
            {
                "source_id": f"secondary-{index:03d}",
                "url": url,
                "source_field": "extra",
                "source_provenance": "zotero_parent_snapshot",
                "eligibility": "eligible" if eligible else "rejected",
                "rejection_reason": None if eligible else "source_limit",
            }
        )
    plan["sources"] = sources
    plan["eligible_source_count"] = 8
    _rewrite_plan_binding(plan_path, plan)
    assert plan_path.stat().st_size > 1024 * 1024

    result, _ = run_raw_strict_capture(tmp_path, plan_path=plan_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "captured"


def test_plan_bound_capture_accepts_finding_anchor_policy(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["finding_anchor_policy"] = "codepoint_sha256_v1"
    _rewrite_plan_binding(plan_path, plan)

    result, _ = run_raw_strict_capture(tmp_path, plan_path=plan_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "captured"


@pytest.mark.parametrize(
    "case",
    [
        "misspelled_policy",
        "wrong_policy_type",
        "unknown_key",
        "missing_legacy_key",
    ],
)
def test_plan_bound_capture_rejects_invalid_policy_shape_before_browser(
    case: str,
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["finding_anchor_policy"] = "codepoint_sha256_v1"
    if case == "misspelled_policy":
        plan["finding_anchor_policy"] = "codepoints_sha256_v1"
    elif case == "wrong_policy_type":
        plan["finding_anchor_policy"] = True
    elif case == "unknown_key":
        plan["unexpected"] = "value"
    else:
        del plan["usage_boundary"]
    _rewrite_plan_binding(plan_path, plan)

    result = run_strict_capture(
        tmp_path,
        "http://127.0.0.1:1",
        plan_path=plan_path,
        env_extra={
            "ZOTERO_PAPER_READER_CDP_WS_ENDPOINT": (
                "ws://127.0.0.1:1/devtools/browser/unreachable"
            )
        },
    )

    assert result.returncode == 2
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_sources",
        "duplicate_policy",
        "invalid_utf8",
        "noncanonical_json",
        "lone_surrogate",
        "prototype_key",
        "constructor_key",
    ],
)
def test_plan_bound_capture_rejects_noncanonical_or_ambiguous_plan_bytes(
    case: str,
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if case == "duplicate_policy":
        plan["finding_anchor_policy"] = "codepoint_sha256_v1"
    elif case == "prototype_key":
        plan["__proto__"] = {"polluted": True}
    elif case == "constructor_key":
        plan["constructor"] = {"polluted": True}
    canonical = json.dumps(
        plan,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if case == "duplicate_sources":
        raw = canonical.replace(b'"sources":[', b'"sources":[],"sources":[', 1)
    elif case == "duplicate_policy":
        raw = canonical.replace(
            b'"finding_anchor_policy":"codepoint_sha256_v1"',
            (
                b'"finding_anchor_policy":"codepoint_sha256_v1",'
                b'"finding_anchor_policy":"codepoint_sha256_v1"'
            ),
            1,
        )
    elif case == "invalid_utf8":
        raw = canonical.replace(b'"warnings":[]', b'"warnings":["\xff"]', 1)
    elif case == "noncanonical_json":
        raw = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    elif case == "lone_surrogate":
        plan["warnings"] = ["\ud800"]
        raw = json.dumps(
            plan,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    else:
        raw = canonical
    _rewrite_plan_raw_binding(plan_path, raw)

    result = run_strict_capture(
        tmp_path,
        "http://127.0.0.1:1",
        plan_path=plan_path,
        env_extra={
            "ZOTERO_PAPER_READER_CDP_WS_ENDPOINT": (
                "ws://127.0.0.1:1/devtools/browser/unreachable"
            )
        },
    )

    assert result.returncode == 2
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_does_not_disclose_malformed_plan_text(
    tmp_path: Path,
) -> None:
    secret = "paper-reader-malformed-plan-secret"
    plan_path = _write_plan(tmp_path)
    _rewrite_plan_raw_binding(plan_path, secret.encode("utf-8"))

    result = run_strict_capture(
        tmp_path,
        "http://127.0.0.1:1",
        plan_path=plan_path,
        env_extra={
            "ZOTERO_PAPER_READER_CDP_WS_ENDPOINT": (
                "ws://127.0.0.1:1/devtools/browser/unreachable"
            )
        },
    )

    assert result.returncode == 2
    assert_strict_machine_error(result)
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert not (tmp_path / "secondary-001.json").exists()


@pytest.mark.parametrize("tamper", ["unsafe_nonselected", "duplicate_url"])
def test_plan_bound_capture_rejects_semantically_invalid_plan_before_browser_navigation(
    tamper: str,
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    second = dict(plan["sources"][0])
    second["source_id"] = "secondary-002"
    if tamper == "unsafe_nonselected":
        second["url"] = "http://127.0.0.1/private"
    plan["sources"].append(second)
    plan["eligible_source_count"] = 2
    _rewrite_plan_binding(plan_path, plan)

    result = run_strict_capture(
        tmp_path,
        "http://127.0.0.1:1",
        plan_path=plan_path,
        env_extra={
            "ZOTERO_PAPER_READER_CDP_WS_ENDPOINT": (
                "ws://127.0.0.1:1/devtools/browser/unreachable"
            )
        },
    )

    assert result.returncode == 2
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_rejects_hash_valid_json_tamper_before_navigation(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["sources"][0]["url"] = "https://93.184.216.35/replaced"
    plan_path.write_text(
        json.dumps(plan, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, plan_path=plan_path)
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_rejects_source_snapshot_drift_before_navigation(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    (plan_path.parent / "source.json").write_text('{"changed":true}', encoding="utf-8")
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, plan_path=plan_path)
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


@pytest.mark.parametrize(
    "source_url",
    [
        "http://100.64.0.1/context",
        "http://198.18.0.1/context",
        "http://203.0.113.1/context",
        "http://224.0.0.1/context",
        "http://2130706433/context",
        "http://0x7f000001/context",
        "http://127.1/context",
    ],
)
def test_plan_bound_capture_rejects_non_global_ipv4_before_browser_navigation(
    source_url: str,
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path, source_url=source_url)
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, plan_path=plan_path)
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_accepts_public_ipv6_literal(tmp_path: Path) -> None:
    source_url = "https://[2606:4700:4700::1111]/context"
    plan_path = _write_plan(tmp_path, source_url=source_url)
    result, raw_cdp = run_raw_strict_capture(tmp_path, plan_path=plan_path)

    assert result.returncode == 0, result.stderr
    navigations = [
        command
        for command in raw_cdp.commands
        if command["method"] == "Page.navigate"
    ]
    assert [command["params"]["url"] for command in navigations] == [source_url]
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["requested_url"] == source_url


def test_plan_bound_capture_supports_explicit_public_dns_over_https(
    tmp_path: Path,
) -> None:
    source_url = "https://public.example/context"
    plan_path = _write_plan(tmp_path, source_url=source_url)
    preload = _write_doh_fetch_preload(tmp_path, address="93.184.216.34")
    result, raw_cdp = run_raw_strict_capture(
        tmp_path,
        plan_path=plan_path,
        extra_args=("--public-dns-over-https",),
        env_extra={"NODE_OPTIONS": f"--import={preload}"},
    )

    assert result.returncode == 0, result.stderr
    assert any(command["method"] == "Page.navigate" for command in raw_cdp.commands)


def test_plan_bound_capture_doh_mode_rejects_non_public_answer_before_navigation(
    tmp_path: Path,
) -> None:
    source_url = "https://public.example/context"
    plan_path = _write_plan(tmp_path, source_url=source_url)
    preload = _write_doh_fetch_preload(tmp_path, address="127.0.0.1")
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(
            tmp_path,
            base_url,
            plan_path=plan_path,
            extra_args=("--public-dns-over-https",),
            env_extra={"NODE_OPTIONS": f"--import={preload}"},
        )
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


@pytest.mark.parametrize(
    "source_url",
    [
        "http://[::1]/context",
        "http://[::ffff:127.0.0.1]/context",
        "http://[2001:db8::1]/context",
        "http://[ff02::1]/context",
    ],
)
def test_plan_bound_capture_rejects_non_global_ipv6_before_browser_navigation(
    source_url: str,
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path, source_url=source_url)
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, plan_path=plan_path)
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert_strict_machine_error(result)
    assert not (tmp_path / "secondary-001.json").exists()


def test_plan_bound_capture_uses_no_replace_output(tmp_path: Path) -> None:
    output = tmp_path / "secondary-001.json"
    output.write_text("sentinel", encoding="utf-8")
    server, base_url = run_mock_cdp(mode="strict_delayed")
    try:
        result = run_strict_capture(tmp_path, base_url, output=output)
    finally:
        server.shutdown()

    assert result.returncode == 2
    assert server.new_count == 0
    assert output.read_text(encoding="utf-8") == "sentinel"
    assert_strict_machine_error(result)


def test_plan_bound_capture_records_expected_timeout_as_unavailable(tmp_path: Path) -> None:
    result, _ = run_raw_strict_capture(
        tmp_path,
        mode="timeout",
        extra_args=("--timeout-ms", "200"),
    )

    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["status"] == "unavailable"
    assert capture["text"] == ""
    assert capture["text_sha256"] == hashlib.sha256(b"").hexdigest()
    assert "navigation_timeout" in capture["warnings"]


def test_plan_bound_capture_preserves_timeout_alongside_proxy_audit_warning(
    tmp_path: Path,
) -> None:
    result, _ = run_raw_strict_capture(
        tmp_path,
        mode="timeout_background_connect",
        extra_args=("--timeout-ms", "200"),
    )

    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["status"] == "unavailable"
    assert "navigation_timeout" in capture["warnings"]
    assert "browser_background_connect_blocked" in capture["warnings"]


def test_plan_bound_capture_marks_private_final_url_unavailable(tmp_path: Path) -> None:
    result, raw_cdp = run_raw_strict_capture(tmp_path, mode="private_redirect")

    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["status"] == "unavailable"
    assert "unsafe_request_blocked" in capture["warnings"]
    failed = [
        command
        for command in raw_cdp.commands
        if command["method"] == "Fetch.failRequest"
    ]
    assert [command["params"]["requestId"] for command in failed] == ["fetch-final"]


def test_plan_bound_capture_rejects_oversized_visible_text(tmp_path: Path) -> None:
    result, _ = run_raw_strict_capture(tmp_path, mode="oversized")

    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["status"] == "unavailable"
    assert capture["text"] == ""
    assert "text_resource_limit" in capture["warnings"]


def test_plan_bound_capture_marks_missing_title_unavailable(tmp_path: Path) -> None:
    result, _ = run_raw_strict_capture(tmp_path, mode="missing_title")

    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["status"] == "unavailable"
    assert capture["text"] == ""
    assert "missing_title" in capture["warnings"]


def test_plan_bound_capture_counts_unicode_code_points(tmp_path: Path) -> None:
    result, _ = run_raw_strict_capture(tmp_path, mode="emoji")

    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["status"] == "captured"
    assert capture["text"] == "中😀e\u0301" * 60
    assert capture["text_length"] == len("中😀e\u0301" * 60)


def test_plan_bound_capture_redacts_invalid_fetch_url_from_all_outputs(
    tmp_path: Path,
) -> None:
    secret = "paper-reader-secret.example/private?token=do-not-log"

    result, raw_cdp = run_raw_strict_capture(tmp_path, mode="invalid_fetch_url")

    assert result.returncode == 0, result.stderr
    artifact_bytes = (tmp_path / "secondary-001.json").read_bytes()
    capture = json.loads(artifact_bytes)
    assert capture["status"] == "unavailable"
    assert capture["text"] == ""
    assert "invalid_network_request_url" in capture["warnings"]
    assert any(
        warning.startswith(
            "invalid_network_request_url:checkpoint=fetch_request_paused;"
        )
        for warning in capture["warnings"]
    )
    assert secret.encode("utf-8") not in artifact_bytes
    assert secret not in result.stdout
    assert secret not in result.stderr
    failed = [
        command
        for command in raw_cdp.commands
        if command["method"] == "Fetch.failRequest"
    ]
    assert [command["params"]["requestId"] for command in failed] == [
        "fetch-invalid-url"
    ]


def test_plan_bound_capture_normalizes_untrusted_unicode_and_control_characters(
    tmp_path: Path,
) -> None:
    result, _ = run_raw_strict_capture(tmp_path, mode="unicode_controls")

    assert result.returncode == 0, result.stderr
    raw = (tmp_path / "secondary-001.json").read_text(encoding="utf-8")
    capture = json.loads(raw)
    combined = "\n".join((capture["title"], capture["text"]))
    assert "\ufffd" in combined
    assert "\x00" not in combined
    assert "\x1b" not in combined
    assert "\u202e" not in combined
    assert not any(0xD800 <= ord(character) <= 0xDFFF for character in combined)


def test_plan_bound_capture_bounds_a_hung_new_target_request(tmp_path: Path) -> None:
    result, _ = run_raw_strict_capture(
        tmp_path,
        mode="hang_create_target",
        extra_args=("--timeout-ms", "80"),
        subprocess_timeout=2,
    )

    assert result.returncode == 0, result.stderr
    capture = json.loads((tmp_path / "secondary-001.json").read_text(encoding="utf-8"))
    assert capture["status"] == "unavailable"
    assert any("timeout" in warning for warning in capture["warnings"])
