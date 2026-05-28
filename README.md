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
| Generation | `POST /api/projects/{id}/generate-plan` · `POST /api/projects/{id}/generate-video` · `GET /api/projects/{id}/status` · `POST /api/projects/{id}/shots/{shot_id}/regenerate` · `GET /api/renders/{id}/download` |
| Studio    | `POST /api/studio/generate-image` · `POST /api/studio/generate-clip` · `GET /api/studio/jobs[/{id}]` · `POST /api/studio/jobs/{id}/save` |
| Providers | `GET /api/providers/status` · `GET /api/providers/openrouter/video-models` · `GET /api/providers/openrouter/image-models` |

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
