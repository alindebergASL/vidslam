"""Sogni's safe filter has a switch on its row in Settings → API keys, like
Runware's content check: always on while Adult mode is off; in Adult mode
the switch decides (default SOGNI_SAFE_CONTENT_FILTER), except that an
unticked Cast picture, a prompt that mentions a child and an avoid list
that steers young still force it on. Minors are always refused."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

SOGNI_WF = "https://api.sogni.ai/v1/creative-agent/workflows"


def _reset():
    from app import config
    from app.services import provider_keys

    config.get_settings.cache_clear()
    provider_keys.invalidate()


@pytest.fixture
def sogni_key(monkeypatch):
    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    _reset()
    yield
    _reset()


def _filters(*calls) -> list[bool]:
    """safe_content_filter of each SogniClient.start(**kwargs) in `calls`."""
    from app.providers.sogni import SogniClient

    bodies: list[dict] = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf",
                                                                                    "status": "queued"}}})

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        for kw in calls:
            SogniClient().start(title="t", tool="generate_image", **kw)
    return [b["safe_content_filter"] for b in bodies]


LAKE = {"arguments": {"prompt": "a lake at dawn"}}


def test_the_option_row_and_endpoint(auth_client, sogni_key):
    rows = {k["name"]: k for k in auth_client.get("/api/settings/keys").json()["keys"]}
    assert rows["sogni"]["options"] == {"safe_filter": False}  # SOGNI_SAFE_CONTENT_FILTER=false
    r = auth_client.put("/api/settings/keys/sogni/options", json={"safe_filter": True})
    assert r.status_code == 200 and r.json()["options"] == {"safe_filter": True}
    for bad in ({"check_content": True}, {"safe_filter": "yes"}, {}):
        assert auth_client.put("/api/settings/keys/sogni/options", json=bad).status_code == 422
    assert auth_client.put("/api/settings/keys/runware/options", json={"safe_filter": True}).status_code == 422
    back = auth_client.put("/api/settings/keys/sogni/options", json={"safe_filter": None}).json()
    assert back["options"] == {"safe_filter": False}


def test_the_env_value_is_the_default(auth_client, sogni_key, monkeypatch):
    from app.services import content_mode, provider_keys

    monkeypatch.setenv("SOGNI_SAFE_CONTENT_FILTER", "true")
    _reset()
    assert provider_keys.option("sogni", "safe_filter") is True
    content_mode.set_adult_mode(True)
    assert _filters(LAKE) == [True]
    provider_keys.set_option("sogni", "safe_filter", False)  # the Settings switch wins once set
    assert _filters(LAKE) == [False]


def test_always_on_while_adult_mode_is_off(auth_client, sogni_key):
    from app.services import provider_keys

    provider_keys.set_option("sogni", "safe_filter", False)
    assert _filters(LAKE, {"arguments": {"prompt": "a woman in lingerie"}}) == [True, True]


def test_adult_mode_follows_the_switch_read_live(auth_client, sogni_key):
    from app.services import content_mode, provider_keys

    content_mode.set_adult_mode(True)
    assert _filters(LAKE) == [False]  # default off
    provider_keys.set_option("sogni", "safe_filter", True)
    assert _filters(LAKE) == [True]  # no restart needed
    provider_keys.set_option("sogni", "safe_filter", False)
    assert _filters(LAKE) == [False]


def test_the_safety_nets_force_it_on_whatever_the_switch(auth_client, sogni_key):
    from app.services import content_mode, provider_keys

    content_mode.set_adult_mode(True)
    provider_keys.set_option("sogni", "safe_filter", False)
    assert _filters(
        {"arguments": {"prompt": "her on a beach"}, "cast": True},  # an unticked Cast picture
        {"arguments": {"prompt": "a teenager walking to school"}},  # mentions a minor
        {"arguments": {"prompt": "a nude woman on a bed"}, "negative": "adult, mature, wrinkles"},  # steers young
        {"arguments": {"prompt": "a nude woman on a bed"}, "negative": "blurry"},
    ) == [True, True, True, False]


def test_minors_are_refused_whatever_the_switch(auth_client, sogni_key):
    from app.providers.sogni import SogniClient
    from app.services import content_mode, provider_keys
    from app.services.safety import UnsafePromptError

    content_mode.set_adult_mode(True)
    provider_keys.set_option("sogni", "safe_filter", False)
    with respx.mock(assert_all_called=False) as router:
        start = router.post(SOGNI_WF)
        with pytest.raises(UnsafePromptError):
            SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "naked teenage girl"})
    assert not start.called


def test_scripts_without_a_database_use_the_config_value(monkeypatch):
    """No database (a script): the env value, never a crash."""
    from app.providers import sogni
    from app.services import provider_keys

    monkeypatch.setattr(sogni, "_adult_mode", lambda: True)

    def broken(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(provider_keys, "option", broken)
    monkeypatch.setenv("SOGNI_SAFE_CONTENT_FILTER", "true")
    _reset()
    assert sogni.safe_filter_default() is True
    monkeypatch.setenv("SOGNI_SAFE_CONTENT_FILTER", "false")
    _reset()
    assert sogni.safe_filter_default() is False
    monkeypatch.setattr(sogni, "_adult_mode", lambda: False)
    assert sogni.safe_filter_default() is True
