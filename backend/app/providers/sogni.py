"""Sogni (sogni.ai) image + video models over the REST workflow API.

Model ids are ``sogni/<selector>`` with Sogni's tool-level selectors
(``sogni/krea-2-turbo``, ``sogni/ltx25``, ``sogni/seedance2-5`` …), not the
worker ids the public model catalog lists. The prefix is stripped before
anything is sent.

Every generation is a one-step durable workflow (docs.sogni.ai,
api-reference/workflows):

  POST /v1/creative-agent/workflows   {"input": {"title", "steps": [{"id",
                                       "toolName", "arguments"}]},
                                       "media_references": [...], ...}
                                      -> 201 data.workflow.workflowId
  GET  /v1/creative-agent/workflows/{id}         -> snapshot (data.workflow)
  GET  /v1/creative-agent/workflows/{id}/events  -> persisted event log

Top-level body keys are snake_case and unknown keys are a 400. Sogni has no
webhooks for REST workflows, so video jobs are polled by video_jobs' sweeper.
Result URLs are signed for ~48 h: they're downloaded straight away, as is,
without our Authorization header.

Billing is in Spark (1 Spark = $0.005); the workflow snapshot carries no
per-job cost, so spend is recorded from the price table below, as an estimate.
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..config import get_settings
from ..services import provider_keys
from .base import (
    ImageProvider,
    ImageRef,
    JobStatus,
    ModelInfo,
    SubmittedJob,
    VideoCapabilities,
    VideoProvider,
)

log = logging.getLogger("avs.sogni")

PREFIX = "sogni/"
STEP_ID = "s1"
USER_AGENT = "vidslam/1.0"
APP_SOURCE = "vidslam"
WORKFLOWS = "/v1/creative-agent/workflows"

# Patched in tests so retries and polling don't sleep.
_sleep = time.sleep


def is_sogni(model: Optional[str]) -> bool:
    return bool(model) and str(model).startswith(PREFIX)


def selector(model: str) -> str:
    """`sogni/krea-2-turbo` -> `krea-2-turbo` (bare selectors pass through)."""
    return model[len(PREFIX):] if is_sogni(model) else model


# ---------- models + prices (one place) ----------

# 1 Spark = $0.005 (docs.sogni.ai pricing).
SPARK_USD = 0.005


@dataclass(frozen=True)
class ImageModel:
    selector: str
    name: str
    note: str
    spark: float  # per image, estimate unless noted
    premium: bool = False  # vendor model: always bills Premium Spark


@dataclass(frozen=True)
class VideoModel:
    selector: str
    name: str
    note: str
    min_s: int
    max_s: int
    spark_per_second: float  # estimate at 720p unless noted
    animate: bool = False  # available in animate_photo (exact first frame)
    last_frame: bool = False  # documented end-frame pinning we rely on
    references: bool = False  # takes loose reference images (referenceImageIndices)
    negative: bool = False  # accepts negativePrompt (LTX / WAN only)
    premium: bool = False


# Documented example: Krea 2 Turbo 1024px ≈ 2.43 Spark. The rest are
# conservative estimates (vendor models bill Premium Spark and cost more).
IMAGE_MODELS: tuple[ImageModel, ...] = (
    ImageModel("krea-2-turbo", "Krea 2 Turbo", "Fast and cheap; the only model on free-API accounts. ~2.4 Spark.", 2.43),
    ImageModel("flux-3-klein", "FLUX.3 Klein", "Small FLUX.3. Estimate ~3 Spark.", 3.0),
    ImageModel("z-turbo", "Z-Image Turbo", "Fast photoreal. Estimate ~3 Spark.", 3.0),
    ImageModel("z-image", "Z-Image", "Estimate ~5 Spark.", 5.0),
    ImageModel("qwen-2512", "Qwen Image 2512", "Good with text in images. Estimate ~6 Spark.", 6.0),
    ImageModel("qwen-2512-lightning", "Qwen Image 2512 Lightning", "Faster Qwen. Estimate ~3 Spark.", 3.0),
    ImageModel("chroma-v46-flash", "Chroma v46 Flash", "Estimate ~3 Spark.", 3.0),
    ImageModel("chroma1-hd", "Chroma1 HD", "Estimate ~5 Spark.", 5.0),
    ImageModel("gpt-image-2", "GPT Image 2 (via Sogni)", "Premium Spark. Estimate ~12 Spark.", 12.0, premium=True),
    ImageModel("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst (via Sogni)",
               "Premium Spark. Estimate ~30 Spark.", 30.0, premium=True),
    ImageModel("gpt-image-2.5-flare", "GPT Image 2.5 Flare (via Sogni)",
               "Premium Spark. Estimate ~20 Spark.", 20.0, premium=True),
    ImageModel("seedream-5-0-pro-uncensored", "Seedream 5.0 Pro (uncensored)",
               "Premium Spark; needs the one-time likeness agreement in the Sogni app. Estimate ~10 Spark.",
               10.0, premium=True),
)

# Durations from the docs: LTX 2-20; Seedance 2.0/Mini 4-15; Seedance 2.5
# 4-30; HappyHorse 3-15; Wan 3 2-30; H3 clamps to ~5.2-15.1 s. Wan 2.2 has no
# documented range: 2-10 is a conservative guess. Prices: documented
# examples (LTX-2.3 5 s ≈ 17.5; LTX-2.5 i2v 14 s ≈ 102.7; Seedance 2.5 720p ≈
# 60.1/s; Seedance 2.0 720p 5 s ≈ 196.6; Seedance 2.0 Mini 720p 5 s ≈ 98.3;
# HappyHorse 720p ≈ 182 per clip; H3 FastH3 Turbo 8 s ≈ 32), else estimates.
VIDEO_MODELS: tuple[VideoModel, ...] = (
    VideoModel("ltx25", "LTX-2.5", "Sogni's default video model. First frame. 2–20 s.", 2, 20, 7.4,
               animate=True, negative=True),
    VideoModel("ltx23", "LTX-2.3", "Previous LTX. First frame. 2–20 s. ~17.5 Spark per 5 s.", 2, 20, 3.5,
               animate=True, negative=True),
    VideoModel("wan22", "Wan 2.2", "First frame. 2–10 s (assumed).", 2, 10, 4.0, animate=True, negative=True),
    VideoModel("minimax-h3-t2v-turbo", "MiniMax H3 Turbo", "Text to video. ~5–15 s.", 5, 15, 4.0),
    VideoModel("seedance2-5", "Seedance 2.5", "Premium Spark. 4–30 s. Picture is a loose reference, not an exact "
               "first frame. ~60 Spark/s at 720p.", 4, 30, 60.1, references=True, premium=True),
    VideoModel("seedance2", "Seedance 2.0", "Premium Spark. 4–15 s. Loose picture reference. ~39 Spark/s.",
               4, 15, 39.3, references=True, premium=True),
    VideoModel("seedance2-mini", "Seedance 2.0 Mini", "Premium Spark. 4–15 s. Loose picture reference. ~20 Spark/s.",
               4, 15, 19.7, references=True, premium=True),
    VideoModel("happyhorse-1.1-i2v", "HappyHorse 1.1", "Premium Spark. First frame. 3–15 s. ~182 Spark per clip.",
               3, 15, 36.4, animate=True, premium=True),
    VideoModel("wan3.0-video", "Wan 3.0 (via Sogni)", "Premium Spark. First frame. 2–30 s. Estimate ~40 Spark/s.",
               2, 30, 40.0, animate=True, negative=True, premium=True),
)

_IMAGE_BY_SEL = {m.selector: m for m in IMAGE_MODELS}
_VIDEO_BY_SEL = {m.selector: m for m in VIDEO_MODELS}


def image_model(model: str) -> Optional[ImageModel]:
    return _IMAGE_BY_SEL.get(selector(model))


def video_model(model: str) -> Optional[VideoModel]:
    return _VIDEO_BY_SEL.get(selector(model))


def image_cost_usd(model: str) -> float:
    """Estimated USD for one image; COST_IMAGE_PER_ITEM for unlisted models."""
    m = image_model(model)
    return round(m.spark * SPARK_USD, 6) if m else get_settings().cost_image_per_item


def video_rate_usd(model: str) -> float:
    """Estimated USD per second of video; COST_VIDEO_PER_SECOND if unlisted."""
    m = video_model(model)
    return round(m.spark_per_second * SPARK_USD, 6) if m else get_settings().cost_video_per_second


def video_cost_usd(model: str, seconds: float) -> float:
    return round(video_rate_usd(model) * max(float(seconds or 0.0), 0.0), 6)


def capabilities_for(model: str) -> VideoCapabilities:
    m = video_model(model)
    if m is None:
        return VideoCapabilities(durations=[5], first_frame=False, last_frame=False, known=False)
    return VideoCapabilities(durations=list(range(m.min_s, m.max_s + 1)),
                             first_frame=m.animate, last_frame=m.animate and m.last_frame)


# ---------- HTTP client ----------


class SogniError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: Any = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def _retry_after(r: httpx.Response, body: dict) -> Optional[float]:
    for raw in (r.headers.get("Retry-After"), body.get("retryAfter"),
                (body.get("details") or {}).get("retryAfterSeconds")
                if isinstance(body.get("details"), dict) else None):
        try:
            if raw is not None:
                return max(float(raw), 0.0)
        except (TypeError, ValueError):
            continue
    return None


class SogniClient:
    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or provider_keys.resolve("sogni")
        if not self.api_key:
            raise RuntimeError("SOGNI_API_KEY missing for the Sogni provider (Settings → API keys)")

    @property
    def base(self) -> str:
        return get_settings().sogni_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
            "X-App-Source": APP_SOURCE,
            "Accept": "application/json",
        }

    @staticmethod
    def _json_ok(r: httpx.Response) -> dict:
        """Parse a 2xx body; a proxy/HTML page with a 2xx status becomes a
        SogniError rather than a bare ValueError (and no raw HTML in text)."""
        try:
            body = r.json()
        except ValueError:
            raise SogniError(f"Sogni {r.status_code}: unexpected non-JSON response",
                             status=r.status_code) from None
        if not isinstance(body, dict):
            raise SogniError(f"Sogni {r.status_code}: unexpected response shape", status=r.status_code)
        return body

    @staticmethod
    def _workflow_of(body: dict) -> dict:
        data = body.get("data")
        if not isinstance(data, dict):
            data = {}
        wf = data.get("workflow")
        return wf if isinstance(wf, dict) else data

    def _scrub(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    def error_for(self, r: httpx.Response) -> SogniError:
        """A readable error from Sogni's envelope {status:"error", errorCode,
        message, details?, retryAfter?}. Never contains the key."""
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = body.get("errorCode")
        msg = str(body.get("message") or (r.text or "").strip()[:200] or r.reason_phrase or "error")
        if r.status_code == 402:
            vendor = (body.get("details") or {}).get("vendorModels") if isinstance(body.get("details"), dict) else None
            msg = ("not enough Sogni credit (Spark)"
                   + (f" for premium models {', '.join(map(str, vendor))}" if vendor else "")
                   + f": {msg}")
        label = f"Sogni {r.status_code}" + (f" {code}" if code is not None else "")
        return SogniError(self._scrub(f"{label}: {msg}")[:600], status=r.status_code, code=code,
                          retry_after=_retry_after(r, body))

    def start(self, *, title: str, tool: str, arguments: dict,
              media_references: Optional[list[dict]] = None) -> dict:
        """Start a one-step workflow; returns the workflow snapshot. One fresh
        Idempotency-Key per call, reused only when retrying this same start
        (network errors, 429 within the wait cap, 5xx)."""
        body: dict = {
            "input": {"title": title[:120] or "vidslam", "steps": [
                {"id": STEP_ID, "toolName": tool, "arguments": arguments},
            ]},
            "token_type": "auto",
            "app_source": APP_SOURCE,
            "safe_content_filter": bool(get_settings().sogni_safe_content_filter),
        }
        if media_references:
            body["media_references"] = media_references
        headers = {**self._headers(), "Content-Type": "application/json",
                   "Idempotency-Key": str(uuid.uuid4())}
        cap = get_settings().sogni_max_retry_after_seconds
        attempts = 3
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                with httpx.Client(timeout=60.0) as client:
                    r = client.post(f"{self.base}{WORKFLOWS}", headers=headers, json=body)
            except httpx.TransportError as e:
                if last:
                    raise SogniError(f"Sogni unreachable: {type(e).__name__}") from None
                _sleep(1.0 + attempt + random.random())
                continue
            if r.status_code < 400:
                wf = self._workflow_of(self._json_ok(r))
                if not wf.get("workflowId"):
                    raise SogniError("Sogni start returned no workflowId")
                return wf
            err = self.error_for(r)
            if r.status_code == 429 and not last:
                wait = err.retry_after if err.retry_after is not None else 2.0 + random.random() * 3
                if wait > cap:
                    raise SogniError(f"{err} (retry after {int(wait)} s)", status=429,
                                     code=err.code, retry_after=wait)
                _sleep(wait)
                continue
            if r.status_code >= 500 and not last:
                _sleep(2.0 * (attempt + 1))
                continue
            raise err
        raise SogniError("Sogni start failed")  # pragma: no cover — loop always returns/raises

    def get(self, workflow_id: str) -> dict:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{self.base}{WORKFLOWS}/{workflow_id}", headers=self._headers())
        if r.status_code >= 400:
            raise self.error_for(r)
        return self._workflow_of(self._json_ok(r))

    def events(self, workflow_id: str) -> list[dict]:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{self.base}{WORKFLOWS}/{workflow_id}/events", headers=self._headers())
        if r.status_code >= 400:
            raise self.error_for(r)
        body = self._json_ok(r)
        data = body.get("data", body)
        if isinstance(data, dict):
            data = data.get("events") or []
        return [e for e in data or [] if isinstance(e, dict)]

    def artifact_url(self, wf: dict, kind: str) -> Optional[str]:
        """Where the result is: steps[i].artifacts, then workflow.artifacts for
        our step, then artifact_created events (the docs and the protocol
        schema disagree on the first two)."""
        url = find_artifact(wf, kind)
        if url:
            return url
        wid = wf.get("workflowId")
        if not wid:
            return None
        try:
            events = self.events(str(wid))
        except Exception as e:  # noqa: BLE001
            log.info("sogni events for %s unavailable: %s", wid, e)
            return None
        for ev in events:
            if ev.get("type") != "artifact_created":
                continue
            d = ev.get("data") or {}
            art = d.get("artifact") if isinstance(d.get("artifact"), dict) else d
            if _artifact_ok(art, kind, step_ok=True):
                return str(art["url"])
        return None

    @staticmethod
    def download(url: str) -> bytes:
        """Fetch a signed result URL as given: no auth header, no rewriting."""
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.content


def _artifact_ok(a: Any, kind: str, *, step_ok: bool = False) -> bool:
    if not isinstance(a, dict) or not a.get("url"):
        return False
    if a.get("kind") and a.get("kind") != kind:
        return False
    sid = a.get("stepId")
    return step_ok or sid in (None, "", STEP_ID)


def find_artifact(wf: dict, kind: str) -> Optional[str]:
    steps = [s for s in wf.get("steps") or [] if isinstance(s, dict)]
    ours = [s for s in steps if s.get("id") == STEP_ID] or steps
    for s in ours:
        for a in s.get("artifacts") or []:
            if _artifact_ok(a, kind, step_ok=True):
                return str(a["url"])
    for a in wf.get("artifacts") or []:
        if _artifact_ok(a, kind):
            return str(a["url"])
    return None


def workflow_state(wf: dict) -> tuple[str, Optional[str]]:
    """(queued|running|done|failed, error) for a snapshot. "done" still needs
    an artifact to count as succeeded (partial_failure may have one)."""
    status = str(wf.get("status") or "").lower()
    if status in ("completed", "partial_failure"):
        return "done", None
    if status in ("failed", "cancelled"):
        return "failed", _failure_message(wf, status)
    if status == "waiting_for_user":
        # Never auto-approve a cost or release a safety pause: a one-step job
        # waiting on a person has failed as far as the pipeline is concerned.
        if wf.get("awaitingCostApproval"):
            return "failed", "Sogni is waiting for cost approval (not approved automatically)"
        return "failed", f"Sogni is waiting for: {wf.get('waitingReason') or 'user input'}"
    if status == "running":
        return "running", None
    return "queued", None


def _failure_message(wf: dict, status: str) -> str:
    err = wf.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code")
    if not err:
        for s in wf.get("steps") or []:
            e = (s or {}).get("error") if isinstance(s, dict) else None
            if e:
                err = e.get("message") if isinstance(e, dict) else e
                break
    return f"Sogni workflow {status}" + (f": {str(err)[:300]}" if err else "")


def _duration_of(wf: dict) -> Optional[float]:
    for a in wf.get("artifacts") or []:
        if isinstance(a, dict) and a.get("durationSeconds"):
            return float(a["durationSeconds"])
    steps = ((wf.get("input") or {}).get("steps") or wf.get("steps") or [])
    for s in steps:
        if isinstance(s, dict):
            d = (s.get("arguments") or {}).get("duration")
            if d:
                return float(d)
    return None


# ---------- image ----------


class SogniImageProvider(ImageProvider):
    """generate_image on Sogni: start, poll, download. Synchronous."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.client = SogniClient(api_key)
        self.model = model or f"{PREFIX}{IMAGE_MODELS[0].selector}"

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=f"{PREFIX}{m.selector}", name=f"{m.name} (Sogni)", modality="image",
                          description=m.note) for m in IMAGE_MODELS]

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        settings: dict | None = None,
    ) -> bytes:
        model = model or self.model
        settings = settings or {}
        args: dict = {
            "prompt": prompt,
            "model": selector(model),
            "numberOfVariations": 1,
            "outputFormat": "png",
        }
        if negative_prompt:
            args["negativePrompt"] = negative_prompt
        if settings.get("aspect_ratio"):
            args["aspectRatio"] = str(settings["aspect_ratio"])
        media: list[dict] = []
        # A frame or a "scene" reference (same place, other time of day — the
        # ambient drift/anchor variants) is the img2img source; character /
        # style / object references have no input on generate_image.
        source = next((r for r in references or [] if r.frame_type), None) or next(
            (r for r in references or [] if r.role == "scene"), None)
        if source is None and settings.get("img2img") and references:
            source = references[0]
        if source is not None:
            media.append({"kind": "image", "url": source.url})
            args["sourceImageIndex"] = -1
            args["starting_image_strength"] = float(
                settings.get("starting_image_strength") or get_settings().sogni_image_strength)
        elif references:
            # generate_image has no identity-reference input.
            log.info("sogni %s: ignoring %d reference image(s) (no reference input)", model, len(references))
        wf = self.client.start(title=prompt[:80], tool="generate_image", arguments=args,
                               media_references=media or None)
        wf = self._wait(wf)
        url = self.client.artifact_url(wf, "image")
        if not url:
            raise SogniError(f"Sogni workflow {wf.get('status')} without an image")
        data = self.client.download(url)
        from ..services import spend

        spend.record_rate_card("sogni", "image", model, image_cost_usd(model),
                               note="Sogni Spark price-table estimate")
        return data

    def _wait(self, wf: dict) -> dict:
        wid = str(wf["workflowId"])
        deadline = time.monotonic() + get_settings().sogni_image_timeout_seconds
        delay = 2.0
        while True:
            state, err = workflow_state(wf)
            if state == "done":
                return wf
            if state == "failed":
                raise SogniError(err or "Sogni workflow failed")
            if time.monotonic() > deadline:
                raise SogniError(f"Sogni image timed out after {int(get_settings().sogni_image_timeout_seconds)} s "
                                 f"(workflow {wid} still {wf.get('status')})")
            _sleep(delay)
            delay = min(delay * 1.5, 5.0)
            try:
                wf = self.client.get(wid)
            except SogniError as e:
                if e.status == 429 or e.status >= 500:
                    if e.retry_after:
                        _sleep(min(e.retry_after, 30.0))
                    continue
                raise
            except httpx.TransportError:
                continue


# ---------- video ----------


class SogniVideoProvider(VideoProvider):
    """generate_video / animate_photo on Sogni. Async: submit, then the
    video_jobs sweeper polls (no webhooks on Sogni)."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.client = SogniClient(api_key)
        self.model = model or f"{PREFIX}{VIDEO_MODELS[0].selector}"

    def list_models(self) -> list[ModelInfo]:
        out = []
        for m in VIDEO_MODELS:
            tag = f"{m.min_s}–{m.max_s}s" + (" · first frame" if m.animate else "")
            out.append(ModelInfo(id=f"{PREFIX}{m.selector}", name=f"{m.name} (Sogni, {tag})",
                                 modality="video", description=m.note))
        return out

    def capabilities(self, model: str) -> VideoCapabilities:
        return capabilities_for(model or self.model)

    def submit(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        reference_strategy: str = "input_references",
        duration_seconds: float = 5.0,
        settings: dict | None = None,
    ) -> SubmittedJob:
        model = model or self.model
        sel = selector(model)
        spec = video_model(model)
        caps = self.capabilities(model)
        settings = settings or {}  # callback_url is ignored: Sogni has no webhooks
        duration = caps.snap(duration_seconds)
        args: dict = {"prompt": prompt, "videoModel": sel, "duration": duration,
                      "generateAudio": bool(settings.get("generate_audio", False))}
        if settings.get("aspect_ratio"):
            args["aspectRatio"] = str(settings["aspect_ratio"])
        if negative_prompt and (spec.negative if spec else sel.startswith(("ltx", "wan"))):
            args["negativePrompt"] = negative_prompt
        media: list[dict] = []
        tool = "generate_video"
        refs = list(references or [])
        first = last = None
        if reference_strategy == "frame_images" and refs:
            for i, r in enumerate(refs):
                ft = r.frame_type or ("first_frame" if i == 0 else "last_frame")
                if ft == "first_frame" and first is None:
                    first = r
                elif ft == "last_frame" and last is None:
                    last = r
        if first is not None and caps.first_frame:
            tool = "animate_photo"
            media.append({"kind": "image", "url": first.url})
            args["sourceImageIndex"] = -1  # -1 = the first media_references image
            args["frameRole"] = "start"
            if last is not None and caps.last_frame:
                if last.url == first.url:
                    args["frameRole"] = "both"
                else:
                    media.append({"kind": "image", "url": last.url})
                    args["endImageIndex"] = -2
        else:
            loose = [first] if first is not None else refs
            if loose and spec is not None and spec.references:
                media += [{"kind": "image", "url": r.url} for r in loose[:9]]
                args["referenceImageIndices"] = [-(i + 1) for i in range(len(media))]
                if first is not None:
                    args["prompt"] = f"Use @Image1 as the first frame. {prompt}"
            elif loose:
                log.info("sogni %s: ignoring %d reference image(s) (model takes none)", model, len(loose))
        wf = self.client.start(title=prompt[:80], tool=tool, arguments=args, media_references=media or None)
        wid = str(wf["workflowId"])
        return SubmittedJob(provider="sogni", model=model, job_id=wid,
                            polling_url=f"{self.client.base}{WORKFLOWS}/{wid}",
                            raw={"duration": duration, "tool": tool})

    def poll(self, job: SubmittedJob) -> JobStatus:
        wf = self.client.get(job.job_id)
        state, err = workflow_state(wf)
        if state == "done":
            url = self.client.artifact_url(wf, "video")
            if not url:
                return JobStatus(state="failed", error=f"Sogni workflow {wf.get('status')} without a video")
            secs = _duration_of(wf) or float(job.raw.get("duration") or 5.0)
            # An estimate from the Spark price table; spend records it as one.
            return JobStatus(state="succeeded", progress=1.0, result_url=url,
                             cost=video_cost_usd(job.model or self.model, secs))
        if state == "failed":
            return JobStatus(state="failed", error=err)
        if state == "running":
            return JobStatus(state="running")
        return JobStatus(state="queued")

    def download(self, job: SubmittedJob, result_url: str | None = None) -> bytes:
        if not result_url:
            st = self.poll(job)
            if st.state != "succeeded" or not st.result_url:
                raise RuntimeError(f"Sogni job not ready: {st.state} {st.error or ''}".strip())
            result_url = st.result_url
        return SogniClient.download(result_url)
