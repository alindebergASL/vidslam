from __future__ import annotations

import logging
import re


def test_request_id_minted_when_not_supplied(client):
    r = client.get("/healthz")
    rid = r.headers.get("X-Request-ID")
    assert rid is not None
    # 32-char hex from uuid.hex.
    assert re.fullmatch(r"[0-9a-f]{32}", rid)


def test_inbound_request_id_is_passed_through(client):
    # A reasonable inbound id (8-64 ASCII) is honoured so a load balancer / SDK
    # can correlate the request across services.
    r = client.get("/healthz", headers={"X-Request-ID": "inbound-test-1234"})
    assert r.headers["X-Request-ID"] == "inbound-test-1234"


def test_unreasonable_request_id_is_replaced(client):
    # Too-short ids would be useless for correlation; we mint a fresh uuid.
    r = client.get("/healthz", headers={"X-Request-ID": "x"})
    rid = r.headers["X-Request-ID"]
    assert rid != "x"
    assert re.fullmatch(r"[0-9a-f]{32}", rid)
    # And one that's pathologically long is also rejected.
    r = client.get("/healthz", headers={"X-Request-ID": "y" * 200})
    assert len(r.headers["X-Request-ID"]) == 32


def test_unhandled_exception_returns_structured_500_with_id(client, monkeypatch):
    """Adding a temporary error route to the app proves the middleware turns an
    uncaught exception into a JSON 500 with the request id stitched in, instead
    of FastAPI's default opaque {detail: 'Internal Server Error'}."""
    from app.main import app

    @app.get("/_test_boom")
    def _boom():
        raise RuntimeError("synthetic explosion")

    # Tell the TestClient to NOT re-raise — we want to inspect the actual
    # response the middleware produces, not the underlying exception.
    client.raise_server_exceptions = False
    try:
        r = client.get("/_test_boom", headers={"X-Request-ID": "trace-this-1234"})
    finally:
        client.raise_server_exceptions = True
        app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != "/_test_boom"]

    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal Server Error"
    assert body["request_id"] == "trace-this-1234"
    assert body["error_type"] == "RuntimeError"
    assert r.headers["X-Request-ID"] == "trace-this-1234"


def test_access_log_records_request_id_for_non_probe(client, caplog):
    """Non-probe requests emit a structured access log line carrying method,
    path, status, duration, and request_id."""
    caplog.set_level(logging.INFO, logger="avs.access")
    client.get("/api/providers/status", headers={"X-Request-ID": "logme-12345"})
    matched = [
        rec for rec in caplog.records
        if "logme-12345" in rec.getMessage() and "path=/api/providers/status" in rec.getMessage()
    ]
    assert matched, "expected an access log line for the probed path with the inbound id"
    msg = matched[0].getMessage()
    # Sanity-check that the structured shape we depend on for log queries is intact.
    assert "method=GET" in msg
    assert "status=" in msg
    assert "duration_ms=" in msg


def test_health_probes_are_excluded_from_access_log(client, caplog):
    """/health, /healthz, /readyz are polled constantly by load balancers; their
    access lines would drown signal in the operator's log aggregator."""
    caplog.set_level(logging.INFO, logger="avs.access")
    client.get("/healthz")
    client.get("/readyz")
    client.get("/health")
    for rec in caplog.records:
        msg = rec.getMessage()
        assert "path=/healthz" not in msg
        assert "path=/readyz" not in msg
        assert "path=/health " not in msg  # trailing space to avoid colliding with /health-check etc.
