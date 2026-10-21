# hypruse-sandbox

A private, nested Hyprland desktop for an AI agent, driven with
[hypruse](https://github.com/IlyasKhallouki/hypruse). The agent clicks any
pixel, drags, types and takes screenshots in its own compositor, with its
own cursor, keyboard focus and D-Bus session. Your cursor, focus and
workspaces are never touched.

Use it for visual tests of games, canvas apps, Chromium/Electron and egui:
apps with no accessibility tree, which hypruse's background mode cannot
drive.

## How it works

`hypruse-sandbox up` asks your Hyprland to run a second Hyprland as an
ordinary window:

- The window goes on the special workspace `special:hypruse`, opened
  silently.
- The `render_unfocused` rule keeps it drawing while hidden.
- It starts with its own short runtime directory, `$XDG_RUNTIME_DIR/hus-<name>`,
  and its own D-Bus session bus.

hypruse, grim, wtype and hyprctl then target the sandbox through three
variables: `WAYLAND_DISPLAY`, `HYPRLAND_INSTANCE_SIGNATURE` and
`XDG_RUNTIME_DIR`.

Stable symlinks (`hypr/sandbox`, `wayland-sandbox`) keep those values the
same across restarts. This lets an MCP server start before its sandbox
exists.

No compositor plugin is needed, and nothing changes on your monitors.

| | |
|---|---|
| Isolated | cursor, keyboard focus, workspaces, windows, XWayland display, D-Bus session (portals, notifications, the accessibility registry), audio (off unless `--audio`) |
| Not isolated | the filesystem and your home directory; an app that is already running for you (a browser on the same profile) hands its new window to that instance, on your screen. Give such apps their own profile. |

## Use

```sh
hypruse-sandbox up [--size 1920x1080] [--audio]   # idempotent
hypruse-sandbox run ./my-game                    # any command, inside the sandbox
hypruse-sandbox run hypruse screenshot --out shot.png
eval "$(hypruse-sandbox env)"                     # or point a whole shell at it
hypruse-sandbox show / hide                       # watch it on your focused monitor
hypruse-sandbox status
hypruse-sandbox down                              # stops everything started in it
```

Every command takes `--name NAME` (1 to 4 of `a-z0-9`, default `main`).

## As an MCP server

```sh
claude mcp add --scope user hypruse-sandbox -- hypruse-sandbox mcp
```

Each MCP server process claims its own numbered sandbox. The sandbox:

- starts on the first tool call (a few seconds);
- goes down when the session ends;
- is never started at all for a session that does not use it.

The proxy prepends a short note to hypruse's server instructions. The note
tells the agent that it is in a sandbox, gives the sandbox's name, and says
the shared-seat warnings do not apply.

Hyprland's `render_unfocused` caps a hidden window at
`misc:render_unfocused_fps` (15 by default). Run `hypruse-sandbox show`
for full speed while you watch.

## Requirements

- Hyprland 0.56+ (the sandbox config is written in Lua).
- A working GPU EGL stack: the nested compositor needs it, like any GL app.
- grim, wtype, dbus-daemon, hypruse.

## Tests

```sh
python3 -m pytest -q -p no:cacheprovider tests/
```
