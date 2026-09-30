"""Exceptions shared between core and the platform services."""


class RouterCheckerError(Exception):
    """Base class for Router Checker errors."""


class LocationPermissionError(RouterCheckerError):
    """Windows refused Wi-Fi scan or connection details.

    On Windows 11 24H2+ the Native Wifi scan/BSS/connection APIs return
    ERROR_ACCESS_DENIED unless "Let desktop apps access your location" is on.
    """


class WifiUnavailableError(RouterCheckerError):
    """No usable Wi-Fi adapter (missing, radio off, or WLAN service stopped)."""


class CheckCancelled(RouterCheckerError):
    """The check was stopped (the app is exiting) before anything from it was saved."""
