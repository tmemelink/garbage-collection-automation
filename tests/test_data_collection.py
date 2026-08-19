"""Tests for querying the mijnafvalwijzer.nl JSON API. No test touches the network."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from garbage_collection_automation import data_collection
from garbage_collection_automation.data_collection import CollectionError, MissingApiKey

from .conftest import make_config, read_fixture

SUCCESS = read_fixture("afvalwijzer_1234ab_21.json")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retries are real seconds in production; keep the suite instant."""
    monkeypatch.setattr(data_collection, "_BACKOFF_SECONDS", 0)


def client_returning(*responses: httpx.Response) -> tuple[httpx.Client, list[httpx.Request]]:
    """A client that replays *responses* in order, and the requests it saw."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def ok(body: str = SUCCESS) -> httpx.Response:
    return httpx.Response(200, text=body)


def test_url_carries_the_whole_address():
    url = httpx.URL(data_collection.build_url(make_config()))

    assert url.host == "api.mijnafvalwijzer.nl"
    assert url.params["postcode"] == "1234AB"
    assert url.params["huisnummer"] == "21"
    assert url.params["toevoeging"] == ""
    assert url.params["method"] == "postcodecheck"


def test_url_carries_the_configured_api_key():
    """The key is configuration, not a constant; whatever is set is what is sent."""
    url = httpx.URL(data_collection.build_url(make_config(api_key="an-app-key")))

    assert url.params["apikey"] == "an-app-key"


def test_without_an_api_key_nothing_is_asked_at_all():
    """A blank key loads fine, so this is where an install that has none finds out."""
    client, seen = client_returning(ok())

    with pytest.raises(MissingApiKey, match="GCA_AFVALWIJZER_API_KEY"):
        data_collection.collect(make_config(api_key=""), client=client)

    assert seen == [], "the request is never made"


def test_a_missing_api_key_is_a_collection_error():
    """The pipeline catches CollectionError and exits 4; this must not slip past it."""
    assert issubclass(MissingApiKey, CollectionError)


def test_url_includes_the_addition():
    """`addition` has no place in the website URL, but the API takes it."""
    config = make_config(postcode="1234AB", house_number="56", addition="bis")

    assert httpx.URL(data_collection.build_url(config)).params["toevoeging"] == "bis"


def test_collect_returns_the_schedule_and_its_version():
    client, seen = client_returning(ok())

    raw = data_collection.collect(make_config(), client=client)

    assert len(seen) == 1
    assert raw.data_version == "1786701549"
    assert len(raw.entries) == 104
    assert raw.entries[0] == {"nameType": "gft", "type": "gft", "date": "2026-01-05"}
    assert raw.address == "Voorbeeldstraat 21, Voorbeeldstad"
    assert raw.fetched_at.tzinfo is not None


def test_collect_identifies_itself():
    """Undocumented API or not, an unattended client should say who it is."""
    client, seen = client_returning(ok())

    data_collection.collect(make_config(), client=client)

    assert "garbage-collection-automation" in seen[0].headers["user-agent"]


def test_collect_merges_next_year_when_the_source_publishes_it():
    payload = json.loads(SUCCESS)
    payload["data"]["ophaaldagenNext"] = {
        "response": "OK",
        "data": [{"nameType": "gft", "type": "gft", "date": "2027-01-04"}],
    }
    client, _ = client_returning(ok(json.dumps(payload)))

    raw = data_collection.collect(make_config(), client=client)

    assert len(raw.entries) == 105
    assert raw.entries[-1]["date"] == "2027-01-04"


def test_unknown_address_is_reported_even_though_the_source_answers_ok():
    """A bogus postcode yields a fallback municipality with an empty schedule, not an error."""
    client, _ = client_returning(ok(read_fixture("afvalwijzer_unknown_address.json")))

    with pytest.raises(CollectionError, match="no collection schedule"):
        data_collection.collect(make_config(), client=client)


def test_unknown_house_number_is_reported():
    client, _ = client_returning(ok(read_fixture("afvalwijzer_no_afvaldata.json")))

    with pytest.raises(CollectionError, match="No Afvaldata"):
        data_collection.collect(make_config(), client=client)


def test_body_that_is_not_json_is_reported():
    client, _ = client_returning(ok("<html>maintenance</html>"))

    with pytest.raises(CollectionError, match="not JSON"):
        data_collection.collect(make_config(), client=client)


def test_server_error_is_retried_and_then_succeeds():
    client, seen = client_returning(httpx.Response(503), ok())

    raw = data_collection.collect(make_config(retries=1), client=client)

    assert len(seen) == 2
    assert len(raw.entries) == 104


def test_retries_are_bounded():
    client, seen = client_returning(httpx.Response(503))

    with pytest.raises(CollectionError, match="503"):
        data_collection.collect(make_config(retries=2), client=client)

    assert len(seen) == 3


def test_retries_can_be_switched_off():
    client, seen = client_returning(httpx.Response(503))

    with pytest.raises(CollectionError):
        data_collection.collect(make_config(retries=0), client=client)

    assert len(seen) == 1


def test_a_network_failure_is_reported_without_a_traceback():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(explode))

    with pytest.raises(CollectionError, match="could not reach"):
        data_collection.collect(make_config(retries=0), client=client)


def test_debug_log_does_not_copy_the_query_credentials_or_address(caplog):
    config = make_config(
        postcode="4321ZX",
        house_number="987",
        addition="bis",
        api_key="do-not-log-this-key",  # pragma: allowlist secret
    )
    client, _ = client_returning(ok())

    with caplog.at_level(logging.DEBUG, logger=data_collection.__name__):
        raw = data_collection.collect(config, client=client)

    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name == data_collection.__name__
    )
    assert "do-not-log-this-key" not in messages
    assert "4321ZX" not in messages
    assert "987" not in messages
    assert raw.url == data_collection.API_URL


def test_a_network_exception_cannot_copy_its_private_request_url_into_the_error():
    config = make_config(postcode="4321ZX", house_number="987", api_key="do-not-report-this-key")

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed request: {request.url}", request=request)

    client = httpx.Client(transport=httpx.MockTransport(explode))

    with pytest.raises(CollectionError) as caught:
        data_collection.collect(config, client=client)

    message = str(caught.value)
    assert "do-not-report-this-key" not in message
    assert "4321ZX" not in message
    assert "987" not in message
    assert "ConnectError" in message


def test_a_status_we_cannot_fix_by_asking_again_is_not_retried():
    """404 is our mistake, not a hiccup; retrying it only annoys the source."""
    client, seen = client_returning(httpx.Response(404))

    with pytest.raises(CollectionError, match="404"):
        data_collection.collect(make_config(retries=3), client=client)

    assert len(seen) == 1


def test_collect_opens_its_own_client_when_it_is_given_none(monkeypatch):
    """The scheduled run passes no client, so that path is the one production uses."""
    real_client = httpx.Client
    opened: list[httpx.Client] = []

    def owned_client(*args, **kwargs) -> httpx.Client:
        client = real_client(transport=httpx.MockTransport(lambda request: ok()))
        opened.append(client)
        return client

    monkeypatch.setattr(data_collection.httpx, "Client", owned_client)

    raw = data_collection.collect(make_config())

    assert len(raw.entries) == 104
    assert len(opened) == 1
    assert opened[0].is_closed, "the client it opened has to be the client it closes"
