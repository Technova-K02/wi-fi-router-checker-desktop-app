"""The HTTP client for the middle router, against a small local web server."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from fakes import mac
from router_checker.core.middle import Endpoint
from router_checker.platform_windows.middle_http import HttpMiddleRouter


class Handler(BaseHTTPRequestHandler):
    status = 200
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        Handler.paths.append(self.path)
        self.send_response(Handler.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def server():
    Handler.status, Handler.paths = 200, []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield Endpoint("127.0.0.1", httpd.server_address[1])
    httpd.shutdown()
    httpd.server_close()


def test_change_router_sends_the_mac(server) -> None:
    HttpMiddleRouter().change_router(server, mac("B0-0A-D5-9A-7B-B8"))
    assert Handler.paths == ["/change_router?router=b0:0a:d5:9a:7b:b8"]


def test_an_error_answer_is_an_oserror(server) -> None:
    Handler.status = 404
    with pytest.raises(OSError, match="HTTP 404"):
        HttpMiddleRouter().change_router(server, mac("B0-0A-D5-9A-7B-B8"))
    assert HttpMiddleRouter().reachable(server) is None  # it did answer


def test_nothing_listening() -> None:
    closed = Endpoint("127.0.0.1", 9)  # the discard port: nothing listens on it here
    with pytest.raises(OSError):
        HttpMiddleRouter().change_router(closed, mac("B0-0A-D5-9A-7B-B8"))
    assert HttpMiddleRouter().reachable(closed) is not None
