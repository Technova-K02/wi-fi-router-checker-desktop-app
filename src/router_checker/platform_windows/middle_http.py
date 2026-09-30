"""Talking to your middle router over plain HTTP (see ``core.middle``): only its
switch URL, and a request to see whether it answers. No login, no proxy (it's on
your own network), short timeouts."""

from __future__ import annotations

import urllib.error
import urllib.request

from router_checker.core.mac import MacAddress
from router_checker.core.middle import Endpoint

TIMEOUT_S = 5.0

# Proxies are for the internet; the middle router is on your own network.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HttpMiddleRouter:
    def change_router(self, endpoint: Endpoint, bssid: MacAddress) -> None:
        """Raises OSError when the middle router can't be reached or doesn't answer 2xx."""
        try:
            with _opener.open(endpoint.change_url(bssid), timeout=TIMEOUT_S) as response:
                response.read(4096)  # let it finish; the text isn't needed
        except urllib.error.HTTPError as exc:
            raise OSError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(str(exc.reason)) from exc

    def reachable(self, endpoint: Endpoint) -> str | None:
        """None if anything answers at the address (any HTTP status), else why not."""
        try:
            with _opener.open(f"http://{endpoint}/", timeout=TIMEOUT_S):
                return None
        except urllib.error.HTTPError:
            return None  # it answered, just not with a page
        except urllib.error.URLError as exc:
            return str(exc.reason)
        except OSError as exc:  # e.g. a timeout while reading
            return str(exc)
