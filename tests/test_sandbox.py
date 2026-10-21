"""Unit tests for hypruse-sandbox. None of them touch the live desktop: the
parent's hyprctl is faked, and the MCP proxy runs against a fake hypruse."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "hypruse-sandbox"


def _load():
    loader = importlib.machinery.SourceFileLoader("hypruse_sandbox", str(SCRIPT))
    spec = importlib.util.spec_from_loader("hypruse_sandbox", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


hs = _load()


@pytest.fixture
def runtime(monkeypatch):
    # short on purpose: the module refuses runtime paths too long for AF_UNIX
    path = Path(tempfile.mkdtemp(prefix="h", dir="/tmp"))
    monkeypatch.setattr(hs, "RUNTIME", path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


# --- names and paths --------------------------------------------------------------


def test_names():
    assert hs.check_name("main") == "main"
    assert hs.check_name("7") == "7"
    for bad in ("", "Main", "toolong", "a/b", "../x"):
        with pytest.raises(hs.SandboxError):
            hs.check_name(bad)


def test_state_dir_refuses_paths_hyprland_would_truncate(monkeypatch):
    monkeypatch.setattr(hs, "RUNTIME", Path("/run/user/1000"))
    deepest = len("/run/user/1000/hus-main") + len("/hypr/") + hs.SIG_MAX + len("/.socket2.sock")
    assert deepest <= hs.SUN_PATH_MAX
    assert hs.state_dir("main") == Path("/run/user/1000/hus-main")
    monkeypatch.setattr(hs, "RUNTIME", Path("/run/user/1000/a-much-longer-runtime"))
    with pytest.raises(hs.SandboxError, match="too long"):
        hs.state_dir("main")


def test_lua_str_escapes_everything_but_plain_ascii():
    assert hs.lua_str("es") == '"es"'
    assert hs.lua_str('a"b\\c') == '"a\\034b\\092c"'
    assert hs.lua_str("x\ny") == '"x\\010y"'
    assert hs.lua_str("ñ") == '"\\195\\177"'


def test_parse_size():
    assert hs.parse_size("1920x1080") == (1920, 1080)
    with pytest.raises(Exception):
        hs.parse_size("1920*1080")


# --- environment -----------------------------------------------------------------


def test_client_env_points_everything_into_the_sandbox(runtime):
    box = hs.Sandbox("t")
    base = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":1", "AT_SPI_BUS_ADDRESS": "x",
            "HYPRLAND_INSTANCE_SIGNATURE": "human", "HOME": "/home/u", "WAYLAND_SOCKET": "3"}
    env = box.client_env(base)
    assert env["XDG_RUNTIME_DIR"] == str(runtime / "hus-t")
    assert env["WAYLAND_DISPLAY"] == hs.WL_LINK
    assert env["HYPRLAND_INSTANCE_SIGNATURE"] == hs.SIG_LINK
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / 'hus-t' / 'bus'}"
    assert env["HOME"] == "/home/u"
    # the human's X display and a11y bus must never leak into the sandbox
    for gone in ("DISPLAY", "AT_SPI_BUS_ADDRESS", "WAYLAND_SOCKET"):
        assert gone not in env
    box.dir.mkdir()
    (box.dir / "display").write_text(":5\n")
    assert box.client_env(base)["DISPLAY"] == ":5"


# --- the parent's dialect ---------------------------------------------------------


class FakeHyprctl:
    def __init__(self, lua: bool):
        self.lua = lua
        self.calls: list[list[str]] = []

    def __call__(self, args, env):
        self.calls.append(args)
        out = "ok"
        if args[:2] == ["-j", "status"]:
            out = json.dumps({"configProvider": "lua" if self.lua else "hyprlang"})
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.mark.parametrize("lua", [False, True])
def test_parent_exec_speaks_both_dialects(monkeypatch, lua):
    fake = FakeHyprctl(lua)
    monkeypatch.setattr(hs, "_hyprctl_ok", fake)
    monkeypatch.setattr(hs, "parent_env", lambda: {})
    hs.parent_exec("workspace special:hypruse silent", "/x/nested.sh")
    sent = fake.calls[-1]
    if lua:
        assert sent[0] == "dispatch" and sent[1].startswith("hl.dsp.exec_cmd(")
        assert "special:hypruse" in sent[1] and "/x/nested.sh" in sent[1]
    else:
        assert sent == ["dispatch", "exec", "[workspace special:hypruse silent] /x/nested.sh"]


def test_parent_exec_reports_a_refusal(monkeypatch):
    monkeypatch.setattr(hs, "parent_env", lambda: {})
    monkeypatch.setattr(
        hs, "_hyprctl_ok",
        lambda args, env: subprocess.CompletedProcess(args, 0, "hyprlang" if args[1] == "status" else "invalid rule", ""),
    )
    with pytest.raises(hs.SandboxError, match="invalid rule"):
        hs.parent_exec("bad", "/x")


# --- processes --------------------------------------------------------------------


def _spawn(env_extra: dict[str, str]) -> subprocess.Popen:
    env = dict(os.environ, **env_extra)
    return subprocess.Popen(["sleep", "30"], env=env)


def test_stop_processes_kills_the_sandbox_and_spares_its_mcp_server(runtime):
    box = hs.Sandbox("t")
    box.dir.mkdir()
    app = _spawn({"XDG_RUNTIME_DIR": str(box.dir)})
    server = _spawn({"XDG_RUNTIME_DIR": str(box.dir), "HUS_ROLE": "mcp"})
    stranger = _spawn({"XDG_RUNTIME_DIR": str(runtime)})
    try:
        time.sleep(0.1)
        assert set(hs._pids_in(box.dir)) == {app.pid, server.pid}
        box._stop_processes()
        assert app.wait(timeout=5) is not None
        time.sleep(0.1)
        assert server.poll() is None and stranger.poll() is None
    finally:
        for p in (app, server, stranger):
            p.kill()
            p.wait()


def test_a_recycled_pid_is_never_signalled(runtime):
    box = hs.Sandbox("t")
    box.dir.mkdir()
    other = _spawn({"XDG_RUNTIME_DIR": str(runtime)})  # not this sandbox's
    try:
        (box.dir / "nested.pid").write_text(str(other.pid))
        assert box.nested_pid() is None
        box._stop_processes()
        time.sleep(0.1)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_down_removes_the_state_directory(runtime):
    box = hs.Sandbox("t")
    box.dir.mkdir()
    (box.dir / "hypr").mkdir()
    (box.dir / "nested.log").write_text("x")
    box.down()
    assert not box.dir.exists()


# --- the MCP proxy ----------------------------------------------------------------

FAKE_SERVER = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "hypruse", "version": "0"}, "instructions": "ORIGINAL"}
    elif msg["method"] == "tools/call":
        res = {"content": [{"type": "text", "text": "called " + msg["params"]["name"]}]}
    else:
        res = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
'''


def _run_proxy(monkeypatch, runtime, requests, up_fails=False):
    fake = runtime / "fake_hypruse.py"
    fake.write_text(FAKE_SERVER)
    wrapper = runtime / "fake_hypruse"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} -I {fake}\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("HUS_HYPRUSE", str(wrapper))
    state = {"running": False, "up": 0, "down": 0}

    def fake_up(self, size, audio):
        state["up"] += 1
        if up_fails:
            raise hs.SandboxError("no GPU")
        state["running"] = True
        return {}

    def fake_down(self):
        state["down"] += 1
        state["running"] = False

    monkeypatch.setattr(hs.Sandbox, "running", lambda self: state["running"])
    monkeypatch.setattr(hs.Sandbox, "up", fake_up)
    monkeypatch.setattr(hs.Sandbox, "down", fake_down)
    stdin = io.TextIOWrapper(io.BytesIO("".join(json.dumps(r) + "\n" for r in requests).encode()))
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    hs.mcp(None, (1920, 1080), False)
    stdout.flush()
    lines = stdout.buffer.getvalue().decode().splitlines()
    return [json.loads(x) for x in lines], state


INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "desktop", "arguments": {}}}


def test_proxy_rewrites_instructions_and_starts_the_sandbox_on_the_first_call(monkeypatch, runtime):
    out, state = _run_proxy(monkeypatch, runtime, [INIT, LIST, CALL])
    by_id = {m["id"]: m for m in out}
    init = by_id[0]["result"]
    assert init["instructions"].startswith("This is hypruse-sandbox")
    assert init["instructions"].endswith("ORIGINAL")
    assert 'sandbox "1"' in init["instructions"]
    assert init["serverInfo"]["name"] == "hypruse-sandbox"
    assert by_id[2]["result"]["content"][0]["text"] == "called desktop"
    assert state["up"] == 1  # once, before the call, not for initialize or tools/list
    assert state["down"] >= 1  # a claimed sandbox goes down with the server


def test_proxy_does_not_start_a_sandbox_nobody_used(monkeypatch, runtime):
    _, state = _run_proxy(monkeypatch, runtime, [INIT, LIST])
    assert state["up"] == 0


def test_proxy_answers_a_failed_start_without_forwarding(monkeypatch, runtime):
    out, state = _run_proxy(monkeypatch, runtime, [INIT, CALL], up_fails=True)
    call = next(m for m in out if m["id"] == 2)
    assert call["result"]["isError"] is True
    assert "no GPU" in call["result"]["content"][0]["text"]


def test_each_server_claims_its_own_number(runtime):
    a, fd_a = hs._claim_name()
    b, fd_b = hs._claim_name()
    try:
        assert (a, b) == ("1", "2")
    finally:
        os.close(fd_a)
        os.close(fd_b)
    c, fd_c = hs._claim_name()  # released claims are reused
    os.close(fd_c)
    assert c == "1"
