"""Prompting: default avoid lists (+ effective_negative merge), the
"enhance my prompt" call (mock + OpenRouter), and saved presets."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

OR = "https://openrouter.ai/api/v1"

BUILTIN_IMAGE = ("malformed hands, extra fingers, missing fingers, bad anatomy, distorted face, deformed, "
                 "low quality, worst quality, blurry, jpeg artifacts, watermark, oversaturated")
BUILTIN_VIDEO = ("slow motion, slo-mo, jittery motion, warping, morphing, flicker, malformed hands, "
                 "bad anatomy, distorted face, low quality, jpeg artifacts, watermark")

PRESET = {"name": "Beach hero", "kind": "image", "prompt": "a woman on a beach at dawn",
          "negative_prompt": "sunglasses", "model": "google/nano-banana", "aspect_ratio": "9:16"}


# ---------- defaults ----------


def test_defaults_are_the_builtins_when_unset(auth_client):
    r = auth_client.get("/api/prompts/defaults")
    assert r.status_code == 200
    assert r.json() == {"image": BUILTIN_IMAGE, "video": BUILTIN_VIDEO}
    # Signs and end cards are wanted on purpose: no text/logo by default.
    for v in r.json().values():
        terms = [t.strip() for t in v.split(",")]
        assert "text" not in terms and "logo" not in terms


def test_defaults_round_trip_and_validation(auth_client):
    body = {"image": "  blurry, extra limbs ", "video": ""}
    r = auth_client.put("/api/prompts/defaults", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"image": "blurry, extra limbs", "video": ""}  # "" = deliberately none
    assert auth_client.get("/api/prompts/defaults").json() == r.json()

    from app.services import prompting

    assert prompting.negative_defaults() == {"image": "blurry, extra limbs", "video": ""}
    assert prompting.effective_negative("video") == ""

    assert auth_client.put("/api/prompts/defaults",
                           json={"image": "x" * 1001, "video": ""}).status_code == 422
    # Reset to built-in = put the built-ins back.
    auth_client.put("/api/prompts/defaults", json={"image": BUILTIN_IMAGE, "video": BUILTIN_VIDEO})
    assert prompting.negative_defaults()["image"] == BUILTIN_IMAGE


# ---------- effective_negative ----------


def test_effective_negative_puts_the_user_first_and_dedupes():
    from app.services.prompting import effective_negative

    out = effective_negative("image", "Sunglasses, BLURRY,  watermark ,, hats")
    terms = out.split(", ")
    assert terms[:4] == ["Sunglasses", "BLURRY", "watermark", "hats"]
    lower = [t.lower() for t in terms]
    assert lower.count("blurry") == 1 and lower.count("watermark") == 1
    assert "malformed hands" in lower and "oversaturated" in lower
    assert effective_negative("image") == BUILTIN_IMAGE
    assert effective_negative("video", "") == BUILTIN_VIDEO
    assert "slow motion" not in effective_negative("image")


def test_effective_negative_unknown_kind_adds_no_defaults():
    from app.services.prompting import effective_negative

    assert effective_negative("ambient", "a, b, A") == "a, b"
    assert effective_negative("lipsync") == ""


def test_effective_negative_is_trimmed_to_1000_chars_on_term_boundaries():
    from app.services.prompting import effective_negative

    user = ", ".join(f"thing number {i}" for i in range(200))
    out = effective_negative("image", user)
    assert len(out) <= 1000
    assert not out.endswith(",") and out.split(", ")[-1].startswith("thing number ")
    # A single over-long term is cut rather than dropped.
    assert effective_negative("video", "y" * 1500) == "y" * 1000


def test_effective_negative_uses_saved_defaults(auth_client):
    from app.services.prompting import effective_negative

    auth_client.put("/api/prompts/defaults", json={"image": "grainy, Blurry", "video": "flicker"})
    assert effective_negative("image", "blurry") == "blurry, grainy"
    assert effective_negative("video", "cats") == "cats, flicker"


# ---------- enhance (mock) ----------


def test_enhance_with_the_mock_planner(auth_client):
    r = auth_client.post("/api/prompts/enhance", json={"text": "a cat on a sofa", "kind": "image"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"].startswith("a cat on a sofa") and out["text"] != "a cat on a sofa"
    assert out["model"]
    v = auth_client.post("/api/prompts/enhance", json={"text": "a dog runs", "kind": "video",
                                                       "model": "bytedance/seedance-2"}).json()
    assert v["text"].startswith("a dog runs") and "slow motion" not in v["text"]
    s = auth_client.post("/api/prompts/enhance",
                         json={"text": "Buy  now.\nIt is   great.", "kind": "script"}).json()
    assert s["text"] == "Buy now.\nIt is great."  # never padded with invented lines
    sc = auth_client.post("/api/prompts/enhance", json={"text": "a quiet fjord", "kind": "scene"})
    assert sc.status_code == 200

    from .test_spend import _entries

    assert _entries() == []  # mock mode bills nothing


@pytest.mark.parametrize("body", [
    {"text": "", "kind": "image"},
    {"text": "   ", "kind": "image"},
    {"text": "x" * 4001, "kind": "image"},
    {"text": "ok", "kind": "music"},
    {"kind": "image"},
])
def test_enhance_validation(auth_client, body):
    assert auth_client.post("/api/prompts/enhance", json=body).status_code == 422


def test_enhance_is_rate_limited(auth_client, monkeypatch):
    from app import config

    monkeypatch.setenv("RATE_LIMIT_GENERATION_BURST", "1")
    monkeypatch.setenv("RATE_LIMIT_GENERATION_PER_MINUTE", "0.0001")
    config.get_settings.cache_clear()
    body = {"text": "a cat", "kind": "image"}
    assert auth_client.post("/api/prompts/enhance", json=body).status_code == 200
    r = auth_client.post("/api/prompts/enhance", json=body)
    assert r.status_code == 429 and r.headers.get("Retry-After")


# ---------- enhance (OpenRouter, mocked HTTP) ----------


def _live(monkeypatch, adult=False):
    from app import config

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "test/planner")
    monkeypatch.setenv("ADULT_MODE", "true" if adult else "false")
    config.get_settings.cache_clear()


def _reply(content, *, cost=0.0004, gen="gen-enh"):
    return httpx.Response(200, json={"id": gen, "model": "test/planner", "usage": {"cost": cost},
                                     "choices": [{"message": {"content": content}}]})


@respx.mock
def test_enhance_via_openrouter_sends_the_kind_guide_and_records_spend(auth_client, monkeypatch):
    from .test_spend import _entries

    _live(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=_reply('Prompt: "A grey cat asleep on a linen sofa, window light from the left."'))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a cat on a sofa", "kind": "image",
                                                       "model": "google/nano-banana"})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "A grey cat asleep on a linen sofa, window light from the left.",
                        "model": "test/planner"}

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "test/planner" and 0.3 <= sent["temperature"] <= 0.5
    system, user = sent["messages"][0], sent["messages"][1]
    assert system["role"] == "system" and user == {"role": "user", "content": "a cat on a sofa"}
    assert "image model" in system["content"] and "lens" in system["content"]
    assert "8k" in system["content"]  # named as banned
    assert "natural sentences" in system["content"]  # Nano Banana hint
    assert route.calls[0].request.headers["Authorization"] == "Bearer fake-key"

    rows = _entries()
    assert len(rows) == 1
    assert rows[0]["provider"] == "openrouter" and rows[0]["kind"] == "chat"
    assert rows[0]["cost_usd"] == 0.0004 and rows[0]["is_estimate"] is False

    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        assert db.query(models.SpendEntry).one().note == "prompt enhance"
    finally:
        db.close()


@respx.mock
def test_each_kind_gets_its_own_guide_and_model_hint(auth_client, monkeypatch):
    _live(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("better"))
    cases = [("video", "bytedance/seedance-2", ["one continuous shot", "slow motion", "camera and motion"]),
             ("video", "", ["one continuous shot"]),
             ("script", "", ["hook in the first line", "never invent", "15%"]),
             ("scene", "sogni/flux1-krea", ["No people unless", "comma-separated"]),
             ("image", "openai/gpt-image-2", ["Photoreal unless", "natural sentences"])]
    for kind, model, needles in cases:
        r = auth_client.post("/api/prompts/enhance", json={"text": "x", "kind": kind, "model": model or None})
        assert r.status_code == 200, r.text
        system = json.loads(route.calls[-1].request.content)["messages"][0]["content"]
        for n in needles:
            assert n in system, (kind, model, n)
    # No hint when the model family is unknown or for a script.
    from app.providers.openrouter_chat import model_family_hint

    assert model_family_hint("image", "acme/mystery") == ""
    assert model_family_hint("script", "bytedance/seedance-2") == ""


@respx.mock
def test_enhance_upstream_failure_is_a_502_and_bills_nothing(auth_client, monkeypatch):
    from .test_spend import _entries

    _live(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(500, json={"error": "boom"}))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a cat", "kind": "image"})
    assert r.status_code == 502
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(
        200, json={"id": "e", "error": {"message": "nope", "code": 400}}))
    assert auth_client.post("/api/prompts/enhance", json={"text": "a cat", "kind": "image"}).status_code == 502
    assert _entries() == []  # an HTTP error or an error body is not a billable completion
    # A blank completion was still billed: recorded, but the caller gets a 502.
    respx.post(f"{OR}/chat/completions").mock(return_value=_reply("   "))
    assert auth_client.post("/api/prompts/enhance", json={"text": "a cat", "kind": "image"}).status_code == 502
    assert len(_entries()) == 1


@respx.mock
def test_enhance_malformed_choices_is_a_502_not_a_500(auth_client, monkeypatch):
    _live(monkeypatch)
    for payload in (
        {"id": "e", "choices": []},
        {"id": "e", "choices": [{}]},
        {"id": "e", "choices": [{"message": None}]},
        {"id": "e", "choices": [{"message": {"content": None}}]},
        {"id": "e", "choices": None},
    ):
        respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(200, json=payload))
        r = auth_client.post("/api/prompts/enhance", json={"text": "a cat", "kind": "image"})
        assert r.status_code == 502, payload
        assert "Couldn't enhance the prompt" in r.json()["detail"]


@respx.mock
def test_enhance_is_blocked_over_budget(auth_client, monkeypatch):
    from .test_spend import _add

    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    _add(1.5)
    _live(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("x"))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a cat", "kind": "image"})
    assert r.status_code == 402 and "Monthly budget" in r.json()["detail"]
    assert not route.called


def test_clean_enhanced_strips_wrappers():
    from app.providers.openrouter_chat import clean_enhanced

    assert clean_enhanced("```\nA cat.\n```") == "A cat."
    assert clean_enhanced("Improved prompt: A cat.") == "A cat."
    assert clean_enhanced("“A cat.”") == "A cat."
    assert clean_enhanced("'A cat.'") == "A cat."
    assert len(clean_enhanced("y" * 5000)) == 4000


def test_mock_enhance_is_deterministic():
    from app.providers.mock_chat import MockChatProvider

    p = MockChatProvider()
    once = p.enhance_prompt("a cat.", "image")
    assert once == p.enhance_prompt("a cat.", "image") == "a cat — soft natural light, 35mm, shallow depth of field"
    assert p.enhance_prompt(once, "image") == once  # not stacked on a second press
    with pytest.raises(ValueError):
        p.enhance_prompt("x", "music")


# ---------- presets ----------


def test_presets_crud(auth_client):
    assert auth_client.get("/api/prompts/presets").json() == []
    r = auth_client.post("/api/prompts/presets", json=PRESET)
    assert r.status_code == 201, r.text
    first = r.json()
    assert {k: first[k] for k in PRESET} == PRESET and first["id"] and first["created_at"]
    second = auth_client.post("/api/prompts/presets", json={**PRESET, "name": "Clip", "kind": "video",
                                                            "aspect_ratio": "", "negative_prompt": ""}).json()
    listed = auth_client.get("/api/prompts/presets").json()
    assert [p["id"] for p in listed] == [second["id"], first["id"]]  # newest first

    r = auth_client.put(f"/api/prompts/presets/{first['id']}", json={**PRESET, "name": "Renamed",
                                                                      "aspect_ratio": "1:1"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed" and r.json()["aspect_ratio"] == "1:1"
    assert r.json()["id"] == first["id"] and r.json()["created_at"] == first["created_at"]

    assert auth_client.delete(f"/api/prompts/presets/{first['id']}").status_code == 204
    assert [p["id"] for p in auth_client.get("/api/prompts/presets").json()] == [second["id"]]
    assert auth_client.delete(f"/api/prompts/presets/{first['id']}").status_code == 404
    assert auth_client.put("/api/prompts/presets/nope", json=PRESET).status_code == 404


@pytest.mark.parametrize("patch", [
    {"name": ""}, {"name": "   "}, {"name": "n" * 81}, {"prompt": ""}, {"prompt": "p" * 4001},
    {"kind": "script"}, {"aspect_ratio": "4:3"}, {"negative_prompt": "z" * 1001},
])
def test_preset_validation(auth_client, patch):
    assert auth_client.post("/api/prompts/presets", json={**PRESET, **patch}).status_code == 422


def test_preset_defaults_and_cap(auth_client, monkeypatch):
    from app.services import prompting

    r = auth_client.post("/api/prompts/presets", json={"name": "Min", "kind": "video", "prompt": "waves"})
    assert r.status_code == 201
    assert {k: r.json()[k] for k in ("negative_prompt", "model", "aspect_ratio")} == \
        {"negative_prompt": "", "model": "", "aspect_ratio": ""}

    monkeypatch.setattr(prompting, "PRESETS_CAP", 3)
    for i in range(2):
        assert auth_client.post("/api/prompts/presets", json={**PRESET, "name": f"p{i}"}).status_code == 201
    r = auth_client.post("/api/prompts/presets", json=PRESET)
    assert r.status_code == 409 and "3 presets" in r.json()["detail"]
    assert len(auth_client.get("/api/prompts/presets").json()) == 3


def test_preset_cap_is_200():
    from app.services import prompting

    assert prompting.PRESETS_CAP == 200


# ---------- auth ----------


def test_every_prompts_route_requires_auth(client):
    calls = [("get", "/api/prompts/defaults", None),
             ("put", "/api/prompts/defaults", {"image": "", "video": ""}),
             ("post", "/api/prompts/enhance", {"text": "a cat", "kind": "image"}),
             ("get", "/api/prompts/presets", None),
             ("post", "/api/prompts/presets", PRESET),
             ("put", "/api/prompts/presets/abc", PRESET),
             ("delete", "/api/prompts/presets/abc", None)]
    for method, url, body in calls:
        kw = {"json": body} if body is not None else {}
        assert getattr(client, method)(url, **kw).status_code == 401, (method, url)


def test_reset_defaults_returns_to_the_built_ins(auth_client):
    from app.services.prompting import BUILTIN_NEGATIVES

    auth_client.put("/api/prompts/defaults", json={"image": "only this", "video": ""})
    assert auth_client.get("/api/prompts/defaults").json() == {"image": "only this", "video": ""}
    r = auth_client.delete("/api/prompts/defaults")
    assert r.status_code == 200 and r.json() == BUILTIN_NEGATIVES
    assert auth_client.get("/api/prompts/defaults").json() == BUILTIN_NEGATIVES


# ---------- adult mode and refusals ----------

DS = "deepseek/deepseek-v4-flash"
DS_PRO = "deepseek/deepseek-v4-pro"
REFUSAL = "I can't write a prompt for explicit sexual content, but here's a version for a non-explicit beach scene: ..."


def _models_asked(route) -> list[str]:
    return [json.loads(c.request.content)["model"] for c in route.calls]


@respx.mock
def test_adult_mode_enhances_with_deepseek_and_off_uses_the_planner(auth_client, monkeypatch):
    _live(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("A rewritten prompt."))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a beach at dusk", "kind": "image"})
    assert r.json()["model"] == "test/planner" and _models_asked(route) == ["test/planner"]
    assert auth_client.put("/api/settings/content", json={"adult": True}).json()["adult"] is True
    r = auth_client.post("/api/prompts/enhance", json={"text": "a beach at dusk", "kind": "image"})
    assert r.status_code == 200 and r.json() == {"text": "A rewritten prompt.", "model": DS}
    system = json.loads(route.calls[-1].request.content)["messages"][0]["content"]
    assert "REFUSED_MINOR" in system and "DECLINED" in system and "adult" in system


@respx.mock
def test_outside_adult_mode_a_decline_is_the_answer_not_retried(auth_client, monkeypatch):
    _live(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply(REFUSAL))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a beach scene", "kind": "image"})
    assert r.status_code == 422 and "Adult mode" in r.json()["detail"] and len(route.calls) == 1


@respx.mock
def test_in_adult_mode_a_decline_is_retried_once_then_reported(auth_client, monkeypatch):
    _live(monkeypatch, adult=True)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[_reply("DECLINED"), _reply("The rewrite.")])
    r = auth_client.post("/api/prompts/enhance", json={"text": "a beach scene", "kind": "image"})
    assert r.status_code == 200 and r.json() == {"text": "The rewrite.", "model": DS_PRO}
    assert _models_asked(route) == [DS, DS_PRO]
    route.mock(side_effect=[_reply(REFUSAL), _reply("I'm sorry, but I can't rewrite this prompt.")])
    r = auth_client.post("/api/prompts/enhance", json={"text": "a beach scene", "kind": "image"})
    assert r.status_code == 422 and "declined" in r.json()["detail"] and "unchanged" in r.json()["detail"]


@respx.mock
def test_a_minors_refusal_is_never_retried(auth_client, monkeypatch):
    _live(monkeypatch, adult=True)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("REFUSED_MINOR"))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a beach scene", "kind": "image"})
    assert r.status_code == 422 and "sexualise a minor" in r.json()["detail"] and len(route.calls) == 1
    route.mock(return_value=_reply("I can't create content that sexualizes a minor or childlike traits."))
    n = len(route.calls)
    assert auth_client.post("/api/prompts/enhance", json={"text": "a beach scene", "kind": "image"}).status_code == 422
    assert len(route.calls) == n + 1  # words about a minor: no second model asked


@pytest.mark.parametrize("reply", [
    "I can't rewrite this prompt because it describes explicit sexual activity. Here's an alternative: ...",
    "I'm unable to rewrite this prompt.",
    "Sorry, I can't do this.",
    "I don't write sexually explicit content, but here's a beach scene",
    "I won't be able to rewrite this.",
    "I apologize, but I can't help with that request.",
    "I’m sorry — I can’t help with this.",
    "I'm not going to write that.",
    "That's not something I can help with.",
    "I can not write that.",
    "As an AI, I can't create explicit sexual content.",
    "I understand you'd like a sensual scene, but I can't produce sexually explicit material.",
    "This request goes against my guidelines.",
    "DECLINED",
    "**DECLINED**",
])
def test_refusal_wordings_are_caught(reply):
    from app.services.prompting import refusal

    assert refusal(reply, "a couple on a beach") == "declined"


@pytest.mark.parametrize("text,out", [
    ("I can't make this up: our sourdough sold out in 12 minutes.", "I can't make this up: our sourdough sold out in 12 minutes. Come early."),
    ("I can't help it, I love this coffee.", "I can't help it: I love this coffee."),
    ("she is excited", "She smiles. \"I can't wait to see the ocean,\" she says."),
    ("an ad for a stroller", "A sleek stroller rolls through a park at golden hour."),
    ("PureGlow: the safer alternative for bath time.",
     "Bath time should be gentle. PureGlow is a safer alternative made for children's sensitive skin."),
    ("Boudoir portrait, tasteful take on vintage pin-up", "A tasteful take on vintage pin-up: soft light, satin."),
    ("Declined. Again. Third time this week.", "Declined. Again. That's the third time this week my card got rejected."),
    ("I can't do this anymore. Then I found FocusTea.", "I can’t do this anymore. Then I found FocusTea, calm focus."),
    ("I can't describe how good this coffee is.", "\"I can't describe how good this coffee is,\" she says."),
])
def test_faithful_rewrites_are_not_refusals(text, out):
    from app.services.prompting import refusal

    assert refusal(out, text) is None


@respx.mock
def test_minors_are_refused_before_any_call_and_in_the_rewrite(auth_client, monkeypatch):
    _live(monkeypatch, adult=True)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("nude photo of a teenage girl"))
    r = auth_client.post("/api/prompts/enhance", json={"text": "naked teenage girl", "kind": "image"})
    assert r.status_code == 422 and "refused" in r.json()["detail"] and not route.calls
    r = auth_client.post("/api/prompts/enhance", json={"text": "a woman on a beach", "kind": "image"})
    assert r.status_code == 422 and "rewrite was refused" in r.json()["detail"]


@respx.mock
def test_scripts_get_the_script_check(auth_client, monkeypatch):
    _live(monkeypatch, adult=True)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply(
        "My daughter and I opened this lingerie boutique ten years ago. Come see the new collection."))
    r = auth_client.post("/api/prompts/enhance", json={
        "text": "My daughter and I started this lingerie boutique ten years ago. Come see the new collection.",
        "kind": "script"})
    assert r.status_code == 200, r.text  # what generating that script accepts
    assert len(route.calls) == 1


@respx.mock
def test_enhance_follows_the_openrouter_switch(auth_client, monkeypatch):
    from app.services import provider_keys

    _live(monkeypatch, adult=True)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("x"))
    provider_keys.set_enabled("openrouter", False)
    r = auth_client.post("/api/prompts/enhance", json={"text": "a beach", "kind": "image"})
    assert r.status_code == 409 and not route.calls


def test_mock_mode_ignores_adult_mode(auth_client):
    auth_client.put("/api/settings/content", json={"adult": True})
    r = auth_client.post("/api/prompts/enhance", json={"text": "a cat on a sofa", "kind": "image"})
    assert r.status_code == 200 and r.json()["text"].startswith("a cat on a sofa")


def test_a_decline_naming_a_minor_is_a_minors_refusal():
    from app.services.prompting import refusal

    assert refusal("DECLINED: this sexualises a 15-year-old.", "x") == "minor"
    assert refusal("REFUSED_MINOR", "x") == "minor"
    assert refusal("I can't create content that sexualizes a minor.", "x") == "minor"
    assert refusal("DECLINED", "a high school dance") == "minor"  # the prompt mentions one: never retried


@respx.mock
def test_scripts_get_the_minors_check_too(auth_client, monkeypatch):
    _live(monkeypatch, adult=True)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("x"))
    r = auth_client.post("/api/prompts/enhance", json={"text": "naked teenage girl in her bedroom", "kind": "script"})
    assert r.status_code == 422 and "refused" in r.json()["detail"] and not route.calls
    route.mock(return_value=_reply("A 14-year-old strips for the camera."))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a dancer warms up", "kind": "script"})
    assert r.status_code == 422 and "rewrite was refused" in r.json()["detail"]


@respx.mock
def test_an_empty_rewrite_is_a_502(auth_client, monkeypatch):
    _live(monkeypatch, adult=True)
    respx.post(f"{OR}/chat/completions").mock(return_value=_reply("   "))
    r = auth_client.post("/api/prompts/enhance", json={"text": "a short ad", "kind": "script"})
    assert r.status_code == 502 and "empty" in r.json()["detail"]
