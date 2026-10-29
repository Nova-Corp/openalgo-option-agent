"""OpenAlgoBroker.ping must never keep a bad keep-alive socket between retries."""
from types import SimpleNamespace

import pytest

from agent.broker import BrokerError, OpenAlgoBroker


class StubClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def make_broker(responses):
    """Build a broker around a stub SDK whose _post returns the given dicts in order."""
    b = OpenAlgoBroker.__new__(OpenAlgoBroker)
    b.cfg = SimpleNamespace(api_key="k", host="http://127.0.0.1:5000", ws_port=8765, timeout_sec=5)
    b.tag = "t"
    b._feed_connected = False
    calls = iter(responses)
    b.api = SimpleNamespace(client=StubClient(), _post=lambda endpoint, payload: next(calls))
    return b


def test_ping_success_keeps_connection():
    b = make_broker([{"status": "success", "data": {"broker": "aliceblue"}}])
    first = b.api.client
    assert b.ping() == {"broker": "aliceblue"}
    assert b.api.client is first and not first.closed


def test_ping_failure_resets_connection_and_names_airplay():
    airplay = {"status": "error", "message": "HTTP 403: ", "raw_response": "", "code": 403, "error_type": "http_error"}
    b = make_broker([airplay])
    first = b.api.client
    with pytest.raises(BrokerError) as ei:
        b.ping()
    assert "AirPlay" in str(ei.value)
    assert ei.value.transient
    assert first.closed and b.api.client is not first  # next retry opens a fresh socket


def test_ping_not_logged_in_resets_but_is_plain():
    not_in = {"status": "error", "message": "Sign in to OpenAlgo to use the API.", "code": 503}
    b = make_broker([not_in])
    first = b.api.client
    with pytest.raises(BrokerError) as ei:
        b.ping()
    assert "Sign in" in str(ei.value) and "AirPlay" not in str(ei.value)
    assert first.closed
