#!/usr/bin/env python3
"""Live check: drive apps in a throwaway sandbox and prove the human's seat
never moved.

Needs a running Hyprland session with a working GPU stack. Everything it
does happens inside sandbox "lt"; the only things it reads from the human's
desktop are hyprctl queries and the event socket, which it records for the
whole run and scans at the end for anything that touched the human's focus,
workspaces or monitors.

    python3 tests/live_check.py            # uses `hypruse` from PATH
    HYPRUSE_CMD="env PYTHONPATH=$HOME/NewStuff/hypruse/src python3 -m hypruse" python3 tests/live_check.py
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HS = [str(HERE.parent / "hypruse-sandbox"), "--name", "lt"]
HY = shlex.split(os.environ.get("HYPRUSE_CMD", "hypruse"))
TMP = Path(os.environ.get("TMPDIR", "/tmp")) / f"hus-live-{os.getpid()}"
results: list[tuple[str, str, str]] = []


def run(cmd: list[str], timeout: float = 60, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{shlex.join(cmd)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def hy(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return run([*HS, "run", *HY, *args], timeout=timeout)


def box_clients() -> list[dict]:
    return json.loads(run([*HS, "run", "hyprctl", "-j", "clients"], check=True).stdout)


def record(name: str, ok: bool | None, detail: str = "") -> None:
    status = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    results.append((status, name, detail))
    print(f"[{status}] {name}{': ' + detail if detail else ''}", flush=True)


def human_state() -> dict:
    act = json.loads(run(["hyprctl", "-j", "activewindow"]).stdout or "{}")
    mons = json.loads(run(["hyprctl", "-j", "monitors"]).stdout)
    return {
        "active": act.get("address"),
        "workspaces": {m["name"]: m["activeWorkspace"]["name"] for m in mons},
        "special": {m["name"]: (m.get("specialWorkspace") or {}).get("name", "") for m in mons},
    }


class EventLog(threading.Thread):
    """The human's socket2, recorded for the whole run."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(f"{rt}/hypr/{os.environ['HYPRLAND_INSTANCE_SIGNATURE']}/.socket2.sock")
        self.lines: list[str] = []

    def run(self) -> None:
        buf = b""
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            *lines, buf = buf.split(b"\n")
            self.lines += [x.decode(errors="replace") for x in lines]


def wait_until(pred, timeout: float = 10.0, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return None


def xlog_lines(path: Path) -> list[list[str]]:
    try:
        return [line.split() for line in path.read_text().splitlines()]
    except OSError:
        return []


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    events.start()
    before = human_state()
    sandbox_addr = None
    try:
        t0 = time.monotonic()
        up = run([*HS, "up"], timeout=40)
        record("sandbox up", up.returncode == 0, f"{time.monotonic() - t0:.1f}s {up.stderr.strip()}")
        if up.returncode != 0:
            return 1
        status = json.loads(run([*HS, "status", "--json"], check=True).stdout)[0]
        sandbox_addr = status.get("window")
        record("window hidden on special:hypruse", status.get("workspace") == "special:hypruse", str(status.get("workspace")))

        # a GTK form: type, read back through the sandbox's own a11y bus, click a pixel
        hy("launch", "--wait", "10", "--", "zenity", "--entry", "--title=LiveCheck", "--text=Type here")
        zen = next((c for c in box_clients() if c["class"] == "zenity"), None)
        record("launch zenity", zen is not None)
        if zen:
            typed = hy("keyboard", "type", "sandbox ok", "--window", zen["address"])
            record("type into zenity", typed.returncode == 0, typed.stdout.strip()[:80])
            ui = hy("ui", "--window", zen["address"])
            record("a11y reads the typed value", 'value="sandbox ok"' in ui.stdout, ui.stdout.strip()[:120])
            ok_line = next((ln for ln in ui.stdout.splitlines() if 'button "OK"' in ln), "")
            if ok_line:
                x, y = ok_line.rsplit("@", 1)[1].split()[0].split(",")
                hy("pointer", "click", x, y)
                gone = wait_until(lambda: not any(c["class"] == "zenity" for c in box_clients()), 5)
                record("pixel click on OK closes it", bool(gone))
            sel = hy("ui", "--window", "class:zenity")
            record("window selector class:zenity", sel.returncode in (0, 4) and "not found" not in sel.stderr, (sel.stderr or sel.stdout).strip()[:100])

        # GPU apps keep animating while the sandbox is hidden
        hy("launch", "--wait", "10", "--", "vkcube")
        a, b = TMP / "a.png", TMP / "b.png"
        hy("screenshot", "--lossless", "--out", str(a))
        time.sleep(0.5)
        hy("screenshot", "--lossless", "--out", str(b))
        differs = a.exists() and b.exists() and a.read_bytes() != b.read_bytes()
        record("vkcube animates while hidden", differs)
        for c in box_clients():
            if c["class"] in ("vkcube",):
                hy("hypr", "close_window", c["address"])

        # an XWayland client sees motion, drags and button holds
        xlog = TMP / "x.log"
        proc = subprocess.Popen([*HS, "run", sys.executable, "-I", str(HERE / "xmotion.py"), str(xlog), "800", "600"])
        time.sleep(0.3)
        w = hy("wait_for", "window_open", "--match", "xmotion", "--timeout", "8")
        record("wait_for catches a window that opened before the wait", w.returncode == 0 and "already" in w.stdout, w.stdout.strip()[:100])
        xw = wait_until(lambda: next((c for c in box_clients() if c["title"] == "xmotion"), None), 8)
        if not xw:
            record("xmotion window", False)
        else:
            ax, ay = xw["at"]
            wait_until(lambda: any(l[:1] == ["mapped"] for l in xlog_lines(xlog)), 5)
            hy("pointer", "move", str(ax + 200), str(ay + 150))
            time.sleep(0.3)
            moves = [l for l in xlog_lines(xlog) if l[0] == "motion"]
            near = any(abs(int(l[1]) - 200) <= 2 and abs(int(l[2]) - 150) <= 2 for l in moves)
            record("pointer move reaches an XWayland client as motion", near, f"{len(moves)} motion events")
            n0 = len(xlog_lines(xlog))
            hy("pointer", "drag", str(ax + 100), str(ay + 100), str(ax + 500), str(ay + 400))
            time.sleep(0.3)
            seg = xlog_lines(xlog)[n0:]
            kinds = [l[0] for l in seg]
            in_drag = kinds[kinds.index("press") + 1 : kinds.index("release")] if "press" in kinds and "release" in kinds else []
            record("drag delivers motion between press and release", in_drag.count("motion") >= 5,
                   f"{in_drag.count('motion')} motions while held")
            last_rel = next((l for l in reversed(seg) if l[0] == "release"), None)
            record("drag ends on the target", bool(last_rel) and abs(int(last_rel[2]) - 500) <= 2 and abs(int(last_rel[3]) - 400) <= 2, str(last_rel))
            n1 = len(xlog_lines(xlog))
            down = hy("pointer", "down", str(ax + 300), str(ay + 300))
            if down.returncode == 2:
                record("pointer down/up", None, "this hypruse has no down/up")
            else:
                time.sleep(0.4)
                hy("pointer", "up")
                seg = [l[0] for l in xlog_lines(xlog)[n1:]]
                record("pointer down then up", "press" in seg and "release" in seg and seg.index("press") < seg.index("release"))
        proc.terminate()
    finally:
        down = run([*HS, "down"], timeout=30)
        record("sandbox down", down.returncode == 0, down.stderr.strip())
        time.sleep(0.5)
        after = human_state()
        record("human's focused window unchanged", before["active"] == after["active"],
               f"{before['active']} -> {after['active']} (moves by the human count too)")
        record("human's workspaces unchanged", before["workspaces"] == after["workspaces"], str(after["workspaces"]))
        record("special:hypruse never shown", not any(v == "special:hypruse" for v in after["special"].values()))
        touched = [
            ln for ln in events.lines
            if (ln.startswith("activespecial>>") and "hypruse" in ln)
            or (ln.startswith(("activewindowv2>>", "activewindow>>")) and sandbox_addr and sandbox_addr[2:] in ln)
            or (ln.startswith("activewindow>>aquamarine"))
        ]
        record("no focus or special-workspace event for the sandbox", not touched, "; ".join(touched[:3]))
    fails = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed or skipped; {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
