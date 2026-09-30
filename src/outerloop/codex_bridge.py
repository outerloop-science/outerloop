"""Private per-session Responses → LiteLLM → SSE shim → Chat bridge.

Standalone entry point: runs under the locked bridge interpreter in the same
container as Codex. No kernel imports or runtime package installation.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def end_to_end(headers: Message) -> dict[str, str]:
    named = ",".join(headers.get_all("Connection") or [])
    drop = HOP | {t.strip().lower() for t in named.split(",")}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def is_opener(event: dict[str, Any]) -> tuple[int, str] | None:
    choices = event.get("choices") or []
    if len(choices) != 1 or event.get("usage"):
        return None
    c = choices[0]
    delta = c.get("delta") or {}
    if (
        c.get("finish_reason")
        or not delta.get("role")
        or set(delta) - {"role", "content"}
        or delta.get("content")
    ):
        return None
    return c.get("index", 0), delta["role"]


class Events:
    """Operate on whole SSE events, keeping opener state separately per choice."""

    def __init__(self) -> None:
        self.held: dict[int, str] = {}
        self.opened: set[int] = set()
        self.done = False

    def emit(self, lines: list[bytes]) -> bytes:
        data = [
            ln[5:].removeprefix(b" ").rstrip(b"\r\n") for ln in lines if ln.startswith(b"data:")
        ]
        other = [ln for ln in lines if ln.strip() and not ln.startswith(b"data:")]
        if b"\n".join(data) == b"[DONE]":
            self.done = True
        try:
            event = json.loads(b"\n".join(data))
        except ValueError:
            event = None
        if isinstance(event, dict):
            op = is_opener(event)
            if op and op[0] not in self.opened:
                self.opened.add(op[0])
                self.held[op[0]] = op[1]
                return b""
            moved = False
            for c in event.get("choices") or []:
                index = c.get("index", 0)
                # A meaningful first chunk also closes the opener window.
                self.opened.add(index)
                if index in self.held:
                    c["delta"] = {"role": self.held.pop(index), **(c.get("delta") or {})}
                    moved = True
            if moved:
                return b"".join(other) + b"data: " + json.dumps(event).encode() + b"\n\n"
        return b"".join(lines)


class Shim(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            parts: list[bytes] = []
            while True:
                line = self.rfile.readline(8192)
                if not line:
                    raise ValueError("truncated body")
                size = int(line.split(b";")[0].strip(), 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(8192)
                        if not trailer:
                            raise ValueError("truncated trailers")
                        if trailer in (b"\r\n", b"\n"):
                            return b"".join(parts)
                if size < 0 or size > 64 * 1024 * 1024:
                    raise ValueError("invalid body size")
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("truncated chunk")
                parts.append(chunk)
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 64 * 1024 * 1024:
            raise ValueError("invalid body size")
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError("truncated body")
        return body

    def do_GET(self) -> None:
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def forward(self) -> None:
        self.close_connection = True
        if not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + os.environ["BRIDGE_SHIM_KEY"]
        ):
            self.send_error(401)
            return
        if self.path == "/health" and self.command == "GET":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path != "/v1/chat/completions" or self.command != "POST":
            self.send_error(404)
            return
        started = False
        try:
            headers = end_to_end(self.headers)
            headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            headers["Authorization"] = "Bearer " + os.environ["OUTERLOOP_SESSION_KEY"]
            base = os.environ["BRIDGE_UPSTREAM"].rstrip("/")
            url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
            request = urllib.request.Request(url, data=self.body(), headers=headers)
            # Never forward credentials to a redirect destination or ambient HTTP proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=300) as response:
                self.send_response(response.status)
                for key, value in end_to_end(response.headers).items():
                    self.send_header(key, value)
                sse = "text/event-stream" in response.headers.get("Content-Type", "")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                started = True
                if sse:
                    events = Events()
                    lines: list[bytes] = []
                    for raw in response:
                        lines.append(raw)
                        if raw.strip():
                            continue
                        self.chunk(events.emit(lines))
                        lines = []
                    # No terminating HTTP chunk on truncation: LiteLLM must see a transport error.
                    if lines or not events.done:
                        return
                else:
                    while data := response.read(65536):
                        self.chunk(data)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (OSError, ValueError):
            if not started:
                self.send_error(502, "upstream unavailable")

    def chunk(self, data: bytes) -> None:
        if data:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def listener() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def child_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("HOME", "PATH", "TERM", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")
    }
    env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def stop(children: list[subprocess.Popen[Any]], *, groups: bool = False) -> None:
    for child in reversed(children):
        if groups:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
        elif child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 3
    for child in reversed(children):
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    if groups:
        for child in children:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)


def wait_ready(
    url: str, key: str, children: list[subprocess.Popen[Any]], deadline: float | None = None
) -> None:
    deadline = min(deadline or float("inf"), time.monotonic() + 120)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if any(child.poll() is not None for child in children):
            raise RuntimeError("bridge child exited")
        try:
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with opener.open(request, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("bridge readiness timed out")


def supervise(upstream: str, model: str, timeout: float, command: list[str]) -> int:
    children: list[subprocess.Popen[Any]] = []
    deadline = time.monotonic() + timeout

    def cancelled(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, cancelled)
    shim_key, proxy_key = ("sk-" + secrets.token_hex(32) for _ in range(2))
    script = str(Path(__file__).resolve())
    with (
        listener() as shim,
        listener() as proxy,
        tempfile.TemporaryDirectory(prefix="outerloop-bridge-") as temporary,
    ):
        try:
            shim_url = f"http://127.0.0.1:{shim.getsockname()[1]}"
            proxy_url = f"http://127.0.0.1:{proxy.getsockname()[1]}/v1"
            env = child_env()
            env.update(
                BRIDGE_SHIM_KEY=shim_key,
                OUTERLOOP_SESSION_KEY=os.environ.pop("OUTERLOOP_SESSION_KEY"),
                BRIDGE_UPSTREAM=upstream,
            )
            children.append(
                subprocess.Popen(
                    [sys.executable, script, "shim", str(shim.fileno())],
                    env=env,
                    pass_fds=(shim.fileno(),),
                    process_group=0,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            shim.close()
            wait_ready(shim_url + "/health", shim_key, children, deadline)
            config = Path(temporary) / "litellm.yaml"
            config.write_text(
                json.dumps(
                    {
                        "model_list": [
                            {
                                "model_name": model,
                                "litellm_params": {
                                    "model": "openai/" + model,
                                    "api_base": shim_url + "/v1",
                                    "api_key": "os.environ/BRIDGE_SHIM_KEY",
                                    "use_chat_completions_api": True,
                                },
                            }
                        ],
                        "general_settings": {
                            "master_key": "os.environ/LITELLM_MASTER_KEY",
                            "disable_spend_logs": True,
                        },
                        "litellm_settings": {
                            "drop_params": True,
                            "telemetry": False,
                            "disable_hf_tokenizer_download": True,
                            "set_verbose": False,
                            "turn_off_message_logging": True,
                        },
                    }
                )
            )
            env = child_env()
            env.update(
                BRIDGE_SHIM_KEY=shim_key,
                LITELLM_MASTER_KEY=proxy_key,
                CONFIG_FILE_PATH=str(config),
                LITELLM_LOCAL_MODEL_COST_MAP="True",
                LITELLM_LOG="ERROR",
                DO_NOT_TRACK="1",
                OPENBLAS_NUM_THREADS="1",
                OMP_NUM_THREADS="1",
                TOKENIZERS_PARALLELISM="false",
            )
            children.append(
                subprocess.Popen(
                    [sys.executable, script, "proxy", str(proxy.fileno())],
                    env=env,
                    pass_fds=(proxy.fileno(),),
                    process_group=0,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            proxy.close()
            wait_ready(proxy_url + "/models", proxy_key, children, deadline)
            env = child_env()
            env["OUTERLOOP_SESSION_KEY"] = proxy_key
            command += [
                "-c",
                f"model_providers.outerloop_endpoint.base_url={json.dumps(proxy_url)}",
                "-c",
                'model_reasoning_summary="detailed"',
                "-c",
                'web_search="disabled"',
                "-c",
                "features.apply_patch_freeform=true",
                "-c",
                "model_supports_reasoning_summaries=true",
                "-c",
                "model_providers.outerloop_endpoint.supports_websockets=false",
                "-c",
                "features.remote_compaction_v2=false",
                "-c",
                "analytics.enabled=false",
                "-c",
                "check_for_update_on_startup=false",
            ]
            children.append(subprocess.Popen(command, env=env, process_group=0))
            while children[-1].poll() is None:
                if any(child.poll() is not None for child in children[:-1]):
                    raise RuntimeError("bridge child exited")
                if time.monotonic() >= deadline:
                    raise RuntimeError("bridge session timed out")
                time.sleep(0.05)
            return children[-1].returncode
        except (OSError, RuntimeError, KeyboardInterrupt):
            print("codex bridge unavailable or interrupted", file=sys.stderr)
            return 1
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, signal.SIG_IGN)
            stop(children, groups=True)


class ProtocolGuard:
    """Bound the proxy surface to stateless full-history text/tool Responses."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        authorization = dict(scope.get("headers", [])).get(b"authorization", b"")
        expected = ("Bearer " + os.environ["LITELLM_MASTER_KEY"]).encode()
        if not hmac.compare_digest(authorization, expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {"type": "http.response.body", "body": b'{"error":{"message":"unauthorized"}}'}
            )
            return
        path = scope["path"]
        if path == "/v1/models" and scope["method"] == "GET":
            await self.app(scope, receive, send)
            return
        payload = b""
        valid = path in ("/responses", "/v1/responses") and scope["method"] == "POST"
        if valid:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                payload += message.get("body", b"")
                if len(payload) > 64 * 1024 * 1024:
                    valid = False
                    break
                if not message.get("more_body"):
                    break
            try:
                data = json.loads(payload)
                valid = valid and supported_request(data)
            except (ValueError, TypeError, AttributeError):
                valid = False
        if not valid:
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":{"message":"unsupported bridge request"}}',
                }
            )
            return
        delivered = False

        async def replay() -> Any:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def supported_request(data: dict[str, Any]) -> bool:
    if data.get("previous_response_id") or data.get("conversation") or data.get("background"):
        return False
    if data.get("text", {}).get("format", {}).get("type", "text") != "text":
        return False
    if any(t.get("type") not in ("function", "custom") for t in data.get("tools", [])):
        return False
    history = data.get("input", [])
    if isinstance(history, str):
        return True
    for item in history:
        if item.get("type", "message") not in (
            "message",
            "reasoning",
            "function_call",
            "function_call_output",
            "custom_tool_call",
            "custom_tool_call_output",
        ):
            return False
        for field in ("content", "output"):
            content = item.get(field, [])
            if isinstance(content, list) and any(
                part.get("type")
                not in ("input_text", "output_text", "reasoning_text", "summary_text")
                for part in content
            ):
                return False
    return True


def main() -> int:
    mode = sys.argv[1]
    if mode == "shim":
        server = ThreadingHTTPServer(("127.0.0.1", 0), Shim, bind_and_activate=False)
        server.socket.close()
        server.socket = socket.socket(fileno=int(sys.argv[2]))
        server.server_address = server.socket.getsockname()
        server.serve_forever()
    elif mode == "proxy":
        import litellm
        import uvicorn

        litellm.telemetry = False
        from litellm.proxy import proxy_server

        proxy_server.user_telemetry = False
        uvicorn.run(
            ProtocolGuard(proxy_server.app),
            fd=int(sys.argv[2]),
            log_level="critical",
            access_log=False,
        )
    else:
        return supervise(sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5:])
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
