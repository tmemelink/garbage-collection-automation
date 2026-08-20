"""The local web interface: what it serves, what it refuses, and how it stops.

Every server here binds port 0, so the kernel picks a free port and two test
runs can never collide. Nothing leaves the loopback interface, which is also the
only place the server will bind at all.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

import garbage_collection_automation as gca
from garbage_collection_automation import api, configuration, data_collection, data_processing, web
from garbage_collection_automation.configuration import WebConfig

from .conftest import REPO_ROOT, make_config
from .test_api import MINIMAL_CONFIG
from .test_application import SCHEDULE
from .test_reconciliation import TODAY


def config(**web_settings):
    """A configuration whose only interesting part is [web]."""
    return make_config(web=WebConfig(enabled=True, port=0, **web_settings))


@pytest.fixture
def ui_dir(tmp_path):
    """A stand-in for ui/: the same shape, none of the real page's content."""
    ui = tmp_path / "ui"
    (ui / "static").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>the page</title>\n")
    (ui / "static" / "app.css").write_text(".panel { color: red }\n")
    return ui


@pytest.fixture
def paths(tmp_path):
    """Where this server's endpoints read and write; a checkout has no crontab."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(MINIMAL_CONFIG)
    return api.Paths(
        config=config_path,
        state=tmp_path / "state.json",
        cron=tmp_path / "no-such-crontab",
    )


@pytest.fixture
def server(ui_dir, paths):
    """A running server. Its ``stopping`` event is what a stop request sets."""
    server = web.create_server(config(), ui_dir, paths)
    thread = threading.Thread(target=server.serve_forever, name="test-http", daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def client(server):
    """A client pointed at it."""
    host, port = server.server_address[:2]
    with httpx.Client(base_url=f"http://{host}:{port}", timeout=5) as client:
        yield client


# --- what it serves -------------------------------------------------------------------


def test_the_root_is_the_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "the page" in response.text


def test_the_page_gets_its_own_files(client):
    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/css; charset=utf-8"


def test_a_head_asks_the_same_question_without_the_answer(client):
    response = client.head("/")

    assert response.status_code == 200
    assert response.content == b""
    assert int(response.headers["content-length"]) > 0


def test_there_is_something_a_tunnel_can_be_checked_against(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": gca.__version__}


def test_the_real_page_is_where_the_server_looks_for_it():
    """The checkout and the install both put ui/ next to src/; nothing else has to agree."""
    assert web.DEFAULT_UI_DIR == REPO_ROOT / "ui"
    assert (web.DEFAULT_UI_DIR / "index.html").is_file()


def test_the_real_page_and_everything_it_asks_for_is_servable():
    """A file the page references but the server would refuse is a broken page."""
    referenced = {"index.html", "static/app.css", "static/app.js"}

    for name in referenced:
        path = web.DEFAULT_UI_DIR / name
        assert path.is_file(), f"the page needs {name}"
        assert path.suffix in web.CONTENT_TYPES, f"the server would not serve {name}"


# --- what it refuses ------------------------------------------------------------------


def test_a_path_that_climbs_out_of_the_interface_is_not_served(client, ui_dir):
    (ui_dir.parent / "secret.txt").write_text("the token\n")

    # Percent-encoded, or the client would resolve the dot segments before sending.
    response = client.get("/%2e%2e/secret.txt")

    assert response.status_code == 404
    assert "the token" not in response.text


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_symlink_out_of_the_interface_is_not_served(client, ui_dir):
    (ui_dir.parent / "secret.txt").write_text("the token\n")
    (ui_dir / "escape.txt").symlink_to(ui_dir.parent / "secret.txt")

    response = client.get("/escape.txt")

    assert response.status_code == 404
    assert "the token" not in response.text


def test_a_file_that_is_not_part_of_the_interface_is_not_served(client, ui_dir):
    """The interface is html, css, js and images - not whatever else lands in the directory."""
    (ui_dir / "config.toml").write_text("token = 'no'\n")

    response = client.get("/config.toml")

    assert response.status_code == 404


def test_there_is_no_directory_listing(client):
    assert client.get("/static/").status_code == 404
    assert client.get("/static").status_code == 404


def test_a_page_that_does_not_exist_says_so(client):
    response = client.get("/nope.html")

    assert response.status_code == 404


def test_only_the_endpoints_take_a_post(client):
    """A post to a file is a mistake; it must be told, not left to guess."""
    response = client.post("/", json={"postcode": "1234AB"})

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


# --- how it answers -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/nope.html"])
def test_every_answer_carries_the_security_headers(client, path):
    headers = client.get(path).headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


@pytest.mark.parametrize("method", ["PUT", "DELETE", "OPTIONS", "TRACE"])
def test_a_method_this_server_does_not_have_is_refused_in_its_own_words(client, method):
    """The base class answers these itself - in html, and without a single header."""
    response = client.request(method, "/")

    assert response.status_code == 501
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_a_preflight_is_the_refusal_the_endpoints_are_counting_on(client):
    """The json content type keeps a cross-site post out only while OPTIONS says no."""
    response = client.options(
        "/api/apply",
        headers={
            "Origin": "https://an.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 501
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-headers" not in response.headers


def test_the_page_is_allowed_to_load_its_own_files_and_its_inline_icon(client):
    """A policy the page itself trips over would be found in a browser, not here."""
    policy = client.get("/").headers["content-security-policy"]

    assert "default-src 'self'" in policy
    assert "img-src 'self' data:" in policy  # index.html has an inline svg favicon


def test_the_server_does_not_announce_the_interpreter(client):
    server = client.get("/").headers["server"]

    assert server == f"garbage-collection-automation/{gca.__version__}"
    assert "Python" not in server


# --- starting and stopping ------------------------------------------------------------


def test_an_interface_that_is_not_installed_is_reported_as_such(tmp_path):
    with pytest.raises(web.WebError, match="not installed"):
        web.create_server(config(), tmp_path / "absent")


def test_a_port_that_is_taken_is_reported_in_the_words_that_help(ui_dir):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]

        with pytest.raises(web.WebError, match="already listening"):
            web.create_server(make_config(web=WebConfig(enabled=True, port=port)), ui_dir)


def test_the_configured_loopback_address_is_the_one_it_listens_on(ui_dir):
    server = web.create_server(config(host="127.0.0.1"), ui_dir)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.address_family == socket.AF_INET
    finally:
        server.server_close()


def test_an_ipv6_loopback_address_is_understood(ui_dir):
    server = web.create_server(config(host="::1"), ui_dir)
    try:
        assert server.address_family == socket.AF_INET6
    finally:
        server.server_close()


# --- the command line -----------------------------------------------------------------


def test_a_switched_off_interface_is_not_a_failure(write_config, caplog):
    """systemd restarts a unit that fails; being switched off is not that."""
    path = write_config('[address]\npostcode = "1234AB"\nhouse_number = "56"\n')

    with caplog.at_level("INFO"):
        assert web.main(["--config", str(path)]) == gca.EXIT_OK

    assert "switched off" in caplog.text


def test_a_missing_page_ends_the_process_rather_than_serving_nothing(write_config, tmp_path):
    path = write_config(
        '[address]\npostcode = "1234AB"\nhouse_number = "56"\n[web]\nenabled = true\n'
    )

    assert web.main(["--config", str(path), "--ui-dir", str(tmp_path / "absent")]) == (
        gca.EXIT_WEB_ERROR
    )


def test_a_bad_config_is_reported_the_way_the_job_reports_it(write_config, caplog):
    path = write_config("[address]\npostcode = 'nope'\nhouse_number = 1\n")

    assert web.main(["--config", str(path)]) == gca.EXIT_CONFIG_ERROR
    assert "postcode" in caplog.text


def test_the_http_client_does_not_narrate_a_button_press_either(write_config):
    """The same reason the cron job quiets it: build_url() puts the api key in the
    query string, and httpx logs the whole url at INFO."""
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    path = write_config('[address]\npostcode = "1234AB"\nhouse_number = "56"\n')

    web.main(["--config", str(path)])

    assert logging.getLogger("httpx").level == logging.WARNING


def test_the_config_path_defaults_to_the_installed_one():
    assert web.build_parser().parse_args([]).config == gca.DEFAULT_CONFIG


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_sigterm_stops_it_the_way_systemd_will(tmp_path, write_config):
    """The unit file's whole stop path: a signal, a clean exit, a released port.

    The client below stays open across the signal, the way a forgotten browser
    tab holds a keep-alive connection: a stop drops that where it is rather than
    waiting out CONNECTION_TIMEOUT, which the ten seconds here is what checks.
    """
    port = free_port()
    config_file = write_config(
        f'[address]\npostcode = "1234AB"\nhouse_number = "56"\n'
        f"[web]\nenabled = true\nport = {port}\n"
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "garbage_collection_automation.web", "--config", str(config_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    idle = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1)
    try:
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None, f"the server exited: {process.stdout.read()}"
            try:
                idle.get("/healthz")
                break
            except httpx.HTTPError:
                assert time.monotonic() < deadline, "the server never came up"
                time.sleep(0.1)

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == gca.EXIT_OK
    finally:
        idle.close()
        if process.poll() is None:  # pragma: no cover - only on a failed run
            process.kill()
        process.stdout.close()

    # The listening socket is closed too, so a restart does not have to wait for
    # it. SO_REUSEADDR because that is what HTTPServer binds with, and what lets
    # the next process have the port while the dropped connection above is still
    # in TIME_WAIT; it does not let anything past a socket that is still listening.
    with socket.socket() as after:
        after.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        after.bind(("127.0.0.1", port))


# --- the endpoints --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def source(monkeypatch):
    """No test here reaches the network either; the schedule is the stubbed one."""
    monkeypatch.setattr(data_processing, "today", lambda: TODAY)
    monkeypatch.setattr(data_collection, "collect", lambda config: SCHEDULE)


def test_the_page_can_ask_what_to_draw_itself_from(client):
    response = client.get("/api/state")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json()["config"]["postcode"] == "1234AB"
    assert response.json()["config_file"]["text"], "the panel showing the file would be empty"


def test_the_page_has_a_field_for_every_setting_the_server_will_save(client):
    """The markup and api.FORM_FIELDS agreeing about names, checked rather than assumed.

    A field the server accepts and the page has no input for is a setting that
    is only reachable by hand - which is the thing this page exists to avoid.
    """
    markup = (web.DEFAULT_UI_DIR / web.INDEX).read_text()

    missing = [field for field in sorted(api.FORM_FIELDS) if f'id="{field}"' not in markup]

    assert not missing, f"the page has no field for: {', '.join(missing)}"


def test_every_field_the_page_saves_has_a_name_to_confirm_it_by(client):
    """The save asks before it writes, and lists what changes; an unnamed field
    would be listed as "undefined" in the one dialog nobody should have to guess at."""
    script = (web.DEFAULT_UI_DIR / "static" / "app.js").read_text()
    labels = script.split("const FIELD_LABELS = {")[1].split("};")[0]

    unnamed = [field for field in sorted(api.FORM_FIELDS) if f"{field}:" not in labels]

    assert not unnamed, f"the confirmation cannot name: {', '.join(unnamed)}"


def test_a_button_runs_the_job_and_answers_with_what_it_found(client):
    response = client.post("/api/collect", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["ok"] is True
    assert body["log"], "the console panel would have nothing to show"


def test_the_form_saves_through_the_endpoint(client, paths):
    response = client.post("/api/config", json={"lookahead_days": 45})

    assert response.status_code == 200
    assert response.json()["config"]["lookahead_days"] == 45
    assert "lookahead_days = 45" in paths.config.read_text()


def test_a_refused_value_comes_back_as_json_the_page_can_show(client):
    response = client.post("/api/config", json={"postcode": "nope"})

    assert response.status_code == 400
    assert "not a Dutch postcode" in response.json()["error"]


def test_an_endpoint_that_does_not_exist_says_so_in_json(client):
    response = client.post("/api/wat", json={})

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("route", ["collect", "check", "apply", "config", "stop"])
def test_nothing_that_changes_anything_can_be_reached_with_a_get(client, route):
    """A link, a redirect or a prefetch must never be able to set the job going."""
    response = client.get(f"/api/{route}")

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_the_read_only_endpoint_is_not_a_post(client):
    response = client.post("/api/state", json={})

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


def test_the_wrong_method_is_refused_in_json_like_every_other_endpoint(client):
    """The page parses one shape; a text/plain refusal is a parse error to it."""
    response = client.get("/api/collect")

    assert response.headers["content-type"] == "application/json"
    assert "POST" in response.json()["error"]


def test_a_run_already_in_progress_is_a_conflict_not_a_queue(client, paths, monkeypatch):
    """Two tabs, or a tab and the weekly run: the second is told, not left hanging."""
    paths.lock.parent.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def busy(_paths):
        raise api.Busy("the scheduled run is in progress")
        yield  # pragma: no cover - never reached

    monkeypatch.setattr(api, "_locked", busy)
    response = client.post("/api/collect", json={})

    assert response.status_code == 409
    assert "in progress" in response.json()["error"]


def test_a_configuration_that_stopped_loading_is_reported_not_crashed(client, paths):
    """Someone edited the file by hand while the server was up; say so, do not fall over."""
    paths.config.write_text("[address]\npostcode = 'nope'\n")

    response = client.post("/api/collect", json={})

    assert response.status_code == 500
    assert "postcode" in response.json()["error"]


def test_a_job_that_raises_becomes_a_sentence_rather_than_a_traceback(client, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("the token was made of cheese")

    monkeypatch.setattr(api, "collect", explode)
    response = client.post("/api/collect", json={})

    assert response.status_code == 500
    assert "the token was made of cheese" not in response.text, "internals stay in the log"
    assert "error" in response.json()


# --- the stop button ------------------------------------------------------------------


def test_the_stop_endpoint_answers_before_it_stops_anything(client, paths):
    """The order the page depends on: it has to be told, and then lose the server.

    The other way round leaves a page that cannot say whether the thing it just
    asked for happened, on a machine whose only other way in is ssh.
    """
    paths.config.write_text(MINIMAL_CONFIG + "\n[web]\nenabled = true\n")

    response = client.post("/api/stop", json={})

    assert response.status_code == 200
    assert response.json()["stopping"] is True
    assert response.json()["config"]["web_enabled"] is False


def test_the_stop_endpoint_asks_the_server_to_stop(client, paths, server):
    paths.config.write_text(MINIMAL_CONFIG + "\n[web]\nenabled = true\n")
    assert not server.stopping.is_set()

    client.post("/api/stop", json={})

    assert server.stopping.is_set(), "serve() would have kept serving"


def test_a_refused_stop_leaves_the_server_serving(client, paths, server):
    """No file written, no server stopped: the page would come back at the next boot."""
    paths.config.chmod(0o444)

    response = client.post("/api/stop", json={})

    assert response.status_code == 400
    assert "cannot write" in response.json()["error"]
    assert not server.stopping.is_set()
    assert client.get("/healthz").status_code == 200


def test_the_page_is_not_stopped_by_a_link_from_another_site(client, paths):
    """It is a POST and it is checked like the rest; neither is an accident."""
    paths.config.write_text(MINIMAL_CONFIG + "\n[web]\nenabled = true\n")

    response = client.post("/api/stop", json={}, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert configuration.load(paths.config).web.enabled is True


def test_the_stop_button_ends_the_process_the_way_a_signal_does(tmp_path, write_config):
    """The whole thing, in the layout the unit runs: a request in, an exit 0 out.

    Exit 0 is the load-bearing part. The unit restarts on failure, so a stop
    that looked like one would put the server straight back on the port it was
    just asked to give up.
    """
    port = free_port()
    config_file = write_config(
        f'[address]\npostcode = "1234AB"\nhouse_number = "56"\n'
        f"[web]\nenabled = true\nport = {port}\n"
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "garbage_collection_automation.web", "--config", str(config_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as caller:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, f"the server exited: {process.stdout.read()}"
                try:
                    caller.get("/healthz")
                    break
                except httpx.HTTPError:
                    assert time.monotonic() < deadline, "the server never came up"
                    time.sleep(0.1)

            assert caller.post("/api/stop", json={}).json()["stopping"] is True

        assert process.wait(timeout=10) == gca.EXIT_OK
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed run
            process.kill()
        process.stdout.close()

    # And it stays off: the next start reads the key it just wrote and exits.
    assert "enabled = false" in config_file.read_text()
    again = subprocess.run(
        [sys.executable, "-m", "garbage_collection_automation.web", "--config", str(config_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert again.returncode == gca.EXIT_OK
    assert "switched off" in again.stdout + again.stderr


# --- who is allowed to call them ------------------------------------------------------
#
# The page has no login, so these are what stand between the buttons and any
# website the browser happens to have open at the same time.


@pytest.mark.parametrize("content_type", ["application/x-www-form-urlencoded", "text/plain", ""])
def test_a_post_a_form_could_have_sent_is_refused(client, content_type):
    """These three are what a cross-origin <form> may set without a preflight."""
    response = client.post(
        "/api/apply",
        content=b'{"postcode": "1234AB"}',
        headers={"Content-Type": content_type} if content_type else {},
    )

    assert response.status_code == 415


def test_a_post_from_another_site_is_refused(client):
    response = client.post("/api/apply", json={}, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403


def test_the_page_posting_to_its_own_server_is_not(client):
    origin = str(client.base_url).rstrip("/")

    response = client.post("/api/collect", json={}, headers={"Origin": origin})

    assert response.status_code == 200


def test_a_name_that_merely_resolves_here_is_refused(client):
    """DNS rebinding: a hostname the attacker owns, pointed at 127.0.0.1."""
    response = client.get("/api/state", headers={"Host": "gotcha.evil.example"})

    assert response.status_code == 403


def test_the_tunnels_own_port_is_this_server(client):
    """``ssh -L 8081:127.0.0.1:8080`` makes the browser say a port we never bound."""
    response = client.get("/api/state", headers={"Host": "127.0.0.1:8081"})

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["localhost", "localhost:8081", "[::1]:8081", "127.0.0.2"])
def test_the_other_ways_of_saying_this_machine_are_this_server(client, host):
    response = client.get("/api/state", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["192.168.1.10:8080", "10.0.0.1", "127.0.0.1:half"])
def test_a_host_that_is_not_the_loopback_is_refused(client, host):
    response = client.get("/api/state", headers={"Host": host})

    assert response.status_code == 403


def test_a_post_from_another_port_on_this_machine_is_refused(client):
    """Same host, different port is still another origin, tunnel or no tunnel."""
    response = client.post(
        "/api/apply", json={}, headers={"Host": "127.0.0.1:8081", "Origin": "http://127.0.0.1:9000"}
    )

    assert response.status_code == 403


def test_a_request_with_no_host_at_all_is_not_the_page_either(client):
    """HTTP/1.1 requires one and every browser sends one; its absence is not the page."""
    answer = raw_request(client, "GET /api/state HTTP/1.1\r\nConnection: close\r\n\r\n")

    assert answer.splitlines()[0].split()[1] == "403"
    assert "not reachable under that name" in answer


def raw_request(client, request: str) -> str:
    """One request written by hand, for the headers httpx will not let go of."""
    with socket.create_connection((client.base_url.host, client.base_url.port), timeout=5) as sock:
        sock.sendall(request.encode())
        received = b""
        while chunk := sock.recv(4096):
            received += chunk
    return received.decode()


def test_a_body_larger_than_anything_the_page_sends_is_refused(client):
    response = client.post(
        "/api/config",
        content=b"x" * (web.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_a_body_that_is_not_a_json_object_is_refused(client):
    for body in (b"[1, 2, 3]", b'"a string"', b"not json at all"):
        response = client.post(
            "/api/config", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400, body


def test_the_endpoints_answer_with_the_security_headers_too(client):
    headers = client.get("/api/state").headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"


def test_every_route_the_page_calls_exists(client):
    """The page and the server agreeing about the route names, checked rather than assumed."""
    called = set(
        re.findall(r'"(/api/[a-z]+)"', (web.DEFAULT_UI_DIR / "static" / "app.js").read_text())
    )

    assert called, "the page stopped calling anything"
    for path in called:
        assert path[len(web.API_PREFIX) :] in web.ROUTES, f"the page calls {path}"
