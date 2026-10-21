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
    assert env[hs.MEMBER] == "t"
    # what `env` prints for a human's shell carries no membership marker
    assert hs.MEMBER not in box.client_env(base, member=False)
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
    hs.parent_exec("workspace special:hypruse silent", "/x y/nested.sh")
    sent = fake.calls[-1]
    if lua:
        assert sent[0] == "dispatch" and sent[1].startswith("hl.dsp.exec_cmd(")
        assert "special:hypruse" in sent[1] and "nested.sh" in sent[1]
    else:
        # quoted for the /bin/sh -c Hyprland runs it through
        assert sent == ["dispatch", "exec", "[workspace special:hypruse silent] exec '/x y/nested.sh'"]


def test_parent_exec_reports_a_refusal(monkeypatch):
    monkeypatch.setattr(hs, "parent_env", lambda: {})
    monkeypatch.setattr(
        hs, "_hyprctl_ok",
        lambda args, env: subprocess.CompletedProcess(args, 0, "hyprlang" if args[1] == "status" else "invalid rule", ""),
    )
    with pytest.raises(hs.SandboxError, match="invalid rule"):
        hs.parent_exec("bad", "/x")


INSTANCES = [
    {"instance": "old_1", "time": 100, "pid": 1, "wl_socket": "wayland-2"},
    {"instance": "new_2", "time": 200, "pid": 2, "wl_socket": "wayland-10"},
]


def _instances(monkeypatch, listing):
    monkeypatch.setattr(
        hs, "_hyprctl_ok",
        lambda args, env: subprocess.CompletedProcess(args, 0, json.dumps(listing), ""),
    )


def test_parent_env_without_variables_takes_one_live_instance_whole(monkeypatch):
    _instances(monkeypatch, INSTANCES)
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    env = hs.parent_env()
    assert (env["HYPRLAND_INSTANCE_SIGNATURE"], env["WAYLAND_DISPLAY"]) == ("new_2", "wayland-10")


def test_parent_env_replaces_a_dead_signature_and_its_socket(monkeypatch):
    # a crashed instance leaves its socket file behind; only a live one counts
    _instances(monkeypatch, INSTANCES)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "crashed_0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    env = hs.parent_env()
    assert (env["HYPRLAND_INSTANCE_SIGNATURE"], env["WAYLAND_DISPLAY"]) == ("new_2", "wayland-10")


def test_parent_env_keeps_a_live_signature(monkeypatch):
    _instances(monkeypatch, INSTANCES)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "old_1")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    env = hs.parent_env()
    assert (env["HYPRLAND_INSTANCE_SIGNATURE"], env["WAYLAND_DISPLAY"]) == ("old_1", "wayland-2")


def test_parent_env_without_hyprland(monkeypatch):
    _instances(monkeypatch, [])
    with pytest.raises(hs.SandboxError, match="no running Hyprland"):
        hs.parent_env()


# --- processes --------------------------------------------------------------------


def _spawn(env_extra: dict[str, str]) -> subprocess.Popen:
    env = dict(os.environ, **env_extra)
    return subprocess.Popen(["sleep", "30"], env=env)


def _member_env(box) -> dict[str, str]:
    return {"XDG_RUNTIME_DIR": str(box.dir), hs.MEMBER: box.name}


def test_stop_processes_kills_the_sandbox_and_spares_the_rest(runtime):
    box = hs.Sandbox("t")
    box.dir.mkdir()
    app = _spawn(_member_env(box))
    server = _spawn({**_member_env(box), "HUS_ROLE": "mcp"})
    stranger = _spawn({"XDG_RUNTIME_DIR": str(runtime)})
    # a human's shell after `eval "$(hypruse-sandbox env)"`: same runtime dir, no marker
    shell = _spawn({"XDG_RUNTIME_DIR": str(box.dir)})
    try:
        time.sleep(0.1)
        assert set(hs._members(box)) == {app.pid}
        box._stop_processes()
        assert app.wait(timeout=5) is not None
        time.sleep(0.1)
        assert server.poll() is None and stranger.poll() is None and shell.poll() is None
    finally:
        for p in (app, server, stranger, shell):
            p.kill()
            p.wait()


def test_stop_processes_follows_children_that_dropped_the_marker(runtime):
    box = hs.Sandbox("t")
    box.dir.mkdir()
    # a member whose child runs with a wiped environment (Flatpak, env -i)
    parent = subprocess.Popen(
        ["sh", "-c", "env -i sleep 30 & echo $!; wait"],
        env=dict(os.environ, **_member_env(box)), stdout=subprocess.PIPE, text=True,
    )
    try:
        child = int(parent.stdout.readline())
        assert child in hs._members(box)
        box._stop_processes()
        time.sleep(0.1)
        assert not hs._alive(child)
    finally:
        parent.kill()
        parent.wait()


def test_the_caller_and_its_ancestors_are_never_members(runtime, monkeypatch):
    box = hs.Sandbox("t")
    box.dir.mkdir()
    me = os.getpid()
    monkeypatch.setattr(hs, "_is_member", lambda pid, b: pid == me)
    assert me not in hs._members(box)


def test_a_zombie_counts_as_gone():
    proc = subprocess.Popen(["true"])
    deadline = time.monotonic() + 5
    while hs._state(proc.pid) not in ("Z", "") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not hs._alive(proc.pid)  # and it reaps it
    assert hs._state(proc.pid) == ""


def test_a_recycled_pid_is_never_signalled(runtime):
    box = hs.Sandbox("t")
    box.dir.mkdir()
    other = _spawn({"XDG_RUNTIME_DIR": str(box.dir)})  # same dir, but not a member
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
    assert box.lock.exists() and box.lock.parent == runtime  # never unlinked


def test_up_and_mcp_make_the_state_directory_private(runtime):
    box = hs.Sandbox("t")
    box.dir.mkdir(mode=0o755)  # as hypruse leaves it when it starts first
    os.chmod(box.dir, 0o755)
    box._make_dir()
    assert (box.dir.stat().st_mode & 0o777) == 0o700


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

    monkeypatch.setattr(hs.Sandbox, "healthy", lambda self: state["running"])
    monkeypatch.setattr(hs, "_spawn_watcher", lambda name: None)
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


DYING_SERVER = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"instructions": "X"}}), flush=True)
    elif msg.get("method") == "tools/call":
        sys.exit(1)  # crashes mid-call, never answering
'''

INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "desktop", "arguments": {}}}


def test_proxy_rewrites_instructions_and_starts_the_sandbox_on_the_first_call(monkeypatch, runtime):
    out, state = _run_proxy(monkeypatch, runtime, [INIT, LIST, CALL])
    by_id = {m["id"]: m for m in out}
    init = by_id[0]["result"]
    assert init["instructions"].startswith("This is hypruse-sandbox")
    assert init["instructions"].endswith("ORIGINAL")
    assert 'sandbox "m1"' in init["instructions"]
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
        assert (a, b) == ("m1", "m2")
    finally:
        os.close(fd_a)
        os.close(fd_b)
    c, fd_c = hs._claim_name()  # released claims are reused
    os.close(fd_c)
    assert c == "m1"


def test_proxy_starts_the_sandbox_for_a_batched_call(monkeypatch, runtime):
    out, state = _run_proxy(monkeypatch, runtime, [INIT, [LIST, CALL]])
    assert state["up"] == 1


def test_a_failed_start_is_not_retried_on_every_call(monkeypatch, runtime):
    call2 = dict(CALL, id=3)
    out, state = _run_proxy(monkeypatch, runtime, [INIT, CALL, call2], up_fails=True)
    assert state["up"] == 1
    assert all(m["result"]["isError"] for m in out if m["id"] in (2, 3))


def test_proxy_ends_and_answers_when_hypruse_dies(monkeypatch, runtime):
    fake = runtime / "dying.py"
    fake.write_text(DYING_SERVER)
    wrapper = runtime / "dying"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} -I {fake}\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("HUS_HYPRUSE", str(wrapper))
    monkeypatch.setattr(hs.Sandbox, "healthy", lambda self: True)
    monkeypatch.setattr(hs.Sandbox, "down", lambda self: None)
    monkeypatch.setattr(hs, "_spawn_watcher", lambda name: None)
    rfd, wfd = os.pipe()  # a client that keeps stdin open, like Claude Code
    os.write(wfd, (json.dumps(INIT) + "\n" + json.dumps(CALL) + "\n").encode())
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(os.fdopen(rfd, "rb")))
    monkeypatch.setattr(sys, "stdout", stdout)
    try:
        hs.mcp(None, (1920, 1080), False)  # returns instead of blocking on stdin
    finally:
        os.close(wfd)
    stdout.flush()
    msgs = [json.loads(x) for x in stdout.buffer.getvalue().decode().splitlines()]
    call = next(m for m in msgs if m.get("id") == 2)
    assert call["error"]["message"] == "hypruse exited"


# --- show / hide ------------------------------------------------------------------


def _monitors(focused: str, special_on: str | None):
    return [
        {"name": n, "focused": n == focused,
         "specialWorkspace": {"name": "special:hypruse" if n == special_on else ""}}
        for n in ("DP-1", "HDMI-A-1")
    ]


@pytest.mark.parametrize(
    "show,focused,open_on,toggles,raises",
    [
        (True, "DP-1", None, True, False),
        (True, "DP-1", "DP-1", False, False),
        (False, "DP-1", "DP-1", True, False),
        (False, "DP-1", None, False, False),
        # open on the other monitor: a toggle here would MOVE it, not hide it
        (False, "DP-1", "HDMI-A-1", False, True),
    ],
)
def test_show_hide_acts_on_the_focused_monitor_only(monkeypatch, runtime, show, focused, open_on, toggles, raises):
    sent = []
    monkeypatch.setattr(hs.Sandbox, "healthy", lambda self: True)
    monkeypatch.setattr(hs.Sandbox, "window", lambda self: None)
    monkeypatch.setattr(hs, "parent_q", lambda what: _monitors(focused, open_on))
    monkeypatch.setattr(hs, "parent_dispatch", lambda name, arg, lua: sent.append(name))
    box = hs.Sandbox("t")
    if raises:
        with pytest.raises(hs.SandboxError, match="HDMI-A-1"):
            hs.show_hide(box, show)
    else:
        hs.show_hide(box, show)
    assert ("togglespecialworkspace" in sent) == toggles


# --- the watcher ------------------------------------------------------------------


def _watch_proc(runtime, name, pid):
    return subprocess.Popen(
        [sys.executable, "-I", str(SCRIPT), "--name", name, "_watch", str(pid)],
        env=dict(os.environ, XDG_RUNTIME_DIR=str(runtime)),
    )


def test_watcher_takes_the_sandbox_down_after_its_server_dies(runtime):
    box = hs.Sandbox("m7")
    box.dir.mkdir()
    (box.dir / "nested.log").write_text("x")
    server = subprocess.Popen(["sleep", "30"])
    watcher = _watch_proc(runtime, "m7", server.pid)
    time.sleep(0.3)
    assert box.dir.exists()  # nothing happens while the server lives
    server.kill()
    server.wait()
    watcher.wait(timeout=10)
    assert not box.dir.exists()


def test_watcher_leaves_a_reclaimed_sandbox_alone(runtime):
    box = hs.Sandbox("m8")
    box.dir.mkdir()
    claim = hs._try_claim("m8")  # a new server already took the name
    try:
        watcher = _watch_proc(runtime, "m8", 999999999)  # long gone
        watcher.wait(timeout=10)
        assert box.dir.exists()
    finally:
        os.close(claim)
