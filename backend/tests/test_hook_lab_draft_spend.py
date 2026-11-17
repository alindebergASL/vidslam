"""Hook lab drafts: their safety checks are paid, charged to the project and
rate limited (#57 review, finding 10).

Creating or changing a draft makes no voice or clip, but every opening is
judged by the safety check model (paid). Those calls ran with no spend
scope (the ledger couldn't say which project paid) and with no rate limit.
"""
from __future__ import annotations

from .test_hook_lab import _base, _create


def _scopes(monkeypatch) -> list[dict]:
    from app.providers import mock_chat
    from app.services import spend

    seen: list[dict] = []
    real = mock_chat.MockChatProvider.classify_minor

    def judge(self, text):  # noqa: ANN001, ANN202
        seen.append(dict(spend._scope.get() or {}))
        return real(self, text)

    monkeypatch.setattr(mock_chat.MockChatProvider, "classify_minor", judge)
    return seen


def test_draft_safety_checks_are_charged_to_the_project(auth_client, monkeypatch):
    from app.services import minor_check

    pid, base_id = _base(auth_client)
    seen = _scopes(monkeypatch)
    minor_check.reset_cache()
    made = _create(auth_client, pid, base_id, [{"hook_text": "Who pays for this check?", "style": "question"}])
    assert seen and all(s.get("project_id") == pid for s in seen)
    seen.clear()
    vid = made["variants"][0]["id"]
    r = auth_client.patch(f"/api/hook-variants/{vid}", json={"hook_text": "Who pays for the next check?"})
    assert r.status_code == 200, r.text
    assert seen and all(s.get("project_id") == pid for s in seen)


def test_draft_endpoints_are_rate_limited_lightly_in_a_bucket_of_their_own(auth_client, monkeypatch):
    from .test_rate_limit import _tighten_limits

    pid, base_id = _base(auth_client)
    _tighten_limits(monkeypatch, burst=1)
    url = f"/api/projects/{pid}/hook-lab/variants"
    codes, ids = [], []
    for i in range(10):
        r = auth_client.post(url, json={"base_render_id": base_id, "window_seconds": 2.5,
                                        "variants": [{"hook_text": f"Opening number {i} here?", "style": "question"}]})
        codes.append(r.status_code)
        if r.status_code == 201:
            ids.append(r.json()["variants"][0]["id"])
        else:
            break
    # A light limit: more room than the generation bucket's one call, then 429.
    assert codes[-1] == 429 and len(ids) > 1, codes
    assert auth_client.patch(f"/api/hook-variants/{ids[0]}", json={"hook_text": "Changed again?"}).status_code == 429
    # Its own bucket: the paid generation endpoints still have theirs.
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id})
    assert r.status_code == 200, r.text


def test_listen_charges_its_opening_check_to_the_project(auth_client, monkeypatch):
    """Listen (preview-audio) checks the opening before its voice is made:
    that check runs inside the project's spend scope, so its calls (when
    not cached) are charged to the project too."""
    from app.services import hook_lab, spend

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Who pays to listen?", "style": "question"}])
    vid = made["variants"][0]["id"]
    scopes: list[dict] = []
    real = hook_lab.check_opening

    def check(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        scopes.append(dict(spend._scope.get() or {}))
        return real(*a, **kw)

    monkeypatch.setattr(hook_lab, "check_opening", check)
    seen = _scopes(monkeypatch)
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 200, r.text
    assert scopes and all(s.get("project_id") == pid for s in scopes), scopes
    assert all(s.get("project_id") == pid for s in seen), seen
