"""Deterministic in-memory training provider for tests + mock-mode demos.

Trains "instantly": submit() returns a job id; poll() returns succeeded
on the second call (or first if the synthetic step count is zero). The
"trained model id" is a hash of the input asset ids so the same training
inputs reliably reproduce the same model id — useful for tests that
assert idempotency.

Doesn't actually run any ML — the point is to make every code path that
*touches* training exercisable without a GPU or external API key, so the
real providers drop in by implementing the same Protocol."""
from __future__ import annotations

import hashlib
from collections import defaultdict

from .base import TrainingAsset, TrainingStatus, TrainingSubmit


class MockTrainingProvider:
    # Per-job poll counter, incremented each poll. We mark the job as
    # "succeeded" on the *second* poll so test code can observe the
    # "training" intermediate state at least once.
    def __init__(self) -> None:
        self._poll_counts: dict[str, int] = defaultdict(int)
        self._asset_ids_for_job: dict[str, list[int]] = {}

    def list_kinds(self) -> list[str]:
        return ["character_lora", "style_lora", "voice_clone"]

    def submit(
        self,
        *,
        kind: str,
        name: str,
        assets: list[TrainingAsset],
        config: dict,
    ) -> TrainingSubmit:
        # Job id is deterministic so test asserts can match against it.
        ids_part = "-".join(str(a.asset_id) for a in assets)
        digest = hashlib.sha256(f"{kind}|{name}|{ids_part}".encode()).hexdigest()[:16]
        job_id = f"mock-train-{digest}"
        self._poll_counts[job_id] = 0
        self._asset_ids_for_job[job_id] = [a.asset_id for a in assets]
        return TrainingSubmit(
            provider="mock",
            provider_job_id=job_id,
            raw={"kind": kind, "name": name, "config": config},
        )

    def poll(self, job: TrainingSubmit) -> TrainingStatus:
        self._poll_counts[job.provider_job_id] += 1
        count = self._poll_counts[job.provider_job_id]
        if count < 2:
            # ~50% through the first "stage" so the UI progress bar moves.
            return TrainingStatus(state="training", progress=0.5)
        # Mint a stable model id reflecting the training inputs.
        ids_part = "-".join(
            str(i) for i in self._asset_ids_for_job.get(job.provider_job_id, [])
        )
        digest = hashlib.sha256(
            f"{job.provider_job_id}|{ids_part}".encode()
        ).hexdigest()[:24]
        return TrainingStatus(
            state="succeeded",
            progress=1.0,
            provider_model_id=f"mock/{digest}",
            cost_usd=0.0,
        )

    def cancel(self, job: TrainingSubmit) -> bool:
        # Pretend the mock provider supports cancel only while still
        # "training". Once succeeded, cancel is a no-op.
        if self._poll_counts.get(job.provider_job_id, 0) < 2:
            self._poll_counts[job.provider_job_id] = 10**9  # force "succeeded" next poll? no — sentinel
            return True
        return False
