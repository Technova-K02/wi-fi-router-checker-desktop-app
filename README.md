# Router Checker

Windows 11 app that monitors several Wi-Fi routers, shows which one is most
stable, and warns when the current one becomes unstable. See [AGENTS.md](AGENTS.md)
for the full project brief and the decisions made so far.

## Setup

```powershell
uv sync
```

## Desktop app

```powershell
uv run router-checker-app                # no console window; lives in the tray
uv run python -m router_checker.ui       # same app with a console, for debugging
```

- The first start opens a 3-step setup: location access, your routers, the check interval.
- Closing the window keeps the app running in the tray. Exit from the tray menu
  (right-click the icon). Windows 11 may put the icon in the hidden-icons area (^);
  drag it onto the taskbar to keep it visible.
- Left-click the tray icon for the flyout, double-click to open the window.
- Shortcuts: F5 check now, Ctrl+1 to Ctrl+4 switch pages, Ctrl+N add a router.
- Starting the app again brings the running window to the front.

## Console harness

```powershell
uv run router-checker status             # adapter, connection, gateway, current router
uv run router-checker scan               # nearby networks, channel crowding, BSS Load
uv run router-checker check              # one full check (pings, DNS, score, verdict)
uv run router-checker watch              # checks on the timer (1 min while unstable)
uv run router-checker routers list
uv run router-checker routers add --name Home --ssid MyWiFi --mac B0-0A-D5-9A-7B-B4
uv run router-checker routers remove Home
uv run router-checker profiles           # Wi-Fi profiles saved in Windows
uv run router-checker location           # open the location privacy settings
```

Don't run `watch` while the app is open; both would write the same settings file.

## Data

Everything stays in `%LOCALAPPDATA%\RouterChecker`: `settings.json`,
`history.db` and `logs\app.log`. Both the app and the CLI accept `--data-dir`
to use another folder.

## Development

```powershell
uv run pytest
uv run ruff check
uv run ruff format
```
