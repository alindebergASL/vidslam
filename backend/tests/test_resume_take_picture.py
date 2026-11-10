"""Regression test (scene 29, Fireplace — 1h): a day/night take whose
picture fails keeps the finished takes, and Resume makes only that take."""
from __future__ import annotations

from pathlib import Path

from .test_ambient_waste import _get, _scene, _spy_submits, _upload


def test_a_take_whose_picture_fails_keeps_the_finished_takes_and_resumes(auth_client, monkeypatch):
    from app.services import ambient

    submits = _spy_submits(monkeypatch)
    stills: list[int] = []
    real = ambient.generate_still

    def still(*a, **kw):
        stills.append(1)
        if len(stills) == 2:  # take 3's picture (take 1 uses the uploaded one)
            raise RuntimeError("Client error '403 Forbidden' for url 'https://openrouter.ai/api/v1/images'")
        return real(*a, **kw)

    monkeypatch.setattr(ambient, "generate_still", still)
    sid = _scene(auth_client, generate=False, variety="day_night", variations=3)["id"]
    assert _upload(auth_client, sid).status_code == 200
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    scene = _get(auth_client, sid)
    assert scene["status"] == "failed"
    first, second = scene["loop_shot"]["clip_path"], scene["variant_shots"][0]["clip_path"]
    assert first and second and not scene["variant_shots"][1]["clip_path"]
    r = scene["resume"]
    assert r and r["take"] == 3 and r["takes"] == 3 and "403" in r["error"], r
    bought = len(submits)

    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 202
    scene = _get(auth_client, sid)
    assert scene["loop_status"] == "completed" and scene["status"] == "planned", scene["loop_error"]
    assert len(submits) == bought + 1  # only take 3's clip
    assert scene["loop_shot"]["clip_path"] == first and scene["variant_shots"][0]["clip_path"] == second
    assert Path(scene["variant_shots"][1]["clip_path"]).exists()


def test_a_refused_picture_says_why(monkeypatch):
    import httpx
    import pytest
    import respx

    from app import config
    from app.providers.openrouter_image import OpenRouterImageProvider

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-abcdefgh1234")
    config.get_settings.cache_clear()
    try:
        with respx.mock() as router:
            router.post("https://openrouter.ai/api/v1/images").mock(return_value=httpx.Response(
                403, json={"error": {"code": 403, "message": "Key limit exceeded",
                                     "metadata": {"reason": "monthly limit"}}}))
            with pytest.raises(httpx.HTTPStatusError) as e:
                OpenRouterImageProvider(model="google/nano").generate(model="google/nano", prompt="a lake",
                                                                      references=[])
        assert "403" in str(e.value) and "Key limit exceeded" in str(e.value) and "monthly limit" in str(e.value)
        assert "or-test" not in str(e.value)
    finally:
        config.get_settings.cache_clear()
