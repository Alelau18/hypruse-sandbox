"""Live check of the sandbox's silent audio, with a guard for the human's card.

    python3 -I tests/audio/live_audio.py   (needs gcc, mingw-w64-gcc, wine, pactl)

A watchdog thread scans for sandbox processes holding /dev/snd/pcm* every
50 ms and SIGKILLs any it finds (recording a failure), so even a broken fix
cannot keep the human's card. Before/after: the human's default sink and the
set of processes holding a PCM device must match.
"""

import os
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import shutil
import tempfile

HERE = Path(__file__).resolve().parent
HS = [str(HERE.parent.parent / "hypruse-sandbox"), "--name", "au"]
STATE = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hus-au"
WORK = Path(tempfile.mkdtemp(prefix="hus-audio-"))
PREFIX = WORK / "wineprefix"  # fresh, never the human's ~/.wine


def build():
    """The two probes: an ALSA client and a Windows waveOut client for Wine."""
    subprocess.run(["gcc", "-O1", "-o", str(WORK / "alsatest"), str(HERE / "alsatest.c"), "-lasound"], check=True)
    subprocess.run(["x86_64-w64-mingw32-gcc", "-O1", "-o", str(WORK / "wavetest.exe"),
                    str(HERE / "wavetest.c"), "-lwinmm"], check=True)
results = []


def record(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", flush=True)


def snd_holders():
    held = {}
    for e in os.scandir("/proc"):
        if e.name.isdigit():
            try:
                for fd in os.scandir(f"/proc/{e.name}/fd"):
                    t = os.readlink(fd.path)
                    if t.startswith("/dev/snd/pcm"):
                        held.setdefault(int(e.name), []).append(t)
            except OSError:
                pass
    return held


def in_sandbox(pid):
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    return f"XDG_RUNTIME_DIR={STATE}".encode() in env


def human_sink():
    out = subprocess.run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True)
    return next((ln.split("=", 1)[1].strip() for ln in out.stdout.splitlines() if "node.name" in ln), "?")


stop = threading.Event()
caught = []


def watchdog():
    while not stop.is_set():
        for pid, devs in snd_holders().items():
            if in_sandbox(pid):
                caught.append((pid, devs))
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.05)


def run(*args, timeout=60, extra_env=None):
    env = dict(os.environ, **(extra_env or {}))
    return subprocess.run([*HS, "run", *args], capture_output=True, text=True, timeout=timeout, env=env)


def main():
    build()
    silence = WORK / "silence.wav"
    with wave.open(str(silence), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000); w.writeframes(b"\0" * 48000 * 4)
    sink0, holders0 = human_sink(), sorted(snd_holders())
    print("before:", sink0, holders0)
    guard = threading.Thread(target=watchdog, daemon=True)
    guard.start()
    try:
        up = subprocess.run([*HS, "up"], capture_output=True, text=True, timeout=60)
        record("sandbox up", up.returncode == 0, up.stderr.strip()[:200])
        st = subprocess.run([*HS, "status"], capture_output=True, text=True).stdout.strip()
        record("status says audio silent", "audio silent" in st, st)
        info = run("pactl", "info").stdout
        record("Pulse default sink is the silent one", "Default Sink: hus-silent" in info,
               next((ln for ln in info.splitlines() if "Default Sink" in ln), "no Default Sink line"))
        t0 = time.monotonic()
        pa = run("paplay", str(silence), timeout=10)
        ms = (time.monotonic() - t0) * 1000
        record("paplay plays in real time", pa.returncode == 0 and 800 < ms < 3000, f"{ms:.0f} ms")
        al = run(str(WORK / "alsatest"), timeout=15).stdout
        print("   " + al.strip().replace("\n", "\n   "))
        record("ALSA default plays", "default: played" in al)
        record("ALSA hardware names refused", "OPENED" not in al and al.count("refused") == 4)
        PREFIX.mkdir(exist_ok=True)
        wine_env = {"WINEPREFIX": str(PREFIX), "WINEDLLOVERRIDES": "mscoree,mshtml=", "WINEDEBUG": "-all"}
        t0 = time.monotonic()
        wt = run("wine", str(WORK / "wavetest.exe"), timeout=180, extra_env=wine_env)
        print("   wine:", wt.stdout.strip().replace("\n", " | "), f"({time.monotonic() - t0:.0f} s incl. prefix setup)")
        record("Wine plays through the sandbox", "played 1 s" in wt.stdout and "done=1" in wt.stdout)
        record("no sandbox process ever held a sound card", not caught, str(caught))
    finally:
        subprocess.run(["env", f"WINEPREFIX={PREFIX}", "wineserver", "-k"], capture_output=True)
        down = subprocess.run([*HS, "down"], capture_output=True, text=True, timeout=30)
        stop.set()
        time.sleep(0.5)
        left = [p for p in (int(e.name) for e in os.scandir("/proc") if e.name.isdigit()) if in_sandbox(p)]
        record("sandbox down, nothing left", down.returncode == 0 and not left, str(left))
        sink1, holders1 = human_sink(), sorted(snd_holders())
        print("after:", sink1, holders1)
        record("human's default sink unchanged", sink1 == sink0, f"{sink0} -> {sink1}")
        record("human's card holders unchanged", holders1 == holders0, f"{holders0} -> {holders1}")
        if not left:
            shutil.rmtree(WORK, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
