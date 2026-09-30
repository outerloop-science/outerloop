"""Offline shim and locked-LiteLLM compatibility tests.

Set OUTERLOOP_BRIDGE_TEST_PYTHON to the installed bridge's venv/bin/python to
exercise the actual pinned proxy. No upstream network or model is used.
"""

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from outerloop import codex_bridge as bridge
from outerloop.bridge_install import runtime_path


def event(delta, index=0, finish=None):
    return {
        "id": "chat-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
    }


def lines(value):
    return [
        b"event: message\n",
        b"id: opener\n",
        b"data: " + json.dumps(value).encode() + b"\n",
        b"\n",
    ]


@pytest.mark.parametrize(
    "delta",
    [
        {"reasoning_content": "think"},
        {"tool_calls": [{"index": 0, "id": "call-a"}]},
        {"content": "text"},
    ],
)
def test_whole_event_opener_and_per_choice_roles(delta):
    events = bridge.Events()
    assert events.emit(lines(event({"role": "assistant", "content": ""}, 0))) == b""
    assert events.emit(lines(event({"role": "assistant", "content": ""}, 1))) == b""
    for index in (1, 0):
        result = events.emit(lines(event(delta, index)))
        assert json.loads(result.split(b"data: ")[1])["choices"][0]["delta"] == {
            "role": "assistant",
            **delta,
        }
    assert not events.held
    # Later empty chunks are real stream events, not new openers.
    value = lines(event({"role": "assistant", "content": ""}))
    assert events.emit(value) == b"".join(value)


@pytest.mark.parametrize(
    "extra", [{"tool_calls": []}, {"reasoning_content": ""}, {"refusal": None}]
)
def test_strict_opener(extra):
    assert bridge.is_opener(event({"role": "assistant", "content": "", **extra})) is None
    assert bridge.is_opener(event({"role": "assistant"}, finish="stop")) is None


def test_multiline_and_headers():
    events = bridge.Events()
    assert events.emit(lines(event({"role": "assistant"}))) == b""
    value = json.dumps(event({"content": "hello"}), indent=2).encode()
    result = events.emit([b"data: " + line + b"\n" for line in value.splitlines()] + [b"\n"])
    assert b'"role": "assistant"' in result
    headers = Message()
    for key, header_value in [
        ("Connection", "x-private"),
        ("X-Private", "drop"),
        ("Transfer-Encoding", "chunked"),
        ("Content-Length", "55"),
        ("Content-Type", "text/event-stream"),
    ]:
        headers[key] = header_value
    assert bridge.end_to_end(headers) == {"Content-Type": "text/event-stream"}


@pytest.fixture
def bridge_python():
    python = Path(
        os.environ.get("OUTERLOOP_BRIDGE_TEST_PYTHON", str(runtime_path() / "venv/bin/python"))
    )
    if not python.is_file():
        pytest.skip("install the locked bridge runtime to run proxy compatibility tests")
    return str(python)


@contextlib.contextmanager
def upstream():
    requests = []
    streams: list[list[bytes]] = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((body, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for chunk in streams.pop(0):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, streams
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def stream(*deltas, finish="stop"):
    return [b"".join(lines(event(delta))) for delta in deltas] + [
        b"".join(lines(event({}, finish=finish))),
        b"data: [DONE]\n\n",
    ]


def launch(python, tmp_path, url, source):
    client = tmp_path / "client.py"
    client.write_text(source)
    env = {**os.environ, "HOME": str(tmp_path), "OUTERLOOP_SESSION_KEY": "upstream-secret"}
    return subprocess.run(
        [
            python,
            str(Path(bridge.__file__)),
            "run",
            url,
            "test-model",
            "180",
            sys.executable,
            str(client),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=200,
    )


CLIENT = """
import json, os, sys, urllib.request, urllib.error
url = json.loads(sys.argv[sys.argv.index('-c') + 1].split('=', 1)[1])
unauthenticated = urllib.request.Request(url + '/responses',
    data=b'{"model":"test-model","input":"hello"}',
    headers={'Content-Type': 'application/json'})
try:
    urllib.request.urlopen(unauthenticated, timeout=30)
except urllib.error.HTTPError as error:
    assert error.code == 401, error.code
else:
    raise AssertionError('proxy accepted an unauthenticated request')
def request(history, tools):
    payload = dict(model='test-model', input=history, tools=tools, stream=True, store=False,
                   instructions='Keep order', parallel_tool_calls=True, tool_choice='auto')
    req = urllib.request.Request(url + '/responses', data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['OUTERLOOP_SESSION_KEY'],
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        events = [json.loads(line[6:]) for line in response if line.startswith(b'data: {')]
    assert events[0]['type'] == 'response.created', events[0]
    added = {e['item']['id']: e['output_index'] for e in events
             if e.get('type') == 'response.output_item.added'}
    for e in events:
        if e.get('type') == 'response.output_item.done':
            assert added[e['item']['id']] == e['output_index'], e
    completed = [e['response'] for e in events if e.get('type') == 'response.completed']
    assert len(completed) == 1, events
    return completed[0]['output']
"""


def test_real_proxy_parallel_custom_reasoning_replay(bridge_python, tmp_path):
    patch = "*** Begin Patch\n*** Add File: sample.txt\n+héllo\\world\n*** End Patch\n"
    with upstream() as (url, requests, streams):
        streams.extend(
            [
                stream(
                    {"role": "assistant", "content": ""},
                    {"reasoning_content": "first thought"},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-a",
                                "type": "function",
                                "function": {
                                    "name": "apply_patch",
                                    "arguments": json.dumps({"content": patch})[:20],
                                },
                            },
                            {
                                "index": 1,
                                "id": "call-b",
                                "type": "function",
                                "function": {"name": "shell", "arguments": '{"command":'},
                            },
                        ]
                    },
                    {
                        "tool_calls": [
                            {"index": 1, "function": {"arguments": '"pwd"}'}},
                            {
                                "index": 0,
                                "function": {"arguments": json.dumps({"content": patch})[20:]},
                            },
                        ]
                    },
                    finish="tool_calls",
                ),
                stream(
                    {"role": "assistant", "content": ""},
                    {"reasoning_content": "second thought"},
                    {"content": "done"},
                ),
                stream(
                    {"role": "assistant", "content": ""},
                    {"reasoning_content": "third thought"},
                    {"content": "resumed"},
                ),
            ]
        )
        client = (
            CLIENT
            + f"""
tools = [dict(type='custom', name='apply_patch', description='Apply patch',
              format=dict(type='text')),
         dict(type='function', name='shell', parameters=dict(type='object',
              properties=dict(command=dict(type='string'))))]
history = [dict(role='developer', content='Developer instruction'),
           dict(role='user', content='edit')]
first = request(history, tools)
print(json.dumps(first))
patches = [i for i in first if i['type'] == 'custom_tool_call']
assert patches[0]['input'] == {patch!r}, patches
assert patches[0]['call_id'] == 'call-a'
assert [i for i in first if i['type'] == 'function_call'][0]['call_id'] == 'call-b'
history += first + [dict(type='custom_tool_call_output', call_id='call-a', output='ok'),
                    dict(type='function_call_output', call_id='call-b', output='/work')]
second = request(history, tools)
history += second + [dict(role='user', content='continue')]
# A new client request with serialized history represents persisted resume replay.
history = json.loads(json.dumps(history))
request(history, tools)
"""
        )
        result = launch(bridge_python, tmp_path, url, client)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(requests) == 3
        for body, headers in requests:
            assert headers["Authorization"] == "Bearer upstream-secret"
            assert body["messages"][0]["content"] == "Keep order"
            assert body["tool_choice"] == "auto"
            assert body["parallel_tool_calls"] is True
            assert any(t["function"]["name"] == "apply_patch" for t in body["tools"])
        for body, _headers in requests[1:]:
            calls = {
                call["id"]: call
                for message in body["messages"]
                for call in message.get("tool_calls", [])
            }
            assert json.loads(calls["call-a"]["function"]["arguments"])["content"] == patch
            assert json.loads(calls["call-b"]["function"]["arguments"]) == {"command": "pwd"}
            outputs = [
                (m["tool_call_id"], m["content"]) for m in body["messages"] if m["role"] == "tool"
            ]
            assert outputs == [("call-a", "ok"), ("call-b", "/work")]
        reasoning = [
            m.get("reasoning_content")
            for m in requests[2][0]["messages"]
            if m["role"] == "assistant"
        ]
        assert "first thought" in reasoning and "second thought" in reasoning
        assert "upstream-secret" not in result.stdout + result.stderr
        assert all(
            "upstream-secret" not in p.read_text() for p in tmp_path.rglob("*") if p.is_file()
        )


def test_pinned_codex_patch_shell_and_resume(bridge_python, tmp_path, monkeypatch):
    from outerloop import bridge_install
    from outerloop.endpoints import EndpointProfile
    from outerloop.harness import CodexHarness

    binary = os.environ.get("OUTERLOOP_BRIDGE_TEST_CODEX")
    if not binary:
        pytest.skip("set OUTERLOOP_BRIDGE_TEST_CODEX to test the pinned CLI")
    from outerloop.harness_pins import pins

    assert pins("codex")["version"] in subprocess.check_output([binary, "--version"], text=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "venv").symlink_to(Path(bridge_python).parent.parent, target_is_directory=True)
    monkeypatch.setenv("OUTERLOOP_BRIDGE_RUNTIME", str(runtime))
    monkeypatch.setattr(bridge_install, "ready", lambda path: True)
    workspace = tmp_path / "work"
    workspace.mkdir()
    patch = "*** Begin Patch\n*** Add File: hello.txt\n+hello bridge\n*** End Patch\n"
    with upstream() as (url, requests, streams):
        streams.extend(
            [
                stream(
                    {"role": "assistant", "content": ""},
                    {"reasoning_content": "edit thought"},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "patch-id",
                                "type": "function",
                                "function": {
                                    "name": "apply_patch",
                                    "arguments": json.dumps({"content": patch}),
                                },
                            }
                        ]
                    },
                    finish="tool_calls",
                ),
                stream(
                    {"role": "assistant", "content": ""},
                    {"reasoning_content": "shell thought"},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "shell-id",
                                "type": "function",
                                "function": {
                                    "name": "exec_command",
                                    "arguments": json.dumps({"cmd": "cat hello.txt"}),
                                },
                            }
                        ]
                    },
                    finish="tool_calls",
                ),
                stream(
                    {"role": "assistant", "content": ""},
                    {"reasoning_content": "finish thought"},
                    {"content": "finished"},
                ),
                stream(
                    {"role": "assistant", "content": ""},
                    {"reasoning_content": "resume thought"},
                    {"content": "resumed"},
                ),
            ]
        )
        harness = CodexHarness(
            api_key="upstream-secret",
            binary=binary,
            model="test-model",
            endpoint=EndpointProfile("fake", url, tmp_path / "unused-key", "test-model", ("chat",)),
            timeout_s=180,
        )
        first = harness.run("Apply the patch and run the command.", workspace)
        assert not first.is_error, first
        assert (workspace / "hello.txt").exists(), [
            m for m in requests[1][0]["messages"] if m["role"] == "tool"
        ]
        assert (workspace / "hello.txt").read_bytes() == b"hello bridge\n"
        second = harness.run("Continue.", workspace, first.session_id)
        assert not second.is_error, second
        assert second.session_id == first.session_id
        assert len(requests) == 4
        reasoning = [
            m.get("reasoning_content")
            for m in requests[-1][0]["messages"]
            if m["role"] == "assistant"
        ]
        assert all(
            thought in reasoning for thought in ("edit thought", "shell thought", "finish thought")
        ), reasoning


@pytest.mark.parametrize(
    "apis,expected", [("chat", True), ("chat,responses", False), ("responses", False)]
)
def test_endpoint_selection_and_used(tmp_path, apis, expected):
    from outerloop.endpoints import endpoint_profile
    from outerloop.harness_cli import used_harnesses

    key = tmp_path / "key"
    key.write_text("secret")
    key.chmod(0o600)
    env = {
        "OUTERLOOP_ENDPOINT_TEST_URL": "http://127.0.0.1:8000/v1",
        "OUTERLOOP_ENDPOINT_TEST_KEY_FILE": str(key),
        "OUTERLOOP_ENDPOINT_TEST_API": apis,
        "OUTERLOOP_ENDPOINT_TEST_MODEL": "model",
        "OUTERLOOP_AUTHOR_BACKEND": "codex",
        "OUTERLOOP_AUTHOR_MODEL": "model[endpoint=test]",
        "OUTERLOOP_PANEL": "",
    }
    assert endpoint_profile("test", "codex", environ=env).codex_bridge is expected
    assert ("bridge" in used_harnesses(env)) is expected
    env.update(
        OUTERLOOP_AUTHOR_BACKEND="claude",
        OUTERLOOP_AUTHOR_MODEL="claude-model",
        OUTERLOOP_PANEL="review:codex:model[endpoint=test]",
    )
    assert ("bridge" in used_harnesses(env)) is expected
    env.update(
        OUTERLOOP_PANEL="",
        OUTERLOOP_AUTHOR_OVERRIDES=json.dumps(
            {"owner/repo": {"backend": "codex", "model": "model[endpoint=test]"}}
        ),
    )
    assert ("bridge" in used_harnesses(env)) is expected


@pytest.mark.parametrize(
    "payload",
    [
        {"previous_response_id": "resp-id"},
        {"conversation": "conv"},
        {"tools": [{"type": "web_search"}]},
        {"input": [{"role": "user", "content": [{"type": "input_image"}]}]},
        {"text": {"format": {"type": "json_schema"}}},
        {"input": [{"type": "function_call_output", "output": [{"type": "input_image"}]}]},
    ],
)
def test_reject_unsupported(payload):
    assert not bridge.supported_request(payload)


def test_shim_auth_chunked_and_truncation(tmp_path):
    import http.client

    with upstream() as (url, requests, streams), bridge.listener() as sock:
        port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, bridge.__file__, "shim", str(sock.fileno())],
            pass_fds=(sock.fileno(),),
            env={
                **os.environ,
                "BRIDGE_SHIM_KEY": "local-secret",
                "OUTERLOOP_SESSION_KEY": "remote-secret",
                "BRIDGE_UPSTREAM": url,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sock.close()
        try:
            bridge.wait_ready(f"http://127.0.0.1:{port}/health", "local-secret", [process])
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/chat/completions", b"{}")
            assert conn.getresponse().status == 401
            conn.close()
            assert not requests
            streams.append(stream({"role": "assistant", "content": ""}, {"content": "hello"}))
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST",
                "/v1/chat/completions",
                iter([b'{"model":', b'"test"}']),
                headers={
                    "Authorization": "Bearer local-secret",
                    "Content-Type": "application/json",
                    "Connection": "x-remove",
                    "X-Remove": "secret",
                },
                encode_chunked=True,
            )
            response = conn.getresponse()
            assert b'"content": "hello"' in response.read()
            conn.close()
            assert requests[0][1]["Authorization"] == "Bearer remote-secret"
            assert "X-Remove" not in requests[0][1]
            streams.append([b"".join(lines(event({"content": "partial"})))])
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST", "/v1/chat/completions", b"{}", {"Authorization": "Bearer local-secret"}
            )
            with pytest.raises(http.client.IncompleteRead):
                conn.getresponse().read()
            conn.close()
        finally:
            bridge.stop([process])
        stdout, stderr = process.communicate()
        assert stdout == stderr == b""


@pytest.mark.parametrize("failure", ["child", "cancel", "timeout", "normal"])
def test_supervisor_reaps_children_and_closes_ports(tmp_path, failure):
    import signal
    import time

    # Stand-in sidecars use the same inherited sockets and environment contract.
    # This exercises lifecycle failures without depending on the optional runtime.
    worker = tmp_path / "worker.py"
    worker.write_text("""
import json, os, socket, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
server = HTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
server.socket.close()
server.socket = socket.socket(fileno=int(sys.argv[2]))
record = dict(pid=os.getpid(), port=server.socket.getsockname()[1],
              keys=[k for k in os.environ if 'KEY' in k])
Path(__file__).with_name(sys.argv[1] + '.json').write_text(json.dumps(record))
server.serve_forever()
""")
    client = tmp_path / "client.py"
    client.write_text(f"""
import json, os, time
from pathlib import Path
record = dict(pid=os.getpid(), keys=[k for k in os.environ if 'KEY' in k])
pending = Path(__file__).with_suffix('.pending')
pending.write_text(json.dumps(record))
pending.replace(Path(__file__).with_suffix('.json'))
time.sleep({0.1 if failure == "normal" else 60})
""")
    runner = tmp_path / "runner.py"
    runner.write_text(f"""
import sys
from pathlib import Path
from outerloop import codex_bridge as b
b.__file__ = {str(worker)!r}
real_clock = b.time.monotonic
b.time.monotonic = lambda: real_clock() + (
    120 if Path(__file__).with_name('expire').exists() else 0)
command = [sys.executable, {str(client)!r}]
sys.exit(b.supervise('http://127.0.0.1:1', 'model', 60, command))
""")
    process = subprocess.Popen(
        [sys.executable, str(runner)],
        env={**os.environ, "OUTERLOOP_SESSION_KEY": "remote-secret"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "client.json").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        records = {
            name: json.loads((tmp_path / f"{name}.json").read_text())
            for name in ("shim", "proxy", "client")
        }
        assert set(records["shim"]["keys"]) == {"BRIDGE_SHIM_KEY", "OUTERLOOP_SESSION_KEY"}
        assert set(records["proxy"]["keys"]) == {"BRIDGE_SHIM_KEY", "LITELLM_MASTER_KEY"}
        assert set(records["client"]["keys"]) == {"OUTERLOOP_SESSION_KEY"}
        if failure == "child":
            os.kill(records["proxy"]["pid"], signal.SIGKILL)
        elif failure == "cancel":
            process.terminate()
        elif failure == "timeout":
            # Advance only after every child is running; CPU contention cannot
            # turn this cleanup test into an accidental startup timeout test.
            (tmp_path / "expire").touch()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == (0 if failure == "normal" else 1)
        assert b"remote-secret" not in stdout + stderr
        for record in records.values():
            with pytest.raises(ProcessLookupError):
                os.kill(record["pid"], 0)
            if "port" in record:
                with socket.socket() as check:
                    assert check.connect_ex(("127.0.0.1", record["port"])) != 0
    finally:
        bridge.stop([process])


def test_real_proxy_text_tool_first_and_truncated(bridge_python, tmp_path):
    with upstream() as (url, _requests, streams):
        streams.extend(
            [
                stream({"role": "assistant", "content": "text only"}),
                stream(
                    {"role": "assistant", "content": ""},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "tool-first",
                                "type": "function",
                                "function": {"name": "shell", "arguments": "{}"},
                            }
                        ]
                    },
                    finish="tool_calls",
                ),
                [b"".join(lines(event({"content": "truncated"})))],
            ]
        )
        result = launch(
            bridge_python,
            tmp_path,
            url,
            CLIENT
            + """
output = request('hello', [])
assert output[0]['content'][0]['text'] == 'text only'
output = request('tool', [dict(type='function', name='shell',
                 parameters=dict(type='object', properties={}))])
assert output[0]['call_id'] == 'tool-first'
try:
    request('truncate', [])
except Exception:
    pass
else:
    raise AssertionError('truncated stream reported success')
""",
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_lock_integrity_and_contained_mounts(tmp_path, monkeypatch):
    import hashlib
    import tomllib

    from outerloop import bridge_install
    from outerloop.endpoints import EndpointProfile
    from outerloop.harness import CodexHarness
    from outerloop.harness_pins import pins

    source = Path(bridge_install.__file__).with_name("bridge_runtime")
    project = tomllib.loads((source / "pyproject.toml").read_text())
    assert f"litellm[proxy]=={pins('bridge')['version']}" in project["project"]["dependencies"]
    lock = tomllib.loads((source / "uv.lock").read_text())
    assert (
        next(p for p in lock["package"] if p["name"] == "litellm")["version"]
        == pins("bridge")["version"]
    )
    assert all(
        p.get("wheels") or p.get("sdist") or p["source"].get("virtual") for p in lock["package"]
    )
    runtime = tmp_path / "runtime"
    python = runtime / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("interpreter")
    python.chmod(0o755)
    assert not bridge_install.ready(runtime)
    (runtime / ".complete").write_text(
        f"{pins('bridge')['version']} {bridge_install.lock_digest()}"
    )
    (runtime / "python.sha256").write_text(hashlib.sha256(python.read_bytes()).hexdigest())
    assert bridge_install.ready(runtime)
    monkeypatch.setenv("OUTERLOOP_BRIDGE_RUNTIME", str(runtime))
    harness = CodexHarness(
        "secret",
        binary="/opt/codex",
        container_image="image.sif",
        endpoint=EndpointProfile(
            "test", "https://example.com/v1", tmp_path / "key", "model", ("chat",)
        ),
    )
    argv = harness._apptainer_argv(["supervisor"], tmp_path / "home", tmp_path / "work")
    assert f"{runtime}:{runtime}:ro" in argv
    assert f"{bridge.__file__}:{bridge.__file__}:ro" in argv
    assert argv[-2:] == ["image.sif", "supervisor"]
    assert "secret" not in " ".join(argv)
    python.write_text("corrupt")
    assert not bridge_install.ready(runtime)


def test_bridge_upgrade_preserves_legacy_env_and_retries(tmp_path, monkeypatch):
    import hashlib

    from outerloop import bridge_install, harness_cli
    from outerloop.harness_pins import pins

    env_file = tmp_path / ".env"
    legacy = '# legacy deployment\nOUTERLOOP_CODEX_BIN="/opt/codex"\nOUTERLOOP_PANEL=\n'
    env_file.write_text(legacy)
    env = {"OUTERLOOP_CACHE_ROOT": str(tmp_path / "cache")}
    interrupted = True
    calls = []

    def install(script, target, install_env):
        calls.append(target)
        if interrupted:
            raise OSError("interrupted install")
        python = target / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("interpreter")
        python.chmod(0o755)
        (target / "python.sha256").write_text(hashlib.sha256(python.read_bytes()).hexdigest())
        (target / ".complete").write_text(
            f"{pins('bridge')['version']} {bridge_install.lock_digest()}"
        )

    monkeypatch.setattr(harness_cli, "run_installer", install)
    with pytest.raises(OSError):
        harness_cli.upgrade_one("bridge", env, env_file, tmp_path)
    assert env_file.read_text() == legacy
    assert not calls[0].exists()
    # Existing retry-record format: age it to permit the next attempt.
    retry = next((tmp_path / "cache/harness-failures").glob("*.json"))
    state = json.loads(retry.read_text())
    state["retry_after"] = 0
    retry.write_text(json.dumps(state))
    interrupted = False
    harness_cli.upgrade_one("bridge", env, env_file, tmp_path)
    after = env_file.read_text()
    assert after.startswith(legacy)
    assert "OUTERLOOP_BRIDGE_RUNTIME=" in after
    assert bridge_install.ready(Path(env["OUTERLOOP_BRIDGE_RUNTIME"]))
    harness_cli.upgrade_one("bridge", env, env_file, tmp_path)
    assert env_file.read_text() == after
    assert len(calls) == 2


def test_pinned_codex_local_compaction(bridge_python, tmp_path, monkeypatch):
    from outerloop import bridge_install
    from outerloop.endpoints import EndpointProfile
    from outerloop.harness import CodexHarness

    binary = os.environ.get("OUTERLOOP_BRIDGE_TEST_CODEX")
    if not binary:
        pytest.skip("set OUTERLOOP_BRIDGE_TEST_CODEX to test local compaction")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "venv").symlink_to(Path(bridge_python).parent.parent, target_is_directory=True)
    monkeypatch.setenv("OUTERLOOP_BRIDGE_RUNTIME", str(runtime))
    monkeypatch.setattr(bridge_install, "ready", lambda path: True)
    workspace = tmp_path / "work"
    workspace.mkdir()
    with upstream() as (url, requests, streams):
        first = stream({"role": "assistant", "content": "first answer"})
        first.insert(
            -1,
            b"data: "
            + json.dumps(
                {
                    "id": "chat-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 20000,
                        "completion_tokens": 10,
                        "total_tokens": 20010,
                    },
                }
            ).encode()
            + b"\n\n",
        )
        streams.extend(
            [
                first,
                stream({"role": "assistant", "content": "COMPACT SUMMARY"}),
                stream({"role": "assistant", "content": "after compaction"}),
            ]
        )
        harness = CodexHarness(
            "upstream-secret",
            binary=binary,
            model="test-model",
            timeout_s=180,
            endpoint=EndpointProfile("fake", url, tmp_path / "key", "test-model", ("chat",)),
            extra_args=("-c", "model_auto_compact_token_limit=10000"),
        )
        first_result = harness.run("Say hello.", workspace)
        assert not first_result.is_error, first_result
        resumed = harness.run("Continue.", workspace, first_result.session_id)
        assert not resumed.is_error, resumed
        assert resumed.final_text == "after compaction"
        assert len(requests) == 3
        assert "COMPACT SUMMARY" in json.dumps(requests[-1][0]["messages"])


@pytest.mark.parametrize("route", ["native", "responses", "bridge"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_interrupted_harness_cleanup_is_bridge_only(tmp_path, monkeypatch, route, error):
    from unittest.mock import Mock

    from outerloop import bridge_install, harness
    from outerloop.endpoints import EndpointProfile

    process = Mock()
    process.communicate.side_effect = error("interrupted")
    monkeypatch.setattr(harness.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(harness.CodexHarness, "_login", lambda self, home: None)
    monkeypatch.setattr(bridge_install, "ready", lambda path: True)
    drain = Mock()
    monkeypatch.setattr(harness, "_kill_and_drain", drain)
    endpoint = (
        None
        if route == "native"
        else EndpointProfile(
            "test",
            "https://example.com/v1",
            tmp_path / "key",
            "model",
            ("chat",) if route == "bridge" else ("responses",),
        )
    )
    with pytest.raises(error, match="interrupted"):
        harness.CodexHarness("secret", endpoint=endpoint).run("brief", tmp_path)
    if route == "bridge":
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        drain.assert_called_once_with(process)
    else:
        process.terminate.assert_not_called()
        process.wait.assert_not_called()
        drain.assert_not_called()
