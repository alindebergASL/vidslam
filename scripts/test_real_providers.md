# Real provider verification runbook

Mock mode (`MOCK_PROVIDERS=true`) is hermetic and covered by the
test suite. The moment you flip to real providers, you're exercising
code paths that have never made an actual outbound request in this
checkout — JSON-mode quirks per chat model, multi-image reference
encoding, ElevenLabs voice-id resolution, S3 presigning. This runbook
is the manual smoke for those.

Run in order on a **staging** deployment, not production. Total time
~10 minutes; total spend ~$0.50 in API costs at default rates.

---

## 1. Pre-flight

```bash
# Stack should be running in prod mode against staging hostname/DNS.
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# Sanity probes — no auth, no providers exercised yet.
curl -fsS https://staging.avs.example.com/api/healthz
curl -fsS https://staging.avs.example.com/api/readyz

# Sign in, save the cookie jar for the rest of the script.
curl -fsS -c /tmp/avs.cookies -X POST \
  https://staging.avs.example.com/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"password\":\"$STAGING_MVP_PASSWORD\"}"
```

Then in the Settings page UI, click **Test provider keys**. Both rows
should turn green with HTTP 200 and a sub-second latency. Any other
result here means a config problem (wrong key, wrong base URL, network
egress blocked) — fix before continuing.

## 2. OpenRouter chat — storyboard planning

```bash
# Load the demo cast + draft project.
curl -fsS -b /tmp/avs.cookies -X POST \
  https://staging.avs.example.com/api/system/seed

# Plan against the *real* chat model.
curl -fsS -b /tmp/avs.cookies -X POST \
  https://staging.avs.example.com/api/projects/1/generate-plan

# Wait for status to flip to 'planned'.
watch -n 2 'curl -sS -b /tmp/avs.cookies https://staging.avs.example.com/api/projects/1/status | jq .project_status'
```

**Expected:** status reaches `planned` within ~30s. The planned JSON
includes 2–5 shots, each with a non-empty `visual_prompt`, the
disclosure text, and caption chunks. If you get `failed`:

```bash
# Inspect the ProviderLog for the response_summary — that's the real
# JSON error the chat model returned.
docker compose exec backend python -c "
from app.db import SessionLocal
from app import models
with SessionLocal() as s:
    for row in s.query(models.ProviderLog).order_by(models.ProviderLog.id.desc()).limit(3):
        print(row.endpoint, row.status, row.response_summary)
"
```

**Common failure modes:**

- `Expecting JSON, got: ...` — the model didn't honor `response_format:
  json_object`. Switch `OPENROUTER_CHAT_MODEL` to one that supports JSON
  mode (e.g. `openai/gpt-4o-mini`, `anthropic/claude-haiku`).
- `401 unauthorized` — key is wrong / expired. The settings page should
  have caught this; if it didn't, the key may be valid for `/models` but
  not for `/chat/completions`. Re-issue.
- Storyboard parses but `content_warning_notes` is set — the script
  tripped the model's safety filter. Edit the script.

## 3. OpenRouter video — per-shot clip generation

```bash
# Trigger the render that drives video model calls.
curl -fsS -b /tmp/avs.cookies -X POST \
  https://staging.avs.example.com/api/projects/1/generate-video

# Watch shots progress through submitted/polling/completed.
watch -n 5 'curl -sS -b /tmp/avs.cookies https://staging.avs.example.com/api/projects/1/status | jq .shots[].status'
```

**Expected:** every non-`end_card` shot reaches `completed`. Real video
models take 1–5 minutes per shot, so a 4-shot project is up to 20 min.
If a shot fails:

- `403 forbidden` on submit → key doesn't have video tier access.
- `polling timed out` → the model finished but our 10-min poll loop
  gave up. Bump `pipeline._ensure_clip_for_shot` deadline if needed.
- Download returns non-MP4 → some models return a webm. Check the
  `final_video_path` extension and confirm ffmpeg can read it.

**Critical thing to verify visually:** open the rendered video and
check that the avatar in each shot looks like the avatar's hero
reference. If shots look like generic people, the reference-image
encoding isn't reaching the model correctly. Inspect:

```bash
# What did we send as references?
docker compose exec backend python -c "
from app.db import SessionLocal
from app import models
with SessionLocal() as s:
    for sh in s.query(models.VideoShot).order_by(models.VideoShot.id.desc()).limit(4):
        print(sh.shot_order, sh.reference_strategy, sh.reference_asset_ids_json)
"
```

Each shot should list at least 1 reference asset id. If the list is
empty for a shot type that needs character consistency
(`hero` / `talking_head`), the auto-reference logic isn't picking them
up — fix in `pipeline._resolve_references_for_shot`.

## 4. ElevenLabs TTS — voiceover

The plan step already populated `cleaned_voice_script`. The render step
above hits the TTS provider.

```bash
# Confirm the audio file ended up in renders/.
docker compose exec backend ls -lh /data/renders/1/
# Expect: voice.m4a or voice.mp3, non-empty
```

Then play it. The voice should match the configured
`ELEVENLABS_DEFAULT_VOICE_ID`. If it's the wrong voice, the per-avatar
`elevenlabs_voice_id` override isn't being used; check the avatar
detail UI to confirm it's set.

**Common failure modes:**

- `401 unauthorized` — same as OpenRouter: settings.test should catch.
- `404 voice_not_found` — voice id is stale or belongs to a different
  account. Re-pick via Cast → avatar detail → Voice picker.
- Audio is generated but the final MP4 has no audio track — ffmpeg
  couldn't mux because the audio file has zero duration. Usually the
  TTS API succeeded but the bytes returned are silence. Inspect with
  `ffprobe -i voice.m4a` — `duration` should match the spoken script
  estimate.

## 5. ElevenLabs music (optional)

Only relevant if `music_source` is set on the project:

```bash
# Configure the project for generated music.
curl -fsS -b /tmp/avs.cookies -X PATCH \
  https://staging.avs.example.com/api/projects/1 \
  -H 'Content-Type: application/json' \
  -d '{"music_source":"generated","music_volume":0.25}'

# Re-render.
curl -fsS -b /tmp/avs.cookies -X POST \
  https://staging.avs.example.com/api/projects/1/generate-video
```

**Expected:** the final MP4's audio has two layers — voiceover at full
volume and music at 25%, the latter ducking under the former. If you
hear only voice, the amix wasn't wired (check `services/renderer.py`
amix filter). If music is at full volume, the volume scaler isn't
applied (check `music_volume` in the renderer args).

## 6. S3-backed asset delivery (optional)

Only relevant if you set `s3_bucket` in `.env`:

```bash
# Confirm the bucket has the seeded assets after seed+render.
aws s3 ls s3://your-bucket/assets/uploads/ --recursive --human-readable | head
# Expect: 1–N png/jpeg objects under uploads/avatar/<id>/

# Confirm the public-asset URL redirects to a presigned S3 URL.
curl -sI "https://staging.avs.example.com/api/public-assets/$ANY_PUBLIC_TOKEN" | head -3
# Expect: HTTP/2 302, Location: https://your-bucket.s3.<region>.amazonaws.com/...?X-Amz-...
```

If the response is `200 OK` with image bytes instead of a 302 redirect,
boto3 isn't installed in the container or the bucket env var isn't
visible to uvicorn. Check `docker compose exec backend python -c
"import boto3; print(boto3.__version__)"` and the actual env in the
backend container.

If the 302 lands at a 403/AccessDenied page, the EC2 instance role
doesn't have `s3:PutObject` + `s3:GetObject` on the bucket — fix the
IAM policy.

## 7. Tear down

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
# Drop the seeded project so the next run is fresh.
docker compose exec backend python -c "
from app.db import SessionLocal
from app import models
with SessionLocal() as s:
    s.query(models.Render).delete()
    s.query(models.VideoShot).delete()
    s.query(models.ProjectCastMember).delete()
    s.query(models.VideoProject).delete()
    s.commit()
"
```

---

## Cost summary at default rates

| Step | Calls | Estimated cost |
|------|------:|---------------:|
| 2. Plan (OpenRouter chat) | 1 | $0.01 |
| 3. Video (OpenRouter video) | 4 shots × 7s | $2.80 |
| 4. TTS (ElevenLabs) | 1 × 200 chars | $0.04 |
| 5. Music (ElevenLabs) | 1 × 25s | $0.05 |
| **Total**                 |       | **~$2.90** |

Run on a budget-capped key if you can.
