"""ElevenLabs Voice Lab voice-cloning adapter.

Trains a single-shot voice clone from a set of audio assets uploaded
against an Avatar. ElevenLabs returns a `voice_id` immediately
(synchronous clone — no real "training" job in their API for instant
cloning), so submit() returns succeeded right away.

Verified manually per `scripts/test_real_providers.md`."""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import httpx

from ..config import get_settings
from .base import TrainingAsset, TrainingStatus, TrainingSubmit

log = logging.getLogger("avs.training.elevenlabs")


class ElevenLabsVoiceLabProvider:
    def __init__(self) -> None:
        self._settings = get_settings()
        if not self._settings.elevenlabs_api_key:
            raise RuntimeError(
                "ElevenLabsVoiceLabProvider needs ELEVENLABS_API_KEY — registry should "
                "fall back to mock when the key isn't set."
            )

    def list_kinds(self) -> list[str]:
        return ["voice_clone"]

    def _voice_id_for_job(self, job_id: str) -> str:
        # We embed the voice id in the job id since the clone is synchronous.
        return job_id

    def submit(
        self,
        *,
        kind: str,
        name: str,
        assets: list[TrainingAsset],
        config: dict,
    ) -> TrainingSubmit:
        if kind != "voice_clone":
            raise ValueError(f"ElevenLabsVoiceLabProvider doesn't handle kind={kind}")
        if not assets:
            raise ValueError("Voice cloning needs at least one audio sample")

        files = []
        for i, a in enumerate(assets):
            p = Path(a.local_path)
            mime = a.mime_type or mimetypes.guess_type(p.name)[0] or "audio/mpeg"
            files.append(("files", (p.name, p.read_bytes(), mime)))

        data = {
            "name": name or f"avs-clone-{assets[0].asset_id}",
            "description": config.get("description", "Cloned via AvatarVideoStudio"),
        }
        if "labels" in config:
            import json

            data["labels"] = json.dumps(config["labels"])

        headers = {"xi-api-key": self._settings.elevenlabs_api_key}
        url = f"{self._settings.elevenlabs_base_url.rstrip('/')}/voices/add"
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, data=data, files=files, headers=headers)
            r.raise_for_status()
            body = r.json()
        voice_id = body.get("voice_id") or body.get("id")
        if not voice_id:
            raise RuntimeError(f"ElevenLabs /voices/add returned no voice_id: {body}")

        return TrainingSubmit(
            provider="elevenlabs",
            provider_job_id=voice_id,  # synchronous: id == voice_id
            raw=body,
        )

    def poll(self, job: TrainingSubmit) -> TrainingStatus:
        # Instant clone — already done at submit. Confirm the voice exists
        # (a 404 here means the voice was deleted out-of-band).
        voice_id = self._voice_id_for_job(job.provider_job_id)
        headers = {"xi-api-key": self._settings.elevenlabs_api_key}
        url = f"{self._settings.elevenlabs_base_url.rstrip('/')}/voices/{voice_id}"
        try:
            with httpx.Client(timeout=15.0) as client:
                r = client.get(url, headers=headers)
            if r.status_code == 404:
                return TrainingStatus(
                    state="failed",
                    error=f"voice {voice_id} no longer exists",
                )
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return TrainingStatus(state="failed", error=str(e)[:200])
        return TrainingStatus(
            state="succeeded",
            progress=1.0,
            provider_model_id=voice_id,
            cost_usd=0.0,
        )

    def cancel(self, job: TrainingSubmit) -> bool:
        # Instant clone — there's nothing to cancel. Returning False signals
        # the pipeline to mark the local row as already-completed rather
        # than attempting a real cancel.
        return False
