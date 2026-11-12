"""The hook in the plan: StoryboardPlan.hook, derived for old plans, asked
for by the planner prompt, kept by "keep my edits" and editable."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize("text,first,rest", [
    ("Stop scrolling. Here is why.", "Stop scrolling.", "Here is why."),
    ("Ever wondered why? Here's the answer!", "Ever wondered why?", "Here's the answer!"),
    ("This changes everything! Really.", "This changes everything!", "Really."),
    ("Wait for it… then laugh.", "Wait for it…", "then laugh."),
    ("Wait for it... then laugh.", "Wait for it...", "then laugh."),
    ('"Never again," she said. "Never." Then left.', '"Never again," she said.', '"Never." Then left.'),
    ("“Is it real?” Yes it is.", "“Is it real?”", "Yes it is."),
    ("It costs $3.50 a day. Cheap.", "It costs $3.50 a day.", "Cheap."),
    ("no punctuation at all", "no punctuation at all", ""),
    ("First line\nSecond line.", "First line", "Second line."),
    ("  ", "", ""),
    ("What?! No way.", "What?!", "No way."),
])
def test_first_sentence(text, first, rest):
    from app.services.hook_lab import first_sentence, rest_of_script, split_first_sentence

    assert split_first_sentence(text) == (first, rest)
    assert first_sentence(text) == first and rest_of_script(text) == rest


def test_replace_first_sentence():
    from app.services.hook_lab import replace_first_sentence

    assert replace_first_sentence("Old hook. The rest.", "New hook?") == "New hook? The rest."
    assert replace_first_sentence("Only a hook", "New one!") == "New one!"


def test_old_plans_get_the_first_sentence_as_hook():
    from app.schemas.storyboard import StoryboardPlan
    from app.services.hook_lab import plan_hook

    old = {"title": "t", "cleaned_voice_script": "Did you know? Cats sleep a lot.", "estimated_duration_seconds": 10}
    assert StoryboardPlan.model_validate(old).hook == "Did you know?"
    assert plan_hook(old) == "Did you know?"
    # A hook that isn't the script's first sentence is never voiced: the
    # plan keeps the first sentence instead.
    assert StoryboardPlan.model_validate({**old, "hook": " Mine. "}).hook == "Did you know?"
    assert plan_hook({**old, "hook": "Mine."}) == "Did you know?"


def test_a_planners_hook_is_matched_to_the_script_and_safety_checked(monkeypatch):
    """The planner's hook field is replaced by the script's first sentence,
    and the plan's safety checks look at the hook too."""
    from app.schemas.storyboard import StoryboardPlan
    from app.services import pipeline, safety

    plan = StoryboardPlan.model_validate({
        "title": "t", "cleaned_voice_script": "Cats nap. All day long.", "estimated_duration_seconds": 5,
        "hook": "Something the voice never says."})
    assert plan.hook == "Cats nap."
    seen: list[str] = []
    monkeypatch.setattr(safety, "check_script", lambda text: seen.append(text))
    monkeypatch.setattr("app.services.minor_check.check_generation", lambda *a, **k: None)
    built = StoryboardPlan.model_construct(**{**plan.model_dump(), "hook": "An unvalidated hook."})
    pipeline._check_plan(built)
    assert "An unvalidated hook." in seen


def test_planner_prompt_asks_for_the_hook():
    from app.providers import openrouter_chat

    text = (Path(openrouter_chat.__file__).parent.parent / "prompts" / "storyboard_system.md").read_text()
    assert '"hook": "...",' in text
    assert "exact first sentence of `cleaned_voice_script`" in text and "stop the scroll" in text


def _planned(auth_client, script="Stop scrolling now! Here is the rest. More words.") -> int:
    pid = auth_client.post("/api/projects", json={"title": "H", "original_script": script,
                                                  "target_duration_seconds": 6}).json()["id"]
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    return pid


def _plan(auth_client, pid) -> dict:
    return auth_client.get(f"/api/projects/{pid}").json()["generated_plan_json"]


def test_new_plans_store_the_hook(auth_client):
    pid = _planned(auth_client)
    plan = _plan(auth_client, pid)
    assert plan["hook"] == "Stop scrolling now!"


def test_hook_edit_replaces_the_opening_line_and_script_edit_moves_the_hook(auth_client):
    pid = _planned(auth_client)
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"hook": "What if you never scrolled again?"})
    assert r.status_code == 200, r.text
    plan = _plan(auth_client, pid)
    assert plan["hook"] == "What if you never scrolled again?"
    assert plan["cleaned_voice_script"].startswith("What if you never scrolled again? Here is the rest.")
    assert "Stop scrolling now!" not in plan["cleaned_voice_script"]

    auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "Brand new start. Then more."})
    plan = _plan(auth_client, pid)
    assert plan["hook"] == "Brand new start." and plan["cleaned_voice_script"] == "Brand new start. Then more."

    # The same hook again changes nothing; an empty one is ignored.
    auth_client.patch(f"/api/projects/{pid}/plan", json={"hook": "Brand new start."})
    auth_client.patch(f"/api/projects/{pid}/plan", json={"hook": ""})
    assert _plan(auth_client, pid)["cleaned_voice_script"] == "Brand new start. Then more."

    # Both sent, with a changed hook: the hook opens the sent script.
    auth_client.patch(f"/api/projects/{pid}/plan",
                      json={"cleaned_voice_script": "Old open. Middle bit.", "hook": "Fresh open!"})
    plan = _plan(auth_client, pid)
    assert plan["cleaned_voice_script"] == "Fresh open! Middle bit." and plan["hook"] == "Fresh open!"


def test_hook_edit_goes_through_the_minors_check(auth_client, monkeypatch):
    from fastapi import HTTPException

    from app.services import minor_check

    seen = []

    def refuse(text, negative="", *, kind="video"):
        seen.append((text, kind))
        raise HTTPException(422, "refused")

    monkeypatch.setattr(minor_check, "check_or_422", refuse)
    pid = _planned(auth_client)
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"hook": "A brand new opening."})
    assert r.status_code == 422
    assert seen and seen[0][1] == "script" and seen[0][0].startswith("A brand new opening.")
    assert _plan(auth_client, pid)["hook"] == "Stop scrolling now!"


def test_keep_my_edits_keeps_the_hook(auth_client):
    pid = _planned(auth_client)
    auth_client.patch(f"/api/projects/{pid}/plan", json={"hook": "My own opening!"})
    r = auth_client.post(f"/api/projects/{pid}/generate-plan", params={"preserve_edits": "true"})
    assert r.status_code == 202
    plan = _plan(auth_client, pid)
    assert plan["hook"] == "My own opening!" and plan["cleaned_voice_script"].startswith("My own opening!")


def test_keep_my_edits_on_a_plan_without_a_hook_derives_it_from_the_kept_script(auth_client):
    from app import models
    from app.db import SessionLocal

    pid = _planned(auth_client)
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, pid)
        plan = dict(p.generated_plan_json)
        plan.pop("hook", None)
        plan["cleaned_voice_script"] = "An edited opening? And the rest."
        p.generated_plan_json = plan
        db.commit()
    finally:
        db.close()
    auth_client.post(f"/api/projects/{pid}/generate-plan", params={"preserve_edits": "true"})
    assert _plan(auth_client, pid)["hook"] == "An edited opening?"
