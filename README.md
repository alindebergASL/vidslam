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
| Projects  | `GET/POST/PATCH /api/projects[/{id}]` · `PATCH /api/projects/{id}/cast` · `PATCH /api/projects/{id}/shots/{shot_id}` |
| Generation | `POST /api/projects/{id}/generate-plan` · `GET /api/projects/{id}/preflight` · `GET /api/projects/{id}/cost-estimate` · `POST /api/projects/{id}/generate-video[?force=true]` · `POST /api/projects/{id}/recompose` · `GET /api/projects/{id}/status` · `GET /api/projects/{id}/renders` · `POST /api/projects/{id}/shots/{shot_id}/regenerate[?recompose=true]` · `POST /api/projects/{id}/shots/regenerate-bulk` · `GET /api/renders/{id}/download` |
| Public share | `GET /api/public-renders/{token}` · `GET /api/public-renders/{token}/thumbnail` · `GET /api/public-renders/{token}/meta` — **unauthenticated**, token-gated; backs the branded `/share/{token}` landing page so a finished video can be shared without exposing the app login |
| Studio    | `POST /api/studio/generate-image` · `POST /api/studio/generate-clip` · `GET /api/studio/jobs[/{id}]` · `POST /api/studio/jobs/{id}/save` |
| Providers | `GET /api/providers/status` · `POST /api/providers/health-check` · `GET /api/providers/openrouter/video-models` · `GET /api/providers/openrouter/image-models` · `GET /api/providers/elevenlabs/voices` |

All routes except `GET /api/public-assets/{token}` and `/health` are gated by the MVP
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

---

## Deploying to EC2

Minimal recipe:

1. Provision an EC2 instance with Docker + Docker Compose.
2. `git clone` this repo, copy `.env.example` → `.env`, set `MVP_PASSWORD`,
   `SESSION_SECRET`, and `PUBLIC_BASE_URL` to your domain (e.g. `https://avs.example.com`).
3. Install Nginx and copy `nginx/avatarvideostudio.conf` into
   `/etc/nginx/sites-enabled/`, then `certbot --nginx` for TLS.
4. `make up` and `make seed`.

The `data/` volume is the source of truth for uploads, generated assets, and final
MP4s — back it up.

### Production considerations (out of scope for this MVP)

- Replace local-fs storage with S3 presigned URLs for `Asset.public_token`.
- Replace `MVP_PASSWORD` with real auth (OAuth, magic links, etc.).
- Add Alembic migrations once the schema needs to evolve in production.
- Add Sentry or similar for `ProviderLog` failures.
- Add rate limiting in front of `/api/studio/*` and `/api/projects/*/generate-*`.

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
