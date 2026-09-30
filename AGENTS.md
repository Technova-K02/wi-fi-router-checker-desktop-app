# Router Checker: project brief

## Goal
A Windows 11 desktop app that monitors several Wi-Fi routers I have access to, shows which one is most stable, and warns me when the router I'm using becomes unstable.

## Environment and constraints
- Windows 11 desktop with ONE Wi-Fi adapter (no USB/second adapter), so it can be connected to only one router at a time.
- Assumption (confirm with me): the routers are separate Wi-Fi networks (different SSIDs), each with its own internet connection.
- Windows 11 24H2+: WlanGetAvailableNetworkList, WlanGetNetworkBssList, WlanScan and WlanQueryInterface(current_connection) return ERROR_ACCESS_DENIED unless location access for desktop apps is allowed. Detect this, explain it in the UI, add a button that opens ms-settings:privacy-location, and degrade gracefully (the current router can still be identified by the gateway MAC from the ARP table).
- No admin rights and no Developer Mode needed. Never store Wi-Fi passwords: only use Wi-Fi profiles Windows has already saved. Never log in to router admin pages. Only ping the targets I configure. All data stays local in %LOCALAPPDATA%\RouterChecker.
- Don't parse the text output of netsh, ping or ipconfig, because it changes with the Windows display language. Call the Windows APIs directly through ctypes.

## Tech stack
- Python 3.13, managed with uv (pyproject.toml + uv.lock).
- UI: PySide6 with PySide6-Fluent-Widgets for the Windows 11 Fluent look. Charts: pyqtgraph. Tray icon: QSystemTrayIcon.
- Notifications: the windows-toasts package (Windows notifications with buttons).
- Wi-Fi: Native Wifi API (wlanapi.dll) through ctypes: scan, BSS list with raw information elements, current connection, and connect with a saved profile.
- Ping: Windows ICMP API (IcmpCreateFile / IcmpSendEcho2 in iphlpapi.dll) through ctypes, so no admin rights are needed. Gateway IP and gateway MAC: GetAdaptersAddresses and GetIpNetTable2 (iphlpapi.dll) through ctypes.
- DNS timing: socket.getaddrinfo measured with time.perf_counter.
- Storage: sqlite3 (standard library) for history; JSON for settings.
- Threading: all network work runs off the UI thread (QThreadPool workers); results reach the UI only through Qt signals.
- Tests: pytest. Lint and format: ruff.
- Packaging (phase 6): Nuitka or PyInstaller to build the .exe, plus an Inno Setup installer.

## Project layout
- src/router_checker/core/: pure Python, NO Windows or Qt imports. Models, MAC parsing, measurements, scoring, alert rules, recommendation, popularity, scheduling, and interfaces as typing.Protocol classes (WifiService, PingService, NetworkInfoService, NotificationService, RouterSwitcher, HistoryStore, Clock).
- src/router_checker/platform_windows/: ctypes implementations of those interfaces.
- src/router_checker/ui/: PySide6 app (main window, tray icon, flyout).
- src/router_checker/cli.py: console harness to run checks without the UI.
- tests/: pytest tests for core (they must run on any OS).

## Routers
- A router has a name, a color, an SSID and one or more MAC addresses. Its Wi-Fi MACs (BSSIDs, one per band) and its LAN MAC (what `arp -a` shows for the gateway) often differ in the last bytes. When connected, link the gateway MAC to the router matched by SSID/BSSID automatically.
- Accept MACs as B0-0A-D5-9A-7B-B4, b0:0a:d5:9a:7b:b4 or b00ad59a7bb4. Display them uppercase with dashes.
- First router for testing: B0-0A-D5-9A-7B-B4 (ZTE, LAN MAC of my gateway). I'll add the others in the app.

## Checks
- Timer: every 5 min by default (1/5/10/15/30 selectable) plus "Check now". While the current router is unstable, check every 1 min.
- Current router (full test): 10 pings to the gateway and 10 to each target (defaults 1.1.1.1, 8.8.8.8, google.com; editable). Measure average, median, p95, jitter (mean absolute difference between consecutive RTTs), packet loss %, and DNS lookup time for domain targets.
- Other routers (passive, no connection change): Wi-Fi scan for visibility, signal quality/RSSI, band, channel, number of networks on the same channel, and the BSS Load element (IE 11: station count, channel utilization) when broadcast.
- Router states: Online (tested OK), Visible (in range, not tested), Not found, Unknown (no location permission).
- Router or internet: if the gateway answers but all targets fail, report an internet provider problem, not a router problem.
- "Test all now" (manual, after a confirmation that the connection will switch briefly): for each router with a saved Windows profile, connect, wait for an IP and gateway (20 s timeout), run the full test, then reconnect to the original router. ALWAYS restore the original connection, including after errors or cancel. Optional scheduled test-all: off by default; when on, it runs only after the user has been idle for 5+ min.

## Scoring, alerts, recommendation, popularity
- Stability score 0-100 over a rolling 60-min window, recent checks weighted more: packet loss 40%, jitter 25%, latency 20%, signal 15%. Labels: 80+ Excellent, 60-79 Good, 40-59 Fair, under 40 Poor. Routers without a recent full test get an "estimated" score from signal, crowding and history. Keep weights and thresholds as named constants and document the formula.
- Unstable (defaults, editable in Settings): loss of 5% or more, gateway p95 over 100 ms, jitter over 30 ms, or all targets failing. Notify after 2 unstable checks in a row, then a 15-min cooldown per router. Send "back to normal" after 2 good checks.
- Recommendation: best score among routers with data under 30 min old. Show "Recommended" only if it beats the current router by 10+ points.
- Popularity (assumption, confirm with me: it means how busy the router is): Low/Medium/High from BSS Load when available, otherwise estimated from channel crowding and jitter history. Store hourly aggregates for a "popular times" view (7 days x 24 hours).
- Auto switching: NOT now. Define a RouterSwitcher protocol and a switching-policy protocol so it can be added later (planned rules: better for 3 checks in a row, 30-min cooldown, switch back if the new router fails).

## UI/UX
- Windows 11 Fluent look through PySide6-Fluent-Widgets: FluentWindow with navigation (Dashboard, Routers, History, Settings), Mica effect on Windows 11, Fluent icons, follows system light/dark.
- Tray icon whose color shows the current status (green/yellow/red/grey), with a small flyout: current router, score, gateway ping, internet ping, recommended router, "Check now", "Open". Closing the window hides it to the tray; "Exit" is in the tray menu.
- Dashboard: large card for the current router (score ring, gateway ping, internet ping, loss, signal); one card per other router (state, signal, busy level, sparkline, Recommended badge); last check time with countdown; "Test all now" button.
- Router details: latency and loss charts (1 h / 24 h / 7 d), popular-times heatmap, event log.
- Add router: pick from nearby networks (fills SSID and MAC) or type a MAC (validated); set a name and color.
- First run: 3 steps: allow location, add routers, choose the interval.
- Settings: interval, targets, thresholds, notifications and quiet hours, start with Windows, scheduled test-all, history retention (30 days), CSV export.
- Status is never shown by color alone (icon + text). Full keyboard navigation. The UI never freezes.

## Phases (one at a time; each ends with something I can run)
1. Core engine, Windows services (ctypes) and console harness (no UI).
2. Desktop app: main window, tray and flyout, dashboard, router details, add router, settings, first run.
3. Notifications, recommendation, popularity and popular times, history charts, CSV export.
4. "Test all now" and optional scheduled test-all.
5. Auto switching.
6. Polish: app icon, .exe build, installer, start with Windows (registry Run key under HKCU).

## Working rules
- Before each phase, reply with a short plan (modules, packages, key classes, open questions) and wait for my OK.
- Keep core free of Windows and Qt imports and unit-test all its logic.
- Run `uv run pytest` and `uv run ruff check` before saying a task is done, and report the real results.
- Use git with small commits and clear messages.
- If something is unclear, ask instead of guessing.

## Decisions log (agreed in chat; newest last)
- Phase 1 (2026-09-29): the routers are separate Wi-Fi networks (different SSIDs), each with its own internet connection. "Popularity" means how busy a router is.
- Scoring, "for now": loss 0% scores 100 and 20% or more scores 0; jitter 0 ms to 50 ms; latency 20 ms to 200 ms (median of the internet targets' medians); RSSI -50 dBm to -85 dBm; recent checks weigh more with a 30-min half-life. Gateway p95 is only used by the unstable rule.
- The jitter rule uses the worse of gateway jitter and the targets' mean jitter. A gateway that never answers ping while the internet works counts as "silent" and its loss is ignored.
- Scans also match a BSSID that differs from a router's MAC only in the last byte (lowest priority). Targets are IPv4 addresses or host names; no IPv6 yet.
- Git author for this repo: talent <talent@email.com> (repo-local config).
- Phase 2 (2026-09-29): personal use for now, so PySide6-Fluent-Widgets under GPLv3 is fine. Revisit before handing the app to others.
- Status colors, always with a glyph and text: green = stable (score 60+); yellow = one unstable check not yet confirmed, recovering, or score under 60; red = unstable 2 checks in a row, router not responding, or internet provider problem; grey = not connected, unknown network, no data, or the check failed.
- Re-check soon after Windows switches networks (route-change notification, 5 s settle, only if the gateway IP/MAC changed). On by default, with an on/off switch in Settings. The user asked for that switch, so give other automatic behaviors one too.
- Until Phase 3 brings windows-toasts, alerts use the tray icon's basic notifications.
- Exit (2026-09-30): besides the tray menu, an Exit button on the dashboard and Ctrl+Q. Exiting stops a running check at once (the process ends within about a second); nothing from a stopped check is saved.
- Phase 3 (2026-09-30): toasts come from windows-toasts under the app's own identity: the app registers `HKCU\Software\Classes\AppUserModelId\RouterChecker.RouterChecker` (DisplayName, IconUri). That is the only thing written outside %LOCALAPPDATA%; the Phase 6 uninstaller removes it. Tray balloons stay as the fallback.
- Toast buttons: unstable, not responding or internet problem get Open and Check now; back to normal gets Open; clicking a toast opens the app. Alerts name the recommended router when there is one. "Switch to <recommended>" comes with the connect code in Phase 4.
- Quiet hours: off by default, 22:00-07:00 when turned on, every day. No toasts then, but the tray icon, dashboard and event log keep updating; no summary afterwards.
- CSV export: one file with every kept check (local time, router, Wi-Fi name, result, reasons, gateway and internet ping numbers, DNS time, signal, score), UTF-8 with BOM, comma-separated; no events or scans. Text cells starting with = + - @ get a leading ' so spreadsheets don't run them as formulas.
- Charts: router details show latency, packet loss and score (1 h / 24 h / 7 d) and popular times (busyness by local weekday and hour, averaged over the kept history). The History page compares the score of every router.
- Phase 4 (2026-09-30): Test all switches only to routers that have a Wi-Fi name (or one learned from a known BSSID), a profile Windows saved (matched by the SSID inside the profile, so renamed profiles work; only that SSID is read, never the key) and that were in the last scan. The router you're on is tested where it is, by the check that ends every run. Skipped routers are listed with the reason in the confirmation.
- Test all needs location access (to know where to go back to); without it the button is disabled. Per router: connect, wait up to 20 s for the connection, a usable IP address and a gateway, pause 2 s, then the full test. It's saved like a check and linked MACs are learned, but a router you aren't using never alerts.
- Restoring: always, with 2 tries of 20 s. Cancel stops after the current step. Exit only asks Windows to reconnect, so the app still closes within about a second. Wi-Fi not connected before means disconnected after. While switched away, `test-all-restore.json` in the data folder names the original network; if it's still there at the next start (within 2 hours) and the PC is on one of the tested networks, the app goes back.
- During Test all or a switch, the check timer and "check when the network changes" pause; each run ends with a regular check.
- Scheduled Test all: off by default; every 1, 2, 4 or 8 hours (default 2); only after 5 minutes without keyboard or mouse input, and only when on Wi-Fi. Silent unless the connection couldn't be restored. The last run is taken from the event log.
- Switch button: "Switch to <recommended>" on the dashboard's recommendation line and on unstable alerts, only when that router can be switched to. If the switch fails, the app goes back to the original network. Results show in the window; a connection that couldn't be restored is also a notification (outside quiet hours). Ctrl+T starts Test all.
