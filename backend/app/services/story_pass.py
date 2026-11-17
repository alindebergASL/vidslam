"""A signed story pass: check_story's verdict, the only way past the story guards.

story_script.check_story judges a whole story (every line, with its speaker
and any delivery instructions, and every sound, bed and music prompt, in
windows, the facts and cast in front). It returns a pass: an HMAC, with the
app's secret key, over

- a hash of each line ("SPEAKER: text", plus its delivery instructions:
  line_item) and each prompt (prompt_item) it passed, normalised
  (story_script.normalize, whitespace collapsed);
- the hash of the context it judged them with (the story note, the facts
  line and the cast header);
- a hash of the whole normalised story (every item, the cast, the title,
  premise and music prompts: what the facts were worked out from);
- the judge (minor_check._identity: the planner that answered) and
  POLICY_VERSION;
- the voice each cast member is read with (check_story's `voices`: a voice
  id, and the voice provider when given; none given is the default voice,
  ""): the voice guard sends a line only with its speaker's voice;
- whether the explicit-content check of a clean deployment passed it
  ("clean"): the guards then don't ask again for what the pass covers;
- an expiry.

A provider guard on the story path sends a line or prompt only when the
pass covers it: the same speaker, text and instructions (or prompt) with
the same context, from the same judge and policy. Anything else (an edited
line, a new speaker, new instructions, a different cast, a forged or
expired pass, another planner) is refused (STALE): the caller runs
check_story on the current story again (its verdict and facts caches make
an unchanged story cheap).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict

# How long a pass is good for: a story is voiced and rendered within it.
TTL_SECONDS = 6 * 3600
VERSION = 3
# Bump when what check_story judges, or how, changes: older passes stop
# covering anything.
POLICY_VERSION = "story-pass-5"
_DIGEST = 16  # bytes of each item's hash kept in the pass

STALE = "This part changed since the story was checked. Check the story again."
WRONG_VOICE = ("{speaker}'s lines can only be read with the voice the story was checked with. Check the story "
               "again with this voice.")

# Verified passes, keyed by (key fingerprint, token): a pass read with an
# older key (the secret rotated) is never answered from here.
_seen: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
_lock = threading.Lock()


def _key() -> bytes:
    from ..config import get_settings

    secret = str(get_settings().session_secret or "").encode("utf-8")
    return hmac.new(secret, b"story-pass-v2", hashlib.sha256).digest()


def _fingerprint(key: bytes) -> str:
    return hashlib.sha256(b"fp\0" + key).hexdigest()[:32]


def _norm(text: str) -> str:
    from .story_script import normalize

    return " ".join(normalize(text or "").split())


def line_item(speaker: str, text: str, instructions: str = "") -> str:
    """What a pass holds for one story line: who says it, the words (tags
    kept) and the delivery instructions sent with it."""
    who = " ".join((speaker or "").split()).upper()
    said = " ".join((instructions or "").split())
    return f"line\0{who}\0{' '.join((text or '').split())}\0{said}"


def prompt_item(prompt: str) -> str:
    """What a pass holds for one sound, bed or music prompt."""
    return f"prompt\0{' '.join((prompt or '').split())}"


def item_hash(text: str) -> bytes:
    """The hash a pass keeps for one item (line_item or prompt_item)."""
    return hashlib.sha256(_norm(text).encode("utf-8")).digest()[:_DIGEST]


def context_hash(context: str) -> str:
    """The hash of a story context (note, facts and cast)."""
    return hashlib.sha256(_norm(context).encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    if not s or len(s) % 4 == 1 or not all(c.isalnum() or c in "-_" for c in s):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def current_judge() -> str:
    """Who judges stories now (minor_check._identity), "" when unknown."""
    from .minor_check import _identity

    return _identity() or ""


def voice_of(voice) -> tuple[str, str]:  # noqa: ANN001
    """(provider, voice id) from what a caller gives for a cast member: a
    voice id, a (provider, voice id) pair, or {"voice_id", "provider"}."""
    if isinstance(voice, dict):
        provider, vid = voice.get("provider"), voice.get("voice_id", voice.get("voice"))
    elif isinstance(voice, (list, tuple)) and len(voice) == 2:
        provider, vid = voice
    else:
        provider, vid = "", voice
    return " ".join(str(provider or "").split()).lower(), " ".join(str(vid or "").split())


def _voice_hash(voice_id: str) -> str:
    return _b64(hashlib.sha256(("voice\0" + " ".join(str(voice_id or "").split())).encode("utf-8")).digest()[:12])


def issue(context: str, items, *, story: str = "", judge: str | None = None,  # noqa: ANN001
          ttl: int = TTL_SECONDS, now: float | None = None, clean: bool = False,
          voices: dict | None = None) -> str:
    """A pass covering every item (line_item / prompt_item) in `items`, read
    with `context`, for the story whose hash is `story`, judged by `judge`
    (default: the current one). `clean`: the explicit-content check of a
    clean deployment passed every item. `voices`: {SPEAKER: voice} (see
    voice_of) the lines are read with."""
    digests = sorted({item_hash(t) for t in items if (t or "").strip()})
    bound = {}
    for name, voice in (voices or {}).items():
        provider, vid = voice_of(voice)
        bound[" ".join(str(name or "").split()).upper()] = [provider, _voice_hash(vid)]
    payload = json.dumps({"v": VERSION, "pol": POLICY_VERSION,
                          "judge": current_judge() if judge is None else judge,
                          "story": story or "", "exp": int((now if now is not None else time.time()) + ttl),
                          "ctx": context_hash(context), "items": _b64(b"".join(digests)),
                          "clean": bool(clean), "voices": bound},
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def read(token: str) -> dict | None:
    """The fields of a pass whose signature is good with the current key
    ({"exp", "ctx", "story", "judge", "pol", "items"}); None for anything
    else."""
    token = str(token or "")
    key = _key()
    cache_key = (_fingerprint(key), token)
    with _lock:
        hit = _seen.get(cache_key)
        if hit is not None:
            _seen.move_to_end(cache_key)
            return hit
    try:
        body, sig = token.split(".")
        payload = _unb64(body)
        if not hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).digest(), _unb64(sig)):
            return None
        data = json.loads(payload)
        if data.get("v") != VERSION:
            return None
        raw = _unb64(str(data["items"])) if data["items"] else b""
        if len(raw) % _DIGEST:
            return None
        voices = data.get("voices") or {}
        if not isinstance(voices, dict):
            return None
        out = {"exp": int(data["exp"]), "ctx": str(data["ctx"]), "story": str(data["story"]),
               "judge": str(data["judge"]), "pol": str(data["pol"]),
               "items": frozenset(raw[i:i + _DIGEST] for i in range(0, len(raw), _DIGEST)),
               "clean": data.get("clean") is True,
               "voices": {str(k): (str(v[0]), str(v[1])) for k, v in voices.items()
                          if isinstance(v, list) and len(v) == 2}}
    except Exception:  # noqa: BLE001 — anything unreadable is no pass
        return None
    with _lock:
        _seen[cache_key] = out
        while len(_seen) > 64:
            _seen.popitem(last=False)
    return out


def covers(token: str, context: str, item: str, *, now: float | None = None, judge: str | None = None) -> bool:
    """Whether `token` is a good, unexpired pass, from this policy and the
    current judge (or `judge`), for `item` (line_item / prompt_item) read
    with `context` (both normalised)."""
    if not token or not (item or "").strip() or not (context or "").strip():
        return False
    data = read(token)
    if data is None:
        return False
    if data["exp"] <= (now if now is not None else time.time()):
        return False
    if data["pol"] != POLICY_VERSION:
        return False
    if data["judge"] != (current_judge() if judge is None else judge):
        return False
    if not hmac.compare_digest(data["ctx"], context_hash(context)):
        return False
    return item_hash(item) in data["items"]


def voice_bound(token: str, speaker: str, voice_id: str, provider: str = "", *,
                default_voice: str | None = None) -> bool:
    """Whether `voice_id` (the voice the line is really read with) is the
    voice the pass binds `speaker` to. When the binding names a provider,
    `provider` (the guard's) must be it. A speaker checked with the default
    voice ("", no voice given) may be read with the provider's own default
    (`default_voice`, what "" resolves to) unless that voice is bound to
    another speaker of the story (a child's voice for an adult's lines)."""
    data = read(token)
    if data is None:
        return False
    who = " ".join((speaker or "").split()).upper()
    provider = " ".join(str(provider or "").split()).lower()
    bound_provider, bound = data["voices"].get(who, ("", _voice_hash("")))
    if bound_provider and bound_provider != provider:
        return False
    vid = " ".join(str(voice_id or "").split())
    if hmac.compare_digest(bound, _voice_hash(vid)):
        return True
    if default_voice is None or not hmac.compare_digest(bound, _voice_hash("")):
        return False
    if vid != " ".join(str(default_voice or "").split()):
        return False
    mine = _voice_hash(vid)
    return not any(hmac.compare_digest(h, mine) for name, (p, h) in data["voices"].items()
                   if name != who and (not p or not provider or p == provider))


def clean_checked(token: str) -> bool:
    """Whether a good pass says the explicit-content check of a clean
    deployment passed what it covers."""
    data = read(token)
    return bool(data and data["clean"])


def story_of(token: str) -> str:
    """The story hash a good pass was issued for ("" for anything else)."""
    data = read(token)
    return data["story"] if data else ""


def of(context: str, story_pass: str = "") -> str:
    """The pass a guard was handed: `story_pass`, else the one check_story
    attached to its context (story_script.StoryCheck)."""
    return story_pass or str(getattr(context, "story_pass", "") or "")
