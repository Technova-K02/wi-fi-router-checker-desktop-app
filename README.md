# Router Checker

Windows 11 app that monitors several Wi-Fi routers, shows which one is most
stable, and warns when the current one becomes unstable. See [AGENTS.md](AGENTS.md)
for the full project brief and the decisions made so far.

## Setup

```powershell
uv sync
```

Without uv, `requirements.txt` pins the same packages (app, tests, lint and build):

```powershell
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
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
- Settings > Switch automatically (off by default) moves you to a router that Test all
  measured as clearly better on 3 checks in a row, or right away when yours has been
  down for 2 checks. It pauses 30 minutes after any switch, and goes back if the new
  router fails its first check. The notification has a **Go back** button.
- Alerts are Windows notifications with buttons (Open, Check now). They name a
  better router when one scores clearly higher. Settings has quiet hours (off by
  default), a test notification, and a link to Windows' notification settings.
- Router details show latency, packet loss and score charts (1 h / 24 h / 7 d) and
  the router's popular times; History compares every router's score. Hover a chart
  for the values at that time; the heatmap reads out each hour with the arrow keys.
- Settings > Export history saves every kept check as a CSV file.
- **Ethernet**: on a cable, the app checks the router you're plugged into, found by
  its LAN MAC. Add it with **Use the router I'm connected to** in Add router, or
  **Add it** on the dashboard. The score leaves out Wi-Fi signal. Test all, Switch to
  and Switch automatically are off while you're wired, since your traffic goes over
  the cable anyway. A PC without Wi-Fi works too (no location step, no scans).
- **Middle router**: if your PC's cable goes into a router of your own that joins one
  of your routers over Wi-Fi, and that router switches with
  `http://<address>:<port>/change_router?router=<Wi-Fi MAC>`, enter its address and
  port in Settings > Middle router (**Test** checks that it answers). The app then
  finds the router in use by a ping that stops at the second hop, pings that router
  as "the router" (and the middle router separately), and Test all, Switch to and
  Switch automatically ask the middle router to switch. Each router's address
  there is learned the first time the app switches to it, or typed in Add router.
  Switching moves every device behind the middle router, not only this PC.
- Settings > Connection: Automatic (the connection Windows uses for the internet),
  Wi-Fi only or Ethernet only. Pings always leave through the checked connection,
  so a cable doesn't carry the pings meant for Wi-Fi.
- **VPN**: the app checks the Wi-Fi or Ethernet connection underneath, pinging past
  the VPN. If the VPN blocks that, the internet targets are pinged through the VPN
  instead (the dashboard says so) rather than reporting an internet provider problem.
  DNS lookups use whatever Windows uses, so they go through the VPN.
- Settings > Start with Windows opens the app in the tray when you sign in. Task
  Manager's Startup apps page shows it too, and turning it off there shows here.
- Starting the app again brings the running window to the front.

## Installer and one-file .exe

```powershell
winget install JRSoftware.InnoSetup      # once; for the installer (no admin rights)
uv run --group build python packaging/build.py              # exe and installer
uv run --group build python packaging/build.py --exe-only   # just the exe
```

This builds `dist\RouterChecker.exe` (about 70 MB): the desktop app as a single file
that runs without Python or admin rights. Copy it anywhere and start it; it uses the
same data folder as `uv run`, so settings and history carry over. Each start unpacks
it to a temporary folder first, so it takes a few seconds to open. The console harness
below isn't part of it.

With Inno Setup 6 installed, it also builds `dist\RouterChecker-<version>-setup.exe`:

- Installs for you only, without admin rights, into
  `%LOCALAPPDATA%\Programs\Router Checker`, with a Start menu shortcut and an entry in
  Settings > Apps. A first install offers "Start Router Checker when I sign in" (on)
  and a desktop shortcut (off).
- Installing a newer version over it closes the running app first
  (`RouterChecker.exe --exit`) and keeps your settings and Start with Windows choice.
- Uninstalling closes the app and removes its files, shortcuts, the Start with Windows
  entry and the notification registration. It asks whether to delete your routers,
  settings and history too (default: keep them); a silent uninstall keeps them.

Neither file is signed, so Windows SmartScreen may warn the first time ("More info" >
"Run anyway").

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
network (`middle-restore.json` the router, behind a middle router), so the next start
can go back if the app was closed in the middle.
Both the app and the CLI accept `--data-dir` to use another folder.

Outside that folder the app writes only to the current user's registry (no admin
rights): the notification sender registration,
`HKCU\Software\Classes\AppUserModelId\RouterChecker.RouterChecker`, so notifications
show "Router Checker" and its icon; and, when Start with Windows is on, the value
`RouterChecker` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. The
uninstaller removes both.

## Development

```powershell
uv run pytest
uv run ruff check
uv run ruff format
```
