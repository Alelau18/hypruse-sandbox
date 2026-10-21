"""A minimal X11 client that logs the input it receives, one line per event.

Games under Wine/Proton are XWayland clients, and the bug this checks is
input that reaches the compositor but never reaches such a client as an
event (a cursor warp is not a MotionNotify). ctypes over libX11, so it needs
nothing beyond what XWayland already pulls in.

    python3 xmotion.py LOGFILE [WIDTH HEIGHT]

Lines: `motion X Y`, `press BUTTON X Y`, `release BUTTON X Y`, `key KEYCODE`,
`enter X Y`, `mapped`. X and Y are window-relative.
"""

import ctypes
import ctypes.util
import sys

x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
x11.XRootWindow.restype = ctypes.c_ulong
x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
x11.XCreateSimpleWindow.restype = ctypes.c_ulong
x11.XCreateSimpleWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong,
]
x11.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
x11.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
x11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

KeyPress, ButtonPress, ButtonRelease, MotionNotify, EnterNotify, MapNotify = 2, 4, 5, 6, 7, 19
MASK = (1 << 0) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 6) | (1 << 17)  # key, buttons, enter, motion, structure


class XInputEvent(ctypes.Structure):
    # the shared prefix of XKeyEvent, XButtonEvent, XMotionEvent and XCrossingEvent
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong), ("x", ctypes.c_int),
        ("y", ctypes.c_int), ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("detail", ctypes.c_uint),
    ]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("input", XInputEvent), ("pad", ctypes.c_long * 24)]


def main() -> None:
    log = open(sys.argv[1], "w", buffering=1)
    w, h = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (800, 600)
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        sys.exit("cannot open display")
    root = x11.XRootWindow(dpy, x11.XDefaultScreen(dpy))
    win = x11.XCreateSimpleWindow(dpy, root, 0, 0, w, h, 0, 0, 0xFFFFFF)
    x11.XStoreName(dpy, win, b"xmotion")
    x11.XSelectInput(dpy, win, MASK)
    x11.XMapWindow(dpy, win)
    x11.XFlush(dpy)
    ev = XEvent()
    while True:
        x11.XNextEvent(dpy, ctypes.byref(ev))
        e = ev.input
        if ev.type == MotionNotify:
            log.write(f"motion {e.x} {e.y}\n")
        elif ev.type in (ButtonPress, ButtonRelease):
            kind = "press" if ev.type == ButtonPress else "release"
            log.write(f"{kind} {e.detail} {e.x} {e.y}\n")
        elif ev.type == KeyPress:
            log.write(f"key {e.detail}\n")
        elif ev.type == EnterNotify:
            log.write(f"enter {e.x} {e.y}\n")
        elif ev.type == MapNotify:
            log.write("mapped\n")


if __name__ == "__main__":
    main()
