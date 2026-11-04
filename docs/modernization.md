# Modernization review (Nov 2026)

Checked against OpenRouter's live model list (694 models), ElevenLabs'
and Replicate's current docs, and the npm/PyPI registries.

## Applied in this pass

| Area | Was | Now | Why |
| --- | --- | --- | --- |
| Storyboard planner default | `openai/gpt-4o-mini` (Jul 2024, $0.15/$0.60 per M tokens) | `anthropic/claude-haiku-5.5` (Oct 2026, $0.10/$0.50) | 2+ years newer and cheaper. `openai/gpt-6-luna` is the same price if you'd rather use OpenAI. For the best scripts, `anthropic/claude-sonnet-5.5` or `openai/gpt-6.1-terra` cost about $0.02–0.03 per plan. All are switchable in Settings → Models. |
| Planner output | `json_object` mode + lenient parsing | **Structured outputs** (`json_schema` with the StoryboardPlan schema) where the model supports them, falling back to `json_object`, then plain prompting | Every current OpenRouter model checked supports `structured_outputs`, so plans come back in the expected shape. |
| ElevenLabs voice default | `eleven_turbo_v2_5` | `eleven_multilingual_v2` | ElevenLabs deprecated Turbo v2.5 (use Flash). Multilingual v2 is their quality pick for the TTS endpoint; `eleven_flash_v2_5` is the fast one. Eleven v4 is selectable via OpenRouter (`elevenlabs/eleven-v4`). |
| ElevenLabs music | default `music_v1` (deprecated, removed 2027-01-31) | `music_v2_5` | Done in an earlier pass; noted here for completeness. |
| Image / video defaults | empty | `.env.example`: `google/gemini-nano-banana-2.1`, `alibaba/wan-3.0` | Wan 3.0 makes 30s clips with first+last frame (closed ambient loops). Nano Banana is reference-capable (avatar consistency). Both are starting points to compare in the Models panel. |
| LoRA trainer pin | `ostris/flux-dev-lora-trainer:b6af14…` | `…:26dce37a…` (latest) | Replicate trainings require an explicit version. I couldn't confirm a newer recommended trainer (e.g. for FLUX 3 or Qwen-Image); revisit when one is listed. |
| SQLite | default settings | WAL mode + 30s busy timeout | Production shares one SQLite file between 2 web workers and the RQ worker; without these, concurrent writes fail with "database is locked". |
| Next.js | 14.2.13 | 14.2.35 (latest 14.x patch) | Picks up the security fixes released for the 14.2 line since. Builds and passes smoke and a11y unchanged. |

## Recommended next (bigger changes, not done yet)

1. ✅ **Done: Postgres for production** (see `docs/postgres.md`). SQLite plus WAL was a stopgap. With multiple
   writers (web workers, RQ worker, background catalog poller) and long
   renders writing progress, Postgres removes lock contention and makes
   backups and replicas standard. The code is SQLAlchemy and Alembic
   throughout, so the work is a `postgres` service in the prod compose
   overlay, `psycopg[binary]`, and a one-time data copy. Effort: small to
   medium.
2. ✅ **Done: webhooks instead of polling for video jobs** (see README → Video jobs). OpenRouter's
   `POST /videos` takes a `callback_url`. Today a worker sleeps in a poll
   loop for each clip, up to about 10 minutes per clip, which ties up a
   worker for chained loops. Callbacks free the worker and are faster.
   Effort: medium.
3. ✅ **Done: Next 16.4, React 19.3, Tailwind 4.3** (TypeScript 5.9). Before/after screenshots of 10 routes showed no visual regressions. These
   are major versions with breaking changes: async `params`/`searchParams`
   in Next 15+, and Tailwind 4's CSS-first config replacing
   `tailwind.config.ts`. It's worth doing for performance (Turbopack builds,
   React compiler) and to stay inside the security-support window. Effort:
   medium. Best done as its own PR, with the smoke and a11y suites as the
   safety net.
4. **Real user accounts.** Auth is a single shared password. Per-user
   accounts (or SSO) are needed before more than one person or team uses
   it, especially now that the app connects a YouTube channel and spends
   on paid APIs. Effort: medium.
5. ✅ **Done: spend tracking from provider responses** (see README → Spend tracking). OpenRouter returns
   `usage.cost` on chat and video jobs, so the app could record real spend
   per render instead of rate-card estimates, and enforce a budget cap.
   Effort: small to medium.
6. **Test client deprecation.** Starlette now warns that `httpx` with
   `TestClient` is deprecated (it suggests `httpx2`). It's harmless today;
   switch when upgrading FastAPI or Starlette. Effort: small.

Backend Python dependencies are current (only SQLAlchemy has a patch
release, 2.1.8). The Docker base images are current (Python 3.12, Node 22
LTS).
