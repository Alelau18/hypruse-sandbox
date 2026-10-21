"""Record the human's Hyprland event stream (socket2) to a file until killed.

Used by the live test: any focus, workspace or monitor event that names the
sandbox window, or a special-workspace toggle, means the sandbox interrupted
the human."""
import os, socket, sys, time
rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
sig = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
s = socket.socket(socket.AF_UNIX)
s.connect(f"{rt}/hypr/{sig}/.socket2.sock")
with open(sys.argv[1], "w", buffering=1) as out:
    buf = b""
    while True:
        data = s.recv(65536)
        if not data:
            break
        buf += data
        *lines, buf = buf.split(b"\n")
        for line in lines:
            out.write(f"{time.time():.3f} {line.decode(errors='replace')}\n")
