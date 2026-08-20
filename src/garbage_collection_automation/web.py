"""The local web interface: the page in ``ui/``, served on the loopback interface.

The job itself is a cron run with no daemon; this is the one long-lived process
in the container. It hands out the handful of files the page is made of, and
answers the endpoints the page drives itself with - see :mod:`api`, which is
where those actually do anything. There is no login, and the page shows the
Todoist token, so it is never put on a network: the server binds a loopback
address and nothing else - see ``_host()`` in :mod:`configuration`. To reach it
from another machine, forward the port over ssh, which authenticates the person
the page has no way to.

One endpoint is unlike the rest: ``/api/stop`` switches ``[web] enabled`` off
and then ends this process. It exits 0, so the unit's ``Restart=on-failure``
leaves it stopped, and the config key is what keeps it stopped across a reboot.
It is the page putting itself away for the eleven months a year nobody needs it.

Having no login is exactly why the endpoints check where a request came from.
A page on any website the browser has open can post to 127.0.0.1 without being
able to read the answer, which is enough to press "apply delta" on someone
else's machine. ``_from_the_page()`` is the three cheap checks that stop it.

Everything is from the standard library. A page load is a few small files for a
single reader, so a framework and an application server would be more moving
parts than the thing they serve.

Run it with ``garbage-collection-automation-web``; ``scheduling/*-web.service``
is the systemd unit the installer puts in front of that.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import signal
import socket
import sys
import threading
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import (
    DEFAULT_CONFIG,
    DEFAULT_STATE,
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_WEB_ERROR,
    __version__,
    api,
    configuration,
    quiet_the_http_client,
)
from .configuration import Config

#: The page's own files: ``<repo>/ui`` in a checkout, ``<install dir>/ui`` once
#: installed - in both layouts the directory next to the ``src/`` this file is in.
DEFAULT_UI_DIR = Path(__file__).resolve().parents[2] / "ui"

INDEX = "index.html"

#: What the page is made of. An extension that is not here is not part of the
#: interface, so it is a 404 rather than a guess at its type.
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/vnd.microsoft.icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}

#: The page is self-contained: its own files and nothing from anywhere else. The
#: data: exception is the inline favicon in index.html.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

#: Sent with every response, without exception: ``_send()`` is the only way out
#: of this server, and ``send_error()`` below is what keeps the base class's own
#: refusals - an unknown method, an unparsable request line - going through it.
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # The page is local and changes when the container is upgraded; a stale
    # stylesheet costs more than re-reading a few kilobytes over loopback.
    "Cache-Control": "no-store",
}

#: Long enough to load a page over an ssh tunnel, short enough that a forgotten
#: connection does not hold a thread until the container is restarted.
CONNECTION_TIMEOUT = 30

#: How long a stop waits for a run that is already inside the pipeline; see
#: ``serve()``. Longer than a collect and an export take together, and well
#: inside the 90 seconds systemd gives a unit to stop before it sends SIGKILL.
SHUTDOWN_GRACE = 30

#: The endpoints the page drives itself with, and what each one is allowed to do.
#: A GET may only read; everything that runs the pipeline or writes a file is a
#: POST, so a link, a redirect or a prefetch can never set any of it going.
API_PREFIX = "/api/"

#: The largest form or body the page ever sends is a few hundred bytes. Anything
#: beyond this is not the page, and is refused before it is read into memory.
MAX_BODY_BYTES = 64 * 1024

#: A cross-origin form or <img> can be sent without any script; what it cannot do
#: is set this content type without a preflight, which this server never grants.
JSON_CONTENT_TYPE = "application/json"

log = logging.getLogger(__name__)


#: Which methods each endpoint answers. GET is for what only reads; the three
#: actions, the save and the stop are POST because they run the job, write a
#: file, or end the process.
ROUTES = {
    "state": frozenset({"GET"}),
    "collect": frozenset({"POST"}),
    "check": frozenset({"POST"}),
    "apply": frozenset({"POST"}),
    "config": frozenset({"POST"}),
    "stop": frozenset({"POST"}),
}

#: The one endpoint whose answer is the last thing this server says. See
#: ``_api()`` for the order it is said in, and ``api.stop_web()`` for the half
#: that happens before it.
STOP_ROUTE = "stop"

#: The three buttons, in the order the page has them and in order of how much
#: each one changes; see the table in :mod:`api`. The names are looked up on
#: :mod:`api` when the button is pressed, not bound here: a table of function
#: objects goes stale the moment anything wraps or replaces one of them.
ACTIONS = {"collect": "collect", "check": "check", "apply": "apply"}


class WebError(Exception):
    """The server could not be started - the port is taken, or the page is missing."""


class Handler(BaseHTTPRequestHandler):
    """Serves the files under one directory, plus the page's own endpoints.

    Only GET and HEAD reach a file. There is no directory listing, no path
    leaves the directory it is rooted in, and no extension outside
    ``CONTENT_TYPES`` is served at all. ``/api/`` is the other half; see
    ``_api()`` for the routes and ``_from_the_page()`` for who may call them.
    """

    protocol_version = "HTTP/1.1"
    timeout = CONNECTION_TIMEOUT
    server_version = f"garbage-collection-automation/{__version__}"
    sys_version = ""  # the interpreter version is nobody else's business

    def version_string(self) -> str:
        # The base class joins the two above with a space, which leaves a
        # trailing one once the interpreter version is gone.
        return self.server_version

    def __init__(self, *args, ui_dir: Path, paths: api.Paths, **kwargs):
        # Already resolved by create_server(): every request path is checked
        # against this, so it has to be the real directory, not a way to it.
        self.ui_dir = ui_dir
        self.paths = paths
        super().__init__(*args, **kwargs)

    # --- the request ---------------------------------------------------------

    def do_GET(self) -> None:
        self._respond(with_body=True)

    def do_HEAD(self) -> None:
        self._respond(with_body=False)

    def do_POST(self) -> None:
        path = self._request_path()
        if path is None:
            self._fail(HTTPStatus.BAD_REQUEST, "unreadable request path")
            return
        if not path.startswith(API_PREFIX):
            # Nothing outside /api/ takes a body; saying so beats the 501 the
            # base class would send, and beats a 404 that looks like a typo.
            self._fail(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "only the endpoints under /api/ take a post",
                headers={"Allow": "GET, HEAD"},
            )
            return
        self._api(path, method="POST", with_body=True)

    def _respond(self, *, with_body: bool) -> None:
        path = self._request_path()
        if path is None:
            self._fail(HTTPStatus.BAD_REQUEST, "unreadable request path")
            return

        if path == "/healthz":
            self._health(with_body=with_body)
            return

        if path.startswith(API_PREFIX):
            self._api(path, method="GET", with_body=with_body)
            return

        target = self._resolve(path)
        if target is None:
            self._fail(HTTPStatus.NOT_FOUND, "no such page")
            return

        try:
            body = target.read_bytes()
        except OSError as exc:
            # The files are installed read-only next to the code, so this is a
            # broken install rather than a bad request.
            log.error("cannot read %s: %s", target, exc)
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "the page could not be read")
            return

        self._send(HTTPStatus.OK, body, CONTENT_TYPES[target.suffix], with_body=with_body)

    def _health(self, *, with_body: bool) -> None:
        """Enough for systemd, a tunnel check or a browser tab to see it is alive."""
        body = json.dumps({"status": "ok", "version": __version__}).encode("utf-8")
        self._send(HTTPStatus.OK, body, "application/json", with_body=with_body)

    # --- the endpoints -------------------------------------------------------

    def _api(self, path: str, *, method: str, with_body: bool) -> None:
        """Route one ``/api/`` request, and answer JSON whatever happens.

        Every action reloads config.toml rather than using the one the process
        started with: the form writes that file, and the next button press must
        act on what was just saved. Only ``[web]`` is fixed at startup, because
        it is the socket that is already bound.
        """
        route = path[len(API_PREFIX) :].strip("/")
        allowed = ROUTES.get(route)
        if allowed is None:
            self._error(HTTPStatus.NOT_FOUND, f"no endpoint {path}")
            return
        if method not in allowed:
            self._error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                f"/api/{route} takes {' or '.join(sorted(allowed))}",
                headers={"Allow": ", ".join(sorted(allowed))},
            )
            return
        if not self._from_the_page(method):
            return

        payload = self._body() if method == "POST" else {}
        if payload is None:
            return

        try:
            body = self._act(route, payload)
        except configuration.ConfigError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"configuration: {exc}")
            return
        except api.ApiError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except api.Busy as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        except Exception:
            # A job that raises is a bug, not an answer; the page gets a sentence
            # and the traceback goes where the rest of the run's log went.
            log.exception("/api/%s failed", route)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "the action failed; see the log")
            return

        if route == STOP_ROUTE:
            # Nothing more will be answered on this connection, and a browser
            # holding it open would be waiting on a socket that is going away.
            self.close_connection = True

        self._json(HTTPStatus.OK, body, with_body=with_body)

        if route == STOP_ROUTE:
            # After the write, not before: serve() closes the listening socket
            # the moment this is set, and the page has to be told what it asked
            # for actually happened before it loses the way to ask anything.
            log.info("the page asked for the server to stop")
            self.server.stopping.set()

    def _act(self, route: str, payload: dict) -> dict:
        """Do what *route* names. Raises; ``_api()`` turns that into a status."""
        if route == "config":
            return api.save_config(payload, self.paths)
        if route == STOP_ROUTE:
            # Only the file half here. The server is stopped by _api() once this
            # answer is on the wire, since a stopped server cannot send one.
            return api.stop_web(self.paths)

        config = configuration.load(self.paths.config)
        if route == "state":
            return api.state_payload(config, self.paths)
        return getattr(api, ACTIONS[route])(config, self.paths)

    def _from_the_page(self, method: str) -> bool:
        """Whether this request can only have come from the page this server serves.

        There is no login, so this is what stands between the buttons and any
        website the browser happens to have open. Three cheap checks, and a
        request is refused unless it passes all of them:

        * ``Host`` must be the address the page is actually served on, which is
          what stops a hostname that resolves to 127.0.0.1 from being used as a
          way in - the classic DNS rebinding trick. A request with no ``Host``
          at all is refused too: HTTP/1.1 requires one and every browser sends
          one, so its absence is as good as a name we do not answer to.
        * ``Origin``, when the browser sends one, must be this server.
        * a POST must be ``application/json``, which a form, an image or a
          navigation cannot be without a preflight this server never answers.
        """
        host = self.headers.get("Host", "")
        if host not in self._own_names():
            log.warning("refused a request for host %r", host)
            self._error(HTTPStatus.FORBIDDEN, "this server is not reachable under that name")
            return False

        origin = self.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc not in self._own_names():
            log.warning("refused a request from origin %r", origin)
            self._error(HTTPStatus.FORBIDDEN, "requests from another site are not accepted")
            return False

        if method == "POST":
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type != JSON_CONTENT_TYPE:
                self._error(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    f"the endpoints take {JSON_CONTENT_TYPE}",
                )
                return False
        return True

    def _own_names(self) -> set[str]:
        """The Host values that mean this server: the address it bound, with and
        without the port. Nothing is added for a name, because it has none."""
        host, port = self.server.server_address[:2]
        literal = f"[{host}]" if ":" in host else host
        return {literal, f"{literal}:{port}"}

    def _body(self) -> dict | None:
        """The posted JSON object, or None once the failure has been answered."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "unreadable content-length")
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "that is not a request this page makes"
            )
            return None
        if length == 0:
            return {}

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"unreadable request body: {exc}")
            return None
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "the request body must be a json object")
            return None
        return payload

    def _json(
        self,
        status: HTTPStatus,
        body: dict,
        *,
        with_body: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._send(
            status,
            json.dumps(body).encode("utf-8"),
            "application/json",
            with_body=with_body,
            headers=headers,
        )

    def _error(
        self, status: HTTPStatus, message: str, headers: dict[str, str] | None = None
    ) -> None:
        """A refusal the page can show as it is, rather than the plain-text one.

        Everything under ``/api/`` answers with this, refusals included: the page
        reads one shape and one shape only, and a text/plain body in the middle
        of it is a parse error where an error message was meant to be.
        """
        self.close_connection = True
        self._json(
            status,
            {"error": message},
            with_body=self.command != "HEAD",
            headers=headers,
        )

    # --- the path ------------------------------------------------------------

    def _request_path(self) -> str | None:
        """The path asked for, without the query string, or None when it is unreadable."""
        try:
            path = unquote(urlsplit(self.path).path, errors="strict")
        except UnicodeDecodeError:
            return None
        # A NUL truncates a filename in the C layer below; never let one through.
        return None if "\0" in path else path

    def _resolve(self, path: str) -> Path | None:
        """The file *path* names, or None when it names anything else.

        ``..`` and a symlink out of the directory are the same mistake here, so
        the answer is what the path really resolves to, checked against the root.
        """
        relative = path.lstrip("/")
        if not relative or relative.endswith("/"):
            relative += INDEX

        target = (self.ui_dir / relative).resolve()
        if not target.is_relative_to(self.ui_dir):
            log.warning("refused a path outside the interface: %s", path)
            return None
        if target.suffix not in CONTENT_TYPES or not target.is_file():
            return None
        return target

    # --- the response --------------------------------------------------------

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        with_body: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # Always, and always accurate: it is what keeps a kept-alive connection
        # in step about where one response ends and the next begins.
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def _fail(self, status: HTTPStatus, reason: str, headers: dict[str, str] | None = None) -> None:
        """Answer plainly and hang up; a browser is the only client here."""
        self.close_connection = True
        self._send(
            status,
            f"{status.value} {status.phrase}: {reason}\n".encode(),
            "text/plain; charset=utf-8",
            with_body=self.command != "HEAD",
            headers=headers,
        )

    def send_error(self, code, message=None, explain=None) -> None:
        """Every refusal the base class makes on its own, answered the way we do.

        A PUT, an OPTIONS, a request line it cannot parse: the base
        class answers those itself, with an HTML page and not one of
        ``SECURITY_HEADERS`` on it, from a server whose whole contract is its own
        files or json. Nothing about the refusal changes here - the OPTIONS that
        stays a 501 is exactly what leaves a cross-site post unable to get the
        preflight it needs - only the words it is said in.
        """
        status = code if isinstance(code, HTTPStatus) else HTTPStatus(code)
        self._fail(status, message or explain or status.phrase)

    # --- logging -------------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base class names it
        log.info("%s %s", self.address_string(), format % args)

    def log_error(self, format: str, *args) -> None:  # noqa: A002 - the base class names it
        log.warning("%s %s", self.address_string(), format % args)


class Server(ThreadingHTTPServer):
    """The threading server, told which address family the configured host needs."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler):
        # Set before the socket is created: TCPServer builds it from this.
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        #: Set when this server should stop serving. Two things set it and they
        #: mean the same thing: a signal, and the page's own stop button through
        #: ``STOP_ROUTE``. ``serve()`` is what waits on it; a server built by
        #: ``create_server()`` alone has one nobody is watching.
        self.stopping = threading.Event()
        super().__init__(address, handler)


def create_server(config: Config, ui_dir: Path, paths: api.Paths | None = None) -> Server:
    """Bind the configured address and return the server, ready to serve.

    *paths* is where the endpoints read and write; it defaults to the installed
    locations, which is what the unit runs with.

    Raises ``WebError`` when the page is not where it should be, or when the
    address cannot be listened on.
    """
    ui_dir = ui_dir.resolve()
    if not (ui_dir / INDEX).is_file():
        raise WebError(f"the web interface is not installed: no {INDEX} in {ui_dir}")

    if paths is None:
        paths = api.Paths(config=DEFAULT_CONFIG, state=DEFAULT_STATE)

    web = config.web
    try:
        return Server((web.host, web.port), partial(Handler, ui_dir=ui_dir, paths=paths))
    except OSError as exc:
        raise WebError(f"cannot listen on {_address(web.host, web.port)}: {_why(exc)}") from exc


def serve(config: Config, *, ui_dir: Path | None = None, paths: api.Paths | None = None) -> int:
    """Serve until the process is asked to stop, and return the exit code.

    SIGTERM - what systemd sends - and SIGINT both mean: stop accepting, let a
    run that is already inside the pipeline finish writing what it started, then
    close the socket and return.

    The page's own stop button is the third way in, and it means the same thing;
    ``server.stopping`` is the one event all three set. It returns ``EXIT_OK``
    however it was asked, which is what leaves a unit with ``Restart=on-failure``
    stopped rather than started again a moment later.

    A connection that is merely open is dropped where it is. The threads serving
    them are daemons, so an idle tab holding one open cannot turn ``systemctl
    restart`` into a wait for ``CONNECTION_TIMEOUT``; what is worth waiting for
    is the half-applied export, and that is what is waited for.
    """
    root = (ui_dir if ui_dir is not None else DEFAULT_UI_DIR).resolve()
    server = create_server(config, root, paths)
    log.info(
        "serving %s on http://%s/ (SIGTERM, ctrl-c or the page's stop button)",
        root,
        _address(*server.server_address[:2]),
    )

    stop = server.stopping
    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, lambda number, frame: stop.set())

    # serve_forever() runs beside this thread rather than in it: shutdown() has
    # to be called from somewhere other than the loop it stops, and a signal
    # handler runs in the main thread.
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    try:
        stop.wait()
    finally:
        log.info("stopping")
        server.shutdown()
        if not api.wait_for_the_pipeline(SHUTDOWN_GRACE):
            log.warning("a run was still in progress after %ds; stopping anyway", SHUTDOWN_GRACE)
        thread.join(timeout=CONNECTION_TIMEOUT)
        server.server_close()
    return EXIT_OK


def _address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _why(exc: OSError) -> str:
    """The two ways a bind fails in practice, said in the words that help."""
    if exc.errno == errno.EADDRINUSE:
        return "something is already listening there"
    if exc.errno == errno.EACCES:
        return "not allowed to listen there; ports below 1024 need root"
    return str(exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garbage-collection-automation-web",
        description="Serve the local web interface for garbage-collection-automation.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="path to the configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
        help="path to the local record of what was exported (default: %(default)s)",
    )
    parser.add_argument(
        "--ui-dir",
        type=Path,
        default=DEFAULT_UI_DIR,
        help="directory holding the page (default: %(default)s)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``garbage-collection-automation-web`` console script."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        config = configuration.load(args.config)
    except configuration.ConfigError as exc:
        log.error("configuration: %s", exc)
        return EXIT_CONFIG_ERROR
    logging.getLogger().setLevel(config.logging.level)
    # A button press runs the same pipeline the cron job does, so it needs the
    # same silence from httpx: see quiet_the_http_client() for what would land in
    # the journal otherwise.
    quiet_the_http_client()

    if not config.web.enabled:
        # Not a failure: the unit is installed either way, and this is the switch.
        # Exiting 0 is what keeps systemd from restarting it in a loop.
        log.info("the web interface is switched off in %s ([web] enabled)", args.config)
        return EXIT_OK

    try:
        return serve(
            config,
            ui_dir=args.ui_dir,
            paths=api.Paths(config=args.config, state=args.state),
        )
    except WebError as exc:
        log.error("%s", exc)
        return EXIT_WEB_ERROR


if __name__ == "__main__":
    sys.exit(main())
