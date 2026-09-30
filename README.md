# Router Checker

Windows 11 app that monitors several Wi-Fi routers, shows which one is most
stable, and warns when the current one becomes unstable. See [AGENTS.md](AGENTS.md)
for the full project brief.

## Setup

```powershell
uv sync
```

## Console harness (Phase 1)

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

Data lives in `%LOCALAPPDATA%\RouterChecker` (`settings.json`, `history.db`);
use `--data-dir` to point somewhere else.

## Development

```powershell
uv run pytest
uv run ruff check
uv run ruff format
```
