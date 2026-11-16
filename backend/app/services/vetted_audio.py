"""App-written sound and music prompts, vetted once instead of on every call.

The ambient presets (ambient.preset_audio_prompts: "steady gentle rain on
leaves and ground...", the spa music) are fixed text in the code. Asking
the safety check model about them on every scene made an ordinary rain
bed wait whenever OpenRouter was out of credit or slow, though the words
can never change. So each preset is vetted at test time instead
(tests/test_vetted_audio.py reads every one: no minor, nothing sexual, and
the checks pass it), and its verdict kept here as a hash of the policy
version and the EXACT text.

Only an exact match counts: the same words with anything added, removed or
changed (a user's own prompt that starts with a preset, a different case,
an extra space, lyrics or a plan joined to it) hash differently and get the
full checks. A user who types a preset word for word sends exactly the
text that was vetted. A new or changed preset, or a new policy version,
fails the test until its hash is added here (after reading the text).
"""
from __future__ import annotations

import hashlib

from .minor_check import POLICY_VERSION

# Bump to re-vet every preset (the hashes below then stop matching).
VETTING = "audio-presets-1"


def vetted_hash(text: str) -> str:
    """The key of an exact app-written prompt under the current policy."""
    return hashlib.sha256(f"{POLICY_VERSION}\0{VETTING}\0{text}".encode()).hexdigest()


# Every preset of ambient.preset_audio_prompts(), as vetted_hash gives it.
VETTED: frozenset[str] = frozenset({
    "3f7d8df6ba991a568983ecff859878eba7bc2b4bad138fe9fd7306be01556bb8",
    "77b316d53f307b054fc829934a3e9480d69fcca2ee3b9ac8506b29affe666f81",
    "2e87be06606efa0c478d5307dc96d46780ef019686c297ac3adf037b05f3caee",
    "17f97d242b88a28e38199fd12481450f384a823209f696ee72758757d1e69145",
    "d4833b0454ef2389e7fc559174867c62b6a148778fa6c1258ec1c3b30012a409",
    "98988c99c39b02a08ee96cadf011a50d192280d55a2c5114172b273a188295fa",
    "e47fa803edba801a5aebb620d3359f88606748fe62c0b8aeb128c917cc8d0693",
    "8259c0d3bab82c6d89357bb3b1b908f5879ce2023cc93e8cbc690027446dca4b",
    "def2c7969510a5cde47d44654bd7c2263df8a1183c6a3685300ed428494daa9e",
    "840f6c05fcb8d2d9357fec85d9225e23ef6f02fe65b9c2c17f40b80b6bde4f88",
    "ffb026b89f12b510e48a195062c1230c7bf3d6bf01ebc77affe2d1dc356e24de",
    "a8e2fc87582f45d0faeb35d12b0042c80dad18e7d2a39fafb5ccdb190d2a438e",
    "09f958237cf08745f2cf2e7f8e5554e57f9f8c285000e597cf8a5ad8eb0cc99e",
    "3f7c9cf026c7b29c010486ca2a13c2081ac8e667a4e7d98468b3f9881cc9d2c1",
    "07f3e360abddf9f5c2606124fd9ee349e2788ee350d85f14109ddad58cbb1bf8",
})


def is_vetted(text: str) -> bool:
    """`text` is, exactly, an app-written prompt vetted under this policy."""
    return bool(text) and vetted_hash(text) in VETTED
