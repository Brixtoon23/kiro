"""PoliteClient tests: rate limiting, backoff, cache, graceful degradation.

These tests use a fake session and monkeypatched ``time`` so no real network
or wall-clock sleeps are involved.
"""

from __future__ import annotations

import requests

from corpus_ingesta import http_client
from corpus_ingesta.http_client import FetchError, PoliteClient


class _FakeResponse:
    def __init__(self, status_code=200, content=b"ok", url="http://example/final"):
        self.status_code = status_code
        self.content = content
        self.url = url


class _FakeSession:
    def __init__(self, responses):
        # responses: list of _FakeResponse or Exception instances
        self._responses = list(responses)
        self.calls = 0
        self.headers = {}

    def get(self, url, timeout=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_clock(monkeypatch):
    """Replace time.sleep/monotonic with a controllable fake clock."""
    state = {"now": 0.0, "slept": []}

    def fake_sleep(seconds):
        state["slept"].append(seconds)
        state["now"] += seconds

    def fake_monotonic():
        return state["now"]

    monkeypatch.setattr(http_client.time, "sleep", fake_sleep)
    monkeypatch.setattr(http_client.time, "monotonic", fake_monotonic)
    return state


def test_get_success_writes_cache(tmp_path, monkeypatch):
    _patch_clock(monkeypatch)
    session = _FakeSession([_FakeResponse(content=b"hello")])
    client = PoliteClient(cache_dir=tmp_path / "cache", session=session)

    result = client.get("http://example/a")
    assert result.content == b"hello"
    assert result.from_cache is False
    assert result.final_url == "http://example/final"
    assert client.cached("http://example/a")


def test_get_uses_cache_second_time(tmp_path, monkeypatch):
    _patch_clock(monkeypatch)
    session = _FakeSession([_FakeResponse(content=b"cached-body")])
    client = PoliteClient(cache_dir=tmp_path / "cache", session=session)

    client.get("http://example/a")
    assert session.calls == 1
    second = client.get("http://example/a")
    assert second.from_cache is True
    assert second.content == b"cached-body"
    assert session.calls == 1  # not re-fetched


def test_rate_limit_enforced(tmp_path, monkeypatch):
    state = _patch_clock(monkeypatch)
    session = _FakeSession([_FakeResponse(), _FakeResponse()])
    client = PoliteClient(base_delay=1.0, cache_dir=tmp_path / "cache", session=session)

    client.get("http://example/a")
    client.get("http://example/b")
    # Second request must wait ~1s due to spacing.
    assert any(s >= 1.0 for s in state["slept"])


def test_backoff_grows_on_failure(tmp_path, monkeypatch):
    state = _patch_clock(monkeypatch)
    session = _FakeSession(
        [
            requests.ConnectionError("boom"),
            requests.ConnectionError("boom"),
            _FakeResponse(content=b"recovered"),
        ]
    )
    client = PoliteClient(
        base_delay=1.0,
        backoff_factor=2.0,
        max_retries=3,
        cache_dir=tmp_path / "cache",
        session=session,
    )
    result = client.get("http://example/flaky")
    assert result.content == b"recovered"
    # Backoff sleeps for attempts 1 and 2 should be present and increasing.
    backoffs = [s for s in state["slept"] if s >= 1.0]
    assert len(backoffs) >= 2
    assert backoffs[-1] >= backoffs[0]


def test_raises_fetcherror_after_retries(tmp_path, monkeypatch):
    _patch_clock(monkeypatch)
    session = _FakeSession([requests.Timeout("t1"), requests.Timeout("t2"), requests.Timeout("t3")])
    client = PoliteClient(max_retries=3, cache_dir=tmp_path / "cache", session=session)
    try:
        client.get("http://example/dead")
    except FetchError as exc:
        assert exc.url == "http://example/dead"
    else:  # pragma: no cover
        raise AssertionError("expected FetchError")


def test_client_4xx_breaks_without_full_retries(tmp_path, monkeypatch):
    _patch_clock(monkeypatch)
    session = _FakeSession([_FakeResponse(status_code=404)])
    client = PoliteClient(max_retries=3, cache_dir=tmp_path / "cache", session=session)
    try:
        client.get("http://example/missing")
    except FetchError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected FetchError")
    assert session.calls == 1  # did not retry a 404


def test_backoff_is_strictly_exponential_and_cache_avoids_network(tmp_path, monkeypatch):
    """Consecutive failures produce strictly increasing backoff delays, and a
    subsequent successful body is served from cache with NO further network."""
    state = _patch_clock(monkeypatch)
    session = _FakeSession(
        [
            requests.ConnectionError("f1"),
            requests.ConnectionError("f2"),
            _FakeResponse(content=b"payload"),
        ]
    )
    client = PoliteClient(
        base_delay=1.0,
        backoff_factor=2.0,
        max_retries=3,
        cache_dir=tmp_path / "cache",
        session=session,
    )
    first = client.get("http://example/exp")
    assert first.content == b"payload"
    assert session.calls == 3  # two failures + one success

    # Backoff delays for attempt 1 and 2 are base*factor^1 and base*factor^2.
    backoffs = [s for s in state["slept"] if s >= 1.0]
    assert backoffs[0] == 2.0
    assert backoffs[1] == 4.0
    assert backoffs[1] > backoffs[0]

    # Second request for the same URL is served from cache, no network call.
    second = client.get("http://example/exp")
    assert second.from_cache is True
    assert second.content == b"payload"
    assert session.calls == 3  # unchanged: cache hit, no network
