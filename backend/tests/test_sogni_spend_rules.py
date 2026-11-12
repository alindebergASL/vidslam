"""Follow-ups from the first live Sogni deploy:

- Only vendor (Premium Spark) models cost money. Native models ran on Free
  Spark and left the Spark balance untouched, so by default they count $0
  toward Spend, the monthly cap and estimates (SOGNI_COUNT_FREE_SPARK=true
  counts them at list price).
- The API key Test names the Sogni account the key belongs to.
- Studio images are sized for their owner (portrait characters, landscape
  scenes) instead of Sogni's square default.
"""
from __future__ import annotations

import httpx
import pytest
import respx


def _settings(monkeypatch, **env):
    from app import config

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    return config.get_settings()


def test_free_spark_models_count_nothing_vendor_models_do(monkeypatch):
    from app.providers import sogni

    _settings(monkeypatch)
    # Native models (Free Spark): listed price kept, Spend amount 0.
    assert sogni.image_cost_usd("sogni/krea-2-turbo") > 0
    assert sogni.image_spend_usd("sogni/krea-2-turbo") == 0.0
    assert sogni.video_rate_usd("sogni/ltx25") > 0
    assert sogni.video_spend_rate_usd("sogni/ltx25") == 0.0
    assert sogni.video_spend_usd("sogni/ltx25", 10) == 0.0
    assert "Free Spark" in sogni.spend_note("sogni/ltx25", "video")
    # Vendor models (Premium Spark) still count at their price.
    vendor_image = next(m.selector for m in sogni.image_models() if m.premium)
    vendor_video = next(m.selector for m in sogni.video_models() if m.premium)
    assert sogni.image_spend_usd(f"sogni/{vendor_image}") == sogni.image_cost_usd(f"sogni/{vendor_image}") > 0
    assert sogni.video_spend_usd(f"sogni/{vendor_video}", 5) == pytest.approx(
        sogni.video_rate_usd(f"sogni/{vendor_video}") * 5) and sogni.video_rate_usd(f"sogni/{vendor_video}") > 0
    # The setting counts Free Spark models again.
    _settings(monkeypatch, SOGNI_COUNT_FREE_SPARK="true")
    assert sogni.image_spend_usd("sogni/krea-2-turbo") == sogni.image_cost_usd("sogni/krea-2-turbo")
    assert sogni.video_spend_rate_usd("sogni/ltx25") == sogni.video_rate_usd("sogni/ltx25")


def _sogni_entry(model_id):
    from app.services import model_catalog

    return next(e for e in model_catalog._sogni_entries() if e["id"] == model_id)


def test_picker_prices_free_spark_models_as_free(monkeypatch):
    """The price shown before running is what Spend will record: a Free
    Spark model isn't "about $0.0125 per image" when it's counted as $0."""
    from app.providers import sogni
    from app.services.prices import price_text

    _settings(monkeypatch)
    assert price_text(_sogni_entry("sogni/krea-2-turbo"), "image") == "free (Free Spark)"
    assert price_text(_sogni_entry("sogni/ltx25"), "video") == "free (Free Spark)"
    vendor_video = next(m.selector for m in sogni.video_models() if m.premium)
    assert price_text(_sogni_entry(f"sogni/{vendor_video}"), "video").startswith("about $")
    assert price_text(_sogni_entry("sogni/gpt-image-2"), "image").startswith("about $")
    # A Free Spark model whose reference pictures go to a Premium Spark editor
    # says what a picture with references costs.
    _settings(monkeypatch, SOGNI_SCENE_EDIT_MODEL="gpt-image-2")
    text = price_text(_sogni_entry("sogni/z-turbo"), "image")
    assert text.startswith("free (Free Spark); about $") and text.endswith("with reference pictures")


def test_picker_keeps_list_prices_when_free_spark_is_counted(monkeypatch):
    from app.providers import sogni
    from app.services.prices import money, price_text

    _settings(monkeypatch, SOGNI_COUNT_FREE_SPARK="true")
    assert price_text(_sogni_entry("sogni/krea-2-turbo"), "image") == \
        f"about {money(sogni.image_cost_usd('sogni/krea-2-turbo'))} per image"
    assert price_text(_sogni_entry("sogni/ltx25"), "video") == \
        f"about {money(sogni.video_rate_usd('sogni/ltx25') * 5)} per 5 s"


def test_free_spark_video_job_is_recorded_at_zero_with_a_note(client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import spend

    from .test_spend import _entries

    _settings(monkeypatch)
    db = SessionLocal()
    try:
        job = models.VideoJob(purpose="shot", ref_id=1, provider="sogni", model="sogni/ltx25",
                              provider_job_id="wf_1", status="completed", context_json={"duration": 10})
        db.add(job)
        db.commit()
        spend.record_video_job(db, job)
        db.commit()
    finally:
        db.close()
    (row,) = _entries(provider="sogni")
    assert row["cost_usd"] == 0.0 and row["is_estimate"] is True


def test_project_estimate_counts_free_spark_shots_as_free(auth_client, monkeypatch):
    from app import models
    from app.config import get_settings
    from app.db import SessionLocal

    from .test_cost_and_bulk import _project_with_plan

    _settings(monkeypatch)
    pid = _project_with_plan(auth_client)
    db = SessionLocal()
    try:
        shots = [s for s in db.get(models.VideoProject, pid).shots if s.shot_type != "end_card"]
        shots[0].model_override = "sogni/ltx25"
        db.commit()
        rest = sum(s.duration_seconds for s in shots[1:])
    finally:
        db.close()
    est = auth_client.get(f"/api/projects/{pid}/cost-estimate").json()
    video = next(ln for ln in est["lines"] if ln["item"].startswith("Video clips"))
    assert video["cost"] == pytest.approx(rest * get_settings().cost_video_per_second, abs=1e-3)


@respx.mock
def test_key_test_names_the_sogni_account(auth_client, monkeypatch):
    _settings(monkeypatch, SOGNI_API_KEY="sogni_dev_test123456789")
    respx.get("https://api.sogni.ai/v2/account/me").mock(return_value=httpx.Response(
        200, json={"status": "success", "data": {"username": "alindy", "emailVerified": True}}))
    r = auth_client.post("/api/settings/keys/sogni/test").json()
    assert r["ok"] is True and "account alindy" in r["detail"]
    assert "sogni_dev_test123456789" not in str(r)


@respx.mock
def test_key_test_without_an_account_name_still_works(auth_client, monkeypatch):
    _settings(monkeypatch, SOGNI_API_KEY="sogni_dev_test123456789")
    respx.get("https://api.sogni.ai/v2/account/me").mock(return_value=httpx.Response(200, json={"data": {}}))
    r = auth_client.post("/api/settings/keys/sogni/test").json()
    assert r["ok"] is True and "account" not in r["detail"]


@pytest.mark.parametrize("owner_kind,aspect", [("avatar", "9:16"), ("ingredient", "16:9")])
def test_studio_images_are_sized_for_their_owner(auth_client, monkeypatch, owner_kind, aspect):
    from app.providers import mock_image

    seen = []
    real = mock_image.MockImageProvider.generate

    def spy(self, **kw):
        seen.append(kw.get("settings"))
        return real(self, **kw)

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", spy)
    if owner_kind == "avatar":
        oid = auth_client.post("/api/avatars", json={"name": "A"}).json()["id"]
    else:
        oid = auth_client.post("/api/ingredients", json={"name": "Rooftop", "kind": "scene"}).json()["id"]
    r = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": owner_kind, "owner_id": oid, "prompt": "x", "model": "sogni/krea-2-turbo"})
    assert r.status_code == 202, r.text
    assert seen and (seen[-1] or {}).get("aspect_ratio") == aspect
