"""App-written sound and music prompts, vetted once instead of on every call.

The ambient presets (ambient.preset_audio_prompts: "steady gentle rain on
leaves and ground...", the spa music) are fixed text in the code. Asking
the safety check model about them on every scene made an ordinary rain
bed wait whenever OpenRouter was out of credit or slow, though the words
can never change. So each preset is vetted at test time instead
(tests/test_check_availability.py reads every one: no minor, nothing sexual, and
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
    "04dcf1ef58397681af04baedde59f13e622d33831c435af3afc0a32c1084a01e",
    "0ce4df5bb616acbaee7db8223f48fd0be0614b8ecc8078ea17086b357a373e0c",
    "1a602e4f4ea3357f199663e337824238f9cf2deb47a7d8de2ff741d9841943ef",
    "1d93b743e8cb559c3b4614d3494755b9362a215c890b876b9ff87222f6b95e9b",
    "1f291e82fe78fe1a15aaabb012f37280cee2358b80e49a74354d1c40c317eadb",
    "2155a1da96a0c32d96fb02bdbeab2b2d66a245c7db6e25dd185bc3f37a5282d2",
    "39ae9a230ba1c70b0110a597efcfdf612547ab034c62afdffd0715699da39f01",
    "44431af0415873a90291d6a1d8ec65f4d3ffd2c4f5b1e885ee3efcc74ff0f946",
    "8bf823d45750a03fb8e7879c2ad31207399905f8c79df76eed095b847471bd3c",
    "a50fe846a737835181d7f6749170bddcbbce8fe1acf4e1ef44d706de1e045b18",
    "d9c2f851bc58b5ec19f5b1cd5bd1c7cc01f26b3de5a59c58f5758d156f57e590",
    "dd92b2e12ed2340fd233a8ddcd6df932e252800be7a0d24fdc6c38e758050de2",
    "ed0e54b3ee4633f8f06aabf236cc389d5119d19b69017d9c6d1e8ab3b7400afa",
    "f104f71dd0e67173cadcca3168e6bc98a4577005c435634f4a8268f59c8721c6",
    "f10591638cf0ac10fac4c4fb920aca857e6f4059a7f66a25a90233c5165d20b1",
})


def is_vetted(text: str) -> bool:
    """`text` is, exactly, an app-written prompt vetted under this policy."""
    return bool(text) and vetted_hash(text) in VETTED
