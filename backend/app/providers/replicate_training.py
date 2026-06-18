"""Replicate-backed LoRA training adapter.

Trains a LoRA adapter on a Replicate hosted training model
(`replicate_lora_trainer`). Used for both `character_lora` (identity
preservation across shots) and `style_lora` (brand aesthetic transfer).

This adapter is **not exercised by the automated test suite** — it
talks to a real provider, costs money, and needs GPU credits. Verified
manually via `scripts/test_real_providers.md`.

The two endpoints we touch on Replicate:

  POST /v1/predictions  — start a training run with a zip of images.
  GET  /v1/predictions/{id}  — poll until status terminal.

When training completes, `provider_model_id` is the *version hash* of
the resulting LoRA — the inference path then passes that hash to the
image/video provider as `--model owner/model:hash`.
"""
from __future__ import annotations

import io
import logging
import zipfile

import httpx

from ..config import get_settings
from .base import TrainingAsset, TrainingStatus, TrainingSubmit

log = logging.getLogger("avs.training.replicate")


class ReplicateTrainingProvider:
    def __init__(self) -> None:
        self._settings = get_settings()
        if not self._settings.replicate_api_token:
            raise RuntimeError(
                "ReplicateTrainingProvider needs REPLICATE_API_TOKEN — registry should "
                "fall back to mock when the token isn't set."
            )

    def list_kinds(self) -> list[str]:
        return ["character_lora", "style_lora"]

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {self._settings.replicate_api_token}",
                "Content-Type": "application/json",
            },
        )

    def _bundle_assets(self, assets: list[TrainingAsset]) -> bytes:
        """Replicate LoRA trainers expect a single zip of training images.
        Build the zip in memory so we don't litter the data dir."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, a in enumerate(assets):
                with open(a.local_path, "rb") as fh:
                    ext = a.mime_type.rsplit("/", 1)[-1].replace("jpeg", "jpg")
                    zf.writestr(f"img_{i:03d}.{ext}", fh.read())
        return buf.getvalue()

    def submit(
        self,
        *,
        kind: str,
        name: str,
        assets: list[TrainingAsset],
        config: dict,
    ) -> TrainingSubmit:
        if kind not in self.list_kinds():
            raise ValueError(f"ReplicateTrainingProvider doesn't handle kind={kind}")
        if not assets:
            raise ValueError("Need at least one training asset")

        # Replicate expects training-image inputs as a publicly fetchable URL
        # *or* a zip uploaded to their files endpoint. We use the upload-zip
        # path — works regardless of whether the app is behind a public URL.
        zip_bytes = self._bundle_assets(assets)
        with self._client() as client:
            up = client.post(
                f"{self._settings.replicate_base_url}/files",
                content=zip_bytes,
                headers={"Content-Type": "application/zip"},
            )
            up.raise_for_status()
            zip_url = up.json()["urls"]["get"]

            inputs = {
                "input_images": zip_url,
                "trigger_word": (config.get("trigger_word") or name or "TOK"),
                "max_train_steps": int(config.get("steps", 1000)),
                "learning_rate": float(config.get("learning_rate", 4e-4)),
                "lora_rank": int(config.get("rank", 16)),
                # caller can override anything else via config — passed through.
                **{
                    k: v
                    for k, v in config.items()
                    if k not in {"trigger_word", "steps", "learning_rate", "rank"}
                },
            }
            r = client.post(
                f"{self._settings.replicate_base_url}/predictions",
                json={"version": self._settings.replicate_lora_trainer.split(":")[-1], "input": inputs},
            )
            r.raise_for_status()
            data = r.json()
        return TrainingSubmit(
            provider="replicate",
            provider_job_id=data["id"],
            raw={"prediction": data},
        )

    def poll(self, job: TrainingSubmit) -> TrainingStatus:
        with self._client() as client:
            r = client.get(
                f"{self._settings.replicate_base_url}/predictions/{job.provider_job_id}"
            )
            r.raise_for_status()
            data = r.json()
        status = data.get("status", "starting")
        # Replicate progress lives in the logs as a number; many trainers
        # don't surface a percentage. Fall back to coarse stages.
        if status in {"starting", "queued"}:
            return TrainingStatus(state="queued", progress=0.0)
        if status == "processing":
            return TrainingStatus(state="training", progress=0.5)
        if status == "succeeded":
            # The LoRA adapter URL is in `output`; the model id we use at
            # inference time is the prediction id (Replicate keys
            # generations off the version hash + the prediction).
            output = data.get("output") or {}
            adapter_url = output.get("weights") if isinstance(output, dict) else None
            return TrainingStatus(
                state="succeeded",
                progress=1.0,
                provider_model_id=adapter_url or job.provider_job_id,
                cost_usd=float(data.get("metrics", {}).get("predict_time", 0)) * 0.0023,
            )
        if status == "canceled":
            return TrainingStatus(state="failed", progress=0.0, error="cancelled by user")
        return TrainingStatus(
            state="failed", progress=0.0, error=data.get("error") or status
        )

    def cancel(self, job: TrainingSubmit) -> bool:
        with self._client() as client:
            r = client.post(
                f"{self._settings.replicate_base_url}/predictions/{job.provider_job_id}/cancel"
            )
        return r.status_code in (200, 202)
