# AvatarVideoStudio

A hosted web app for creating short-form social videos (TikTok / Reels / Shorts) from
**reusable digital avatars** and a Flow-style **Cast** of visual ingredients.

You build a Cast (Characters + Scenes + Styles + Objects/Props), generate new images
and short clips from those references in the **Studio**, then assemble them into a
vertical 1080×1920 video with voiceover, captions, AI disclosure overlay, and an
optional CTA end card.

The full pipeline runs end-to-end **with zero API keys** via `MOCK_PROVIDERS=true` —
useful for demos, CI, and local development.

---

## Stack

- **Frontend** — Next.js 14 + TypeScript + Tailwind
- **Backend** — FastAPI + SQLAlchemy + Pydantic
- **Queue** — Redis + RQ (falls back to inline execution if Redis is unavailable)
- **DB** — SQLite (file in the data volume)
- **Renderer** — FFmpeg (H.264 + AAC, ASS subtitles)
- **Providers** — OpenRouter chat / image / video, ElevenLabs TTS — all behind clean
  adapter Protocols; pure mocks for fully offline operation.

---

## Quick start (Docker Compose)

```bash
cp .env.example .env          # MOCK_PROVIDERS=true is the default
make up                       # builds and starts redis + backend + worker + frontend
make seed                     # creates demo Cast (Naina, Arjun, rooftop, 35mm) + sample project
make smoke                    # drives the full mock pipeline end-to-end and asserts a playable MP4
```

- Frontend → http://localhost:3000 (login with the password from `.env`, default `changeme`)
- Backend  → http://localhost:8000/health
- Tests    → `make test` (runs pytest inside the backend container)

### Going live with real providers

Edit `.env`:

```bash
MOCK_PROVIDERS=false
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_CHAT_MODEL=openai/gpt-4o-mini
OPENROUTER_IMAGE_MODEL=...   # optional, leave blank to discover
OPENROUTER_VIDEO_MODEL=...   # optional, leave blank to discover
ELEVENLABS_API_KEY=...       # optional
ELEVENLABS_DEFAULT_VOICE_ID=...
```

Each channel (chat / image / video / tts) is mocked independently when its key is
missing — partial configs work fine. `GET /api/providers/status` reflects what's live.

---

## Architecture

```
                       ┌───────────────┐
                       │ Next.js (UI)  │
                       └──────┬────────┘
                              │  fetch (cookie auth)
                              ▼
                       ┌───────────────┐
                       │ FastAPI       │
                       │ /api/*        │
                       └──┬────────┬───┘
              enqueue     │        │ direct
                          ▼        ▼
        ┌─────────┐  ┌──────────────────┐
        │ RQ + Redis │ │ SQLite (data/)  │
        └─────┬─────┘  └─────────────────┘
              │
              ▼
   ┌───────────────────────┐
   │ Provider Registry     │
   │  chat  → mock | OR    │
   │  image → mock | OR    │
   │  video → mock | OR    │
   │  tts   → mock | EL    │
   └───────┬───────────────┘
           │ bytes
           ▼
       ┌───────┐
       │FFmpeg │
       └───────┘
```

### Quality control

Before kicking off a real-provider video job (which spends money), the editor
runs a **preflight check** that confirms:

| Check | What it validates |
| --- | --- |
| `avatar_hero` | Every character in the cast has at least one `hero` asset |
| `script_length` | Word count is within ~1.2× of what fits the target duration at 150 wpm |
| `cta_or_disabled` | CTA text is set, or the user has explicitly left it blank |
| `providers_configured` | OpenRouter key is present, or `MOCK_PROVIDERS=true` |
| `output_writable` | `data/renders/{project_id}/` is writable with >100 MB free |

Failures block `POST /api/projects/{id}/generate-video` with a `422` whose body
includes the full check list. The UI shows a modal with traffic-light status per
check; the user can fix the issue or pick **Generate anyway** (sends `?force=true`).

### One-shot regeneration

`POST /api/projects/{id}/shots/{shot_id}/regenerate?recompose=true` (the default)
regenerates only that one clip and then re-runs the FFmpeg composition step
using the existing audio + other shots' clips. A new `Render` row is created so
version history is preserved, but no extra TTS / video provider calls are made
for the unchanged shots.

If you want to swap multiple shots before re-stitching, call regenerate with
`recompose=false` once per shot then `POST /api/projects/{id}/recompose` to
produce the final video in a single FFmpeg pass.

### Audio (voiceover + optional music bed)

Every project has three audio surfaces:

| Surface | Source | How |
| --- | --- | --- |
| **Voiceover** | `voiceover_source = "tts"` (default) | TTS provider (ElevenLabs if configured, mock silent otherwise) speaks `StoryboardPlan.cleaned_voice_script` |
| **Voiceover** | `voiceover_source = "upload"` | `POST /api/projects/{id}/voiceover` (mp3/m4a/wav/aac, ≤50 MB) — pipeline reads the file instead of calling TTS |
| **Voiceover** | `voiceover_source = "silent"` | renderer skips the voice track entirely |
| **Background music** | `POST /api/projects/{id}/music` (upload) **or** `POST /api/projects/{id}/music/generate` (prompt → MusicProvider) | optional; mp3/m4a/wav/aac, ≤50 MB. Loops to cover the full video, mixed under the voiceover at `music_volume` (0.0–1.0, default 0.25) via FFmpeg `amix` |

The renderer auto-routes:
- voice only → AAC voice with apad to video length
- voice + music → FFmpeg `[1:a]apad[v];[2:a]volume=X[m];[v][m]amix=...`
- music only → music at `music_volume` (handy when uploading a finished narration as the "music" track)
- neither → silent video

The editor's Audio panel exposes all three: voiceover source chips, audio
preview, music attach/replace/remove **or generate-from-prompt**, and a
music volume slider.

The MusicProvider is a clean Protocol like the other channels:

```python
class MusicProvider(Protocol):
    def generate(self, *, prompt: str, duration_seconds: float, settings: dict | None = None) -> bytes: ...
    def output_extension(self) -> str: ...
    def list_models(self) -> list[ModelInfo]: ...
```

Implementations: `MockMusicProvider` (deterministic FFmpeg lavfi triad pad,
used in `MOCK_PROVIDERS` mode and tests) and `ElevenLabsMusicProvider`
(real `POST /v1/music` adapter). Swap in Suno, Stable Audio, etc. by
adding another implementation to the registry without touching the UI.

### Not just one vertical

The engine is domain-agnostic — the dating-app seed data (`Naina` / `Kissmet`)
is only demo content. The Cast model (avatars + scene/style/object/prop
ingredients), the three modes, the renderer, and the provider adapters know
nothing about any particular use case. Two per-project fields let you target
any vertical without code changes:

- **`creative_direction`** — a free-text brief injected into the storyboard
  planner ("Calm B2B SaaS product demo, clean and trustworthy"; "High-energy
  fitness hook, fast cuts"; "Patient step-by-step tutorial"). The planner
  adapts shot list, pacing, and language to it.
- **`disclosure_text`** — overrides the burned-in overlay label (default
  "AI-generated virtual creator"; set "AI-generated product demo",
  "Virtual presenter", etc., or disable it entirely).

Both are editable in the New Project form and the project editor (re-plan to
apply a changed brief).

### Brand kits

A `BrandKit` saves a logo + colors (primary / end-card background / end-card
text) + default CTA and disclosure once, and a project can reference one via
`brand_kit_id`. At render time:

- the **end card** uses the kit's background/text colors and overlays the
  uploaded logo above the CTA;
- the **captions** are recolored to the kit's `primary_color` (converted to an
  ASS `&HAABBGGRR` value), so the whole video carries the palette — not just
  the end card;
- the **disclosure** falls back to the kit's value (precedence: project
  override → brand kit → planner → default).

CRUD + logo upload live at `/api/brand-kits`; the **Brand** page provides a
live end-card preview with color pickers. Deleting a kit detaches it from any
projects rather than breaking them.

### Cost estimation

`GET /api/projects/{id}/cost-estimate` returns a transparent rate-card
projection of real-provider spend (storyboard chat call + per-second video +
per-1k-char TTS + optional music), configurable via `COST_*` env vars. The
preflight modal shows this before you commit to a run. Each `Render` records
`estimated_cost` at creation and `actual_cost` at completion — `actual_cost`
is `0` in mock mode (nothing billed).

### Bulk shot re-roll

`POST /api/projects/{id}/shots/regenerate-bulk {shot_ids}` regenerates several
shots' clips in one pass, then runs a single FFmpeg recompose — so re-rolling
three bad shots costs three clip generations and one stitch, not three full
renders. The editor exposes this via a checkbox on each shot and a "Re-roll
selected" toolbar action.

### Provider key health probe

`POST /api/providers/health-check` validates credentials without generating
anything: OpenRouter (chat/image/video share one key) is checked via
`GET /models`, ElevenLabs (tts/music) via `GET /voices`. Each group reports
`ok` + latency, or `mock` when no key is needed. The dashboard's
**Test provider keys** button surfaces this so a bad key is caught before a
generation run rather than mid-pipeline.

### Cast model

| Entity      | What it is                                                |
| ----------- | --------------------------------------------------------- |
| `Avatar`    | Reusable character (name, persona, visual identity)       |
| `Ingredient`| Reusable non-character reference (`scene`, `style`, `object`, `prop`) |
| `Asset`     | An image file owned by either an avatar or an ingredient. Has a random `public_token` for provider-fetchable URLs. `source = upload | generated` |
| `ProjectCastMember` | Joins a Project to N avatars + N ingredients with role labels |

Generated outputs from the Studio (`AssetGenerationJob.result_path`) can be promoted
into the owner's asset library, which is the Flow-style "save to characters/scenes" loop.

### Pipeline statuses

```
draft → planning → planned → generating_audio → generating_shots
                              → polling → rendering → completed | failed
```

Each `VideoShot` has its own status; failed shots can be regenerated without redoing
the whole pipeline.

---

## API surface

| Group     | Endpoints (selected) |
| --------- | -------------------- |
| Auth      | `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/status` |
| Avatars   | `GET/POST/PATCH/DELETE /api/avatars[/{id}]` · `POST /api/avatars/{id}/assets` |
| Ingredients | mirror of avatars (`/api/ingredients`) |
| Assets    | `GET /api/public-assets/{token}` · `DELETE /api/assets/{id}` |
| Projects  | `GET/POST/PATCH/DELETE /api/projects[/{id}]` · `PATCH /api/projects/{id}/cast` · `PATCH /api/projects/{id}/shots/{shot_id}` · `POST /api/projects/{id}/shots/reorder` · `PATCH /api/projects/{id}/plan` (edit spoken script / end card / resync captions) |
| Generation | `POST /api/projects/{id}/generate-plan` · `GET /api/projects/{id}/preflight` · `GET /api/projects/{id}/cost-estimate` · `POST /api/projects/{id}/generate-video[?force=true]` · `POST /api/projects/{id}/recompose` · `POST /api/projects/{id}/retry` (smart retry after a failed render — recomposes when all clips+audio survived, otherwise resumes) · `GET /api/projects/{id}/status` · `GET /api/projects/{id}/renders` · `GET /api/projects/{id}/renders/export` (zip of all completed renders) · `GET /api/renders/recent` (latest completed across all projects) · `POST /api/projects/{id}/shots/{shot_id}/regenerate[?recompose=true]` · `POST /api/projects/{id}/shots/regenerate-bulk` · `GET /api/renders/{id}/download` |
| Public share | `GET /api/public-renders/{token}` · `GET /api/public-renders/{token}/thumbnail` · `GET /api/public-renders/{token}/meta` — **unauthenticated**, token-gated; backs the branded `/share/{token}` landing page so a finished video can be shared without exposing the app login |
| Studio    | `POST /api/studio/generate-image` · `POST /api/studio/generate-clip` · `GET /api/studio/jobs[/{id}]` · `POST /api/studio/jobs/{id}/save` |
| Providers | `GET /api/providers/status` · `POST /api/providers/health-check` · `GET /api/providers/openrouter/video-models` · `GET /api/providers/openrouter/image-models` · `GET /api/providers/elevenlabs/voices` |
| System | `GET /api/system/info` — non-secret deployment overview (version, mock flag, ffmpeg availability, configured model ids, key-presence booleans, entity counts); backs the **Settings & Status** page |

### Rate limiting

Generation routes call paid providers, so they share a per-client token bucket
(default: burst of 10, refilling 10/minute — tune with
`RATE_LIMIT_GENERATION_BURST` / `RATE_LIMIT_GENERATION_PER_MINUTE`, or disable
with `RATE_LIMIT_ENABLED=false`). Throttled routes: `generate-plan`,
`generate-video`, `recompose`, `retry`, `shots/regenerate`,
`shots/regenerate-bulk`, `studio/generate-image`, `studio/generate-clip`, and
`music/generate`. They all draw from **one** bucket, so alternating endpoints
doesn't dodge the limit. `cancel` is deliberately exempt — it's the escape
hatch for a runaway render. Over-limit calls get `429` with a `Retry-After`
header and a human-readable `detail` (which the frontend's toast surfaces).

Clients are keyed by a hash of the session cookie (IP fallback). The limiter is
in-process behind a tiny `check()` surface (`app/services/ratelimit.py`); swap
in a Redis-backed implementation for multi-process deploys without touching
the route dependencies.

### Request tracing & structured errors

Every request gets an `X-Request-ID` (echoed on the response). An inbound id of
8–64 ASCII chars is honoured so a load balancer / SDK can correlate calls
across services; otherwise a fresh `uuid.hex` is minted. Every non-probe
request emits a single structured access-log line —
`method=... path=... status=... duration_ms=... request_id=...` — under the
`avs.access` logger; the `/health`, `/healthz`, `/readyz` probes are skipped so
the log stays signal-dense.

Uncaught exceptions become a structured `500 {detail, request_id, error_type}`
JSON response (instead of FastAPI's opaque `Internal Server Error`) plus a
single `log.exception` line that ties the traceback to the same id. The
frontend's `api.ts` reads `X-Request-ID` on failed responses and surfaces it in
toast errors as `(req 1a2b3c4d…)`, so a user pasting their toast text into a
support ticket gives the operator a direct log handle.

### Health probes

| Route | What it proves | Use it for |
| --- | --- | --- |
| `GET /health` | Process up (back-compat alias) | scripts/legacy clients |
| `GET /healthz` | Process up (liveness) | k8s `livenessProbe`, load-balancer health |
| `GET /readyz` | DB reachable + ffmpeg present + data dir writable (readiness) | k8s `readinessProbe` — returns `503` with a per-check breakdown if any of them is broken so traffic is routed away from a degraded pod |

`docker-compose.yml` wires the backend's compose healthcheck to `/healthz`, so
`docker compose ps` reports the container as `unhealthy` when the process wedges.

All routes except `GET /api/public-assets/{token}`, `/health`, `/healthz`, and `/readyz` are gated by the MVP
password cookie.

---

## Running locally without Docker

```bash
# Backend
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
DATABASE_URL=sqlite:///./data/app.db DATA_DIR=./data MOCK_PROVIDERS=true \
  uvicorn app.main:app --reload

# In another terminal
cd frontend
npm install && npm run dev
```

When Redis isn't reachable, jobs run inline in the request thread — fine for demos.

### Tests

```bash
cd backend
PYTHONPATH=. python -m pytest -q
```

The suite is fully hermetic: no network calls, no real provider keys required.
HTTP adapters are exercised via `respx`.

For a single end-to-end sanity check (auth → avatar+asset → project →
preflight → plan → cost → render → valid MP4 → public share link), run the
smoke script — it exits non-zero on any failure, so it's CI/deploy-gate ready:

```bash
cd backend && PYTHONPATH=. python -m scripts.smoke   # or: make smoke
```

### Verifying a live deployment

`scripts.smoke` runs the app in-process. To confirm an actually-running
instance (local Compose, or a deployed EC2/Nginx host) works end-to-end over
real HTTP — real auth cookie, async render polling, MP4 download with an
`ftyp` sanity check, and the unauthenticated share link — run the deploy
verifier. It creates a throwaway project, drives it to a finished video, then
deletes it (use `--keep` to leave it):

```bash
make verify BASE_URL=https://avs.example.com MVP_PASSWORD=...
# or directly:
cd backend && python -m scripts.verify_deploy --base-url http://localhost:8000 --password changeme
```

It exits non-zero on any failure, so it can gate a deploy pipeline. It also
fails fast if `ffmpeg` is missing on the host (a common deploy mistake) by
checking `/api/system/info` before attempting a render.

To answer the bigger question — *does this whole docker-compose stack actually
deploy?* — `make verify-up` boots the stack ephemerally, waits for the host
port to become reachable, runs the verifier *inside* the backend container, and
tears everything down (with volumes) on the way out. It captures
`docker compose logs` on failure and uses a randomized COMPOSE_PROJECT_NAME so
it doesn't disturb a running `make up`. Refuses to run if a `.env` already
exists at the repo root, so it can't clobber your local config.

```bash
make verify-up                           # full ephemeral boot + verify + teardown
TIMEOUT=600 make verify-up               # extend the /health wait
```

### CI

`.github/workflows/ci.yml` runs on every push/PR: a **backend** job (ruff lint
→ `pytest` → `python -m scripts.smoke`, with ffmpeg installed) and a
**frontend** job (`npm run build`, which type-checks). Both run fully mocked, so
no provider secrets are needed in CI.

---

## Deploying to EC2

Full single-host recipe — works on Ubuntu 22.04 / 24.04, t3.large or bigger
(FFmpeg + Next.js build want ≥4 GB RAM, 20 GB disk for `data/` to start).

### 1. Pick an instance + DNS

```bash
# Security group: 80/443 from 0.0.0.0/0, 22 from your IP only.
# Allocate an Elastic IP and point an A record at it.
DOMAIN=avs.example.com
```

### 2. Install Docker + Nginx + certbot on the host

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin nginx certbot python3-certbot-nginx git
sudo usermod -aG docker $USER  # log out + back in so docker works without sudo
```

### 3. Clone + configure secrets

```bash
git clone https://github.com/alindebergasl/vidslam.git
cd vidslam
cp .env.example .env

# Generate a real session secret (≥32 random bytes — the boot guard refuses
# the dev default in production-flavored config).
SESSION_SECRET=$(openssl rand -hex 32)
MVP_PASSWORD=$(openssl rand -hex 16)

cat > .env <<EOF
MOCK_PROVIDERS=false
MVP_PASSWORD=${MVP_PASSWORD}
SESSION_SECRET=${SESSION_SECRET}
PUBLIC_BASE_URL=https://${DOMAIN}
FRONTEND_ORIGIN=https://${DOMAIN}
NEXT_PUBLIC_API_BASE=https://${DOMAIN}

# Provider keys — leave blank to keep that surface mocked even with
# MOCK_PROVIDERS=false. Real chat/image/video planning needs OPENROUTER;
# real voiceover needs ELEVENLABS.
OPENROUTER_API_KEY=
ELEVENLABS_API_KEY=
ELEVENLABS_DEFAULT_VOICE_ID=

DATA_DIR=/data
DATABASE_URL=sqlite:////data/app.db
EOF
chmod 600 .env  # secrets — owner-read only

# Note: $MVP_PASSWORD is what creators type to sign in — save it somewhere
# safe; this is the only time it's printed.
echo "Sign-in password: ${MVP_PASSWORD}"
```

### 4. Boot the prod stack

The prod compose overlay swaps the frontend from `next dev` (HMR, source
mounted) to a built `next start`, drops `--reload` on the backend, and
unpublishes the 8000/3000 ports so only Nginx can reach them.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# One-shot demo cast + sample project — skip if you want a blank slate.
docker compose exec backend python -m app.seed
```

### 5. Put Nginx + TLS in front

```bash
sudo cp nginx/avatarvideostudio.conf /etc/nginx/sites-available/${DOMAIN}
sudo ln -s /etc/nginx/sites-available/${DOMAIN} /etc/nginx/sites-enabled/
# Edit server_name in the conf to match your $DOMAIN.
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ${DOMAIN} --non-interactive --agree-tos -m you@example.com
```

### 6. Verify

```bash
curl -sf https://${DOMAIN}/api/healthz   # {"status":"ok"}
curl -sf https://${DOMAIN}/api/readyz    # 200 + per-check breakdown
# Browser: https://${DOMAIN}/ → sign in with the password from step 3.
```

### 7. Backups

The `./data` host volume holds all uploads, generated assets, the SQLite DB,
and final MP4s. Nothing else is durable.

```bash
# Snapshot before maintenance:
sudo tar czf "/tmp/avs-backup-$(date +%F).tar.gz" -C /var/lib/docker/volumes data
# Or simpler if you bind-mounted to ./data:
sudo tar czf "/tmp/avs-backup-$(date +%F).tar.gz" ./data

# Cron a daily snapshot to S3:
0 4 * * * tar czf - ./data | aws s3 cp - s3://my-avs-backups/$(date +\%F).tar.gz
```

### Rotating SESSION_SECRET

Rotating the secret invalidates every signed cookie → every signed-in user
gets bounced to the login form on their next request. No DB cleanup needed.

```bash
SESSION_SECRET=$(openssl rand -hex 32)
sed -i "s/^SESSION_SECRET=.*/SESSION_SECRET=${SESSION_SECRET}/" .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### Production considerations still on the roadmap

- S3-backed `Asset.public_token` URLs instead of local filesystem (the
  current code keeps tokens unguessable but reads off-disk).
- Real auth (OAuth / magic links) replacing the shared MVP password.
- Alembic migrations once the schema needs to evolve safely in prod.
- Sentry or similar to alert on `ProviderLog.status == 'error'`.
- Move rate-limit token buckets from in-process to Redis so multi-worker /
  multi-host deployments share one budget per client.

---

## Safety

- A pre-generation script filter rejects obvious disallowed content.
- The storyboard system prompt enforces "AI-generated virtual creator" framing and
  refuses to claim avatars are real people.
- Every uploaded asset requires a "I own or have rights to use these assets" checkbox.
- API keys never leave the backend; the frontend bundle is grep-clean.
- The default render burns a small bottom-left "AI-generated virtual creator"
  disclosure overlay; users can disable per-project.

---

## Repo layout

See the rest of the directory tree for details; the high-level shape is in the plan
file under `/root/.claude/plans/`.

---

## License

MIT-style; do not ship videos that misrepresent a real person.
