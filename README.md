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
.venv\Scripts\router-checker-app         # starts the app; the terminal is free right away
uv run router-checker-app                # same, but the terminal waits until you exit
uv run python -m router_checker.ui       # with a console for log output, for debugging
```

- The first start opens a 3-step setup: location access, your routers, the check interval.
- Closing the window keeps the app running in the tray. To stop it, click **Exit** on
  the dashboard (Ctrl+Q) or right-click the tray icon and choose Exit. Ctrl+C in the
  terminal doesn't reach it, because it's a windowed app without a console.
- Windows 11 may put the tray icon in the hidden-icons area (^); drag it onto the
  taskbar to keep it visible.
- Left-click the tray icon for the flyout, double-click to open the window.
- Shortcuts: F5 check now, Ctrl+T Test all, Ctrl+1 to Ctrl+4 switch pages, Ctrl+N add a
  router, Ctrl+Q exit.
- **Test all now** (dashboard, tray menu, Ctrl+T) connects to each of your routers in
  turn, tests it, then reconnects to the network you were on. It asks first and lists
  what it will test and what it skips, and why. It only uses Wi-Fi profiles Windows has
  already saved: connect to a router once from the taskbar so Windows keeps its
  password. Your internet drops for a few seconds at each switch. Cancel or Exit at
  any time; the original connection is always restored. It needs location access.
- Settings > Scheduled Test all (off by default) runs it every few hours, but only after
  5 minutes without keyboard or mouse input.
- When another router scores clearly better, the dashboard and the "unstable" alert
  offer **Switch to** that router. If the switch fails, the app goes back.
- Alerts are Windows notifications with buttons (Open, Check now). They name a
  better router when one scores clearly higher. Settings has quiet hours (off by
  default), a test notification, and a link to Windows' notification settings.
- Router details show latency, packet loss and score charts (1 h / 24 h / 7 d) and
  the router's popular times; History compares every router's score. Hover a chart
  for the values at that time; the heatmap reads out each hour with the arrow keys.
- Settings > Export history saves every kept check as a CSV file.
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
uv run router-checker profiles           # saved Wi-Fi profiles and their Wi-Fi names
uv run router-checker location           # open the location privacy settings
```

Don't run `watch` while the app is open; both would write the same settings file.

## Data

Everything stays in `%LOCALAPPDATA%\RouterChecker`: `settings.json`,
`history.db`, `logs\app.log` and `app-icon.png` (the icon notifications show).
While Test all is switched away from your network, `test-all-restore.json` names that
network, so the next start can go back if the app was closed in the middle.
Both the app and the CLI accept `--data-dir` to use another folder.

The only thing written elsewhere is the notification sender registration,
`HKCU\Software\Classes\AppUserModelId\RouterChecker.RouterChecker` (no admin
rights needed), so notifications show "Router Checker" and its icon.

## Development

```powershell
uv run pytest
uv run ruff check
uv run ruff format
```
