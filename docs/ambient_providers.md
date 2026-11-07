# Recommended providers for Ambient scenes

What matters for an hour-long ambient loop is different from a short reel:

1. **Clip length per call.** Longer clips mean the scene repeats less often
   and fewer calls are chained.
2. **Last-frame support.** If a model can be pinned to *end* on a given
   image, the app makes an anchor still, uses it as clip 1's first frame
   **and** the final clip's last frame, and the loop closes by itself
   ("Closed loop" in the UI). Without it, a 2s tail→head crossfade hides
   the seam ("Open loop"). That works, but you can see a soft dissolve once
   per loop.
3. **Calm, low-motion output.** The app already appends "locked-off static
   camera, no cuts, no people, very slow motion" to every prompt, and the
   Stillness control can slow a clip to ½ speed.

The app reads each model's limits live from OpenRouter
(`GET /api/v1/videos/models`: `supported_durations`, `supported_frame_images`)
and plans the loop automatically. The builder shows the plan, e.g.
`◎ anchor  10s + 10s + 10s → 28.5s unit × 127 = 1h`.

## Video: via OpenRouter (the adapter already wired in)

Limits below come from OpenRouter's live model listing (checked Nov 2026).
Prices change often, so check each model's `pricing_skus` on that listing.

| Model id (`OPENROUTER_VIDEO_MODEL`) | Per-call length | Last frame | 30s loop = | Notes |
| --- | --- | --- | --- | --- |
| **`alibaba/wan-3.0`** | 2–30s | ✅ | 1 call, closed | **Top pick.** A whole 30s loop in one call, closed on the anchor. `wan-3.0-prime` is the higher-tier variant. |
| **`bytedance/seedance-2.5`** | 4–30s | ✅ | 1 call, closed | Equally good on length; try both on your scenes. |
| `black-forest-labs/flux-3-video` | 5–20s | ✅ | 2 calls (20+10), closed | |
| `kwaivgi/kling-v3.0-pro` | 3–15s | ✅ | 2 × 15s, closed | Strong realism for water and fire; `-std` is the cheaper tier. |
| `minimax/hailuo-3` / `hailuo-3-max` | 5–15s | ✅ | 2 × 15s, closed | |
| `bytedance/seedance-2.0` (`-fast`, `-mini`) | 4–15s | ✅ | 2 × 15s, closed | `-fast` and `-mini` for cheaper drafts. |
| `google/veo-3.1` (`-fast`, `-lite`) | 4, 6 or 8s | ✅ | 4 calls (8+8+8+6), closed | Very high quality, but an 8s cap means more calls. |
| `alibaba/wan-2.7` | 2–10s | ✅ | 3 × 10s, closed | |
| `runway/gen-4.5` | 2–10s | ❌ first only | 3 × 10s, **open** | Usable; the seam is a crossfade. |
| `x-ai/grok-imagine-video(-1.5)` | 1–15s | ❌ first only | 2 × 15s, open | |
| `minimax/hailuo-2.3`, `alibaba/wan-2.6` | 5/6–10s | ❌ first only | 3 × 10s, open | |

| `google/gemini-omni-1.2-flash` / `-1.1-flash` | 3–15s | ✅ | 2 × 15s, closed | Always generates audio and rejects `generate_audio: false`; the app retries without the flag (clip audio is never used). In a Nov 2026 test, 1.1 redrew rock with an embossed texture and blob-like water (Wan kept the still's look); 1.2 was failing on OpenRouter at the time. |
| `heygen/heygen-video-1` | 5–15s (live listing has shown up to 30s) | ❌ first only | 2 × 15s, open | General scene model (released Sep 30 2026), very cheap (~$0.01–0.015/s), but 480p/768p only, so it gets upscaled to 1080p. Good for cheap drafts. |

Not suitable for scenes: `heygen/avatar-iv` (a talking-head model that
animates a photo with lip sync; a good fit for this app's Talking Head
mode, not for Ambient), and `flux-video-edit`, `flux-video-upscale` and
`runway/aleph-2` (they edit existing video rather than generate it).

**Cheapest option for a perfect loop:** set the scene source to **Still +
slow drift**. One image-model call gives an hour with zero seam, because
the motion (a gentle periodic push-in) returns exactly to its start. You
lose real water or flame motion; it works best for night skies, zen
gardens, candles and misty landscapes.

## Images (anchor still, Still + drift)

Scene stills use `AMBIENT_STILL_MODEL` (default **openai/gpt-image-2.6-sunburst**),
or the scene's own *Picture model* (builder step 2). If a call fails, it is
retried once with `AMBIENT_STILL_FALLBACK_MODEL` (default
**google/gemini-nano-banana-2.1**). Leave `AMBIENT_STILL_MODEL` empty to use
the active image model instead (`OPENROUTER_IMAGE_MODEL` / Settings → Models).

Every still is requested at the model's best settings, read from
`GET /images/models/{id}/endpoints`: the highest `quality` it offers (GPT
Image: "max"; the default, "auto", is a cheap tier), `resolution` "2K" where
offered (Gemini, FLUX; 4K came out crunchier, not better), and the scene's
aspect ratio.

Blind tests in Nov 2026 (AI judges, one image per model and prompt):
- **The prompt mattered more than the model.** With the app's earlier
  prompt ("photorealistic, highly detailed", turquoise water, golden hour),
  every model looked AI-made. A prompt written as a real photograph (place,
  one plain light, lens/aperture/shutter, "natural colour, not an enhanced
  or cinematic image") won on all 11 models and all 9 presets.
- **Sunburst at "max" made the most realistic stills**, about $0.13 and
  2 minutes each, with a 1536×864 cap through OpenRouter. Nano Banana 2.1 at
  2K was a close second at about $0.05. FLUX.3 and FLUX.3 Klein trailed.
- Follow-up test: rewriting a typed description as a photograph (planner
  model) lifted hype-heavy descriptions from 4.7–5.7 to 6.3–7.0 and left a
  plain one unchanged. Close-up framings fixed the weakest presets: a
  firebox close-up (no grate or poker, which models malformed), two candles
  with uneven wear (not rows of identical drips), and rain landing on one
  banana leaf (not a flat rain overlay). Night from a day still still reads
  as a "day-for-night" grade whatever the wording.
- OpenRouter reserves a request's maximum cost up front. Several Sunburst
  "max" stills at once on a low balance return 402; the fallback covers that.

## Music bed

| Provider | Status in this app | Notes |
| --- | --- | --- |
| **ElevenLabs Music** | ✅ wired (`ELEVENLABS_API_KEY`) | `music_v2_5`, pinned because `music_v1`, the API's default, is deprecated and removed 2027-01-31. Uses `force_instrumental`, 3s–5 min per call. Needs a **paid** plan; read the licensing note below before publishing. |
| **Google Lyria 3 Pro / Clip, via OpenRouter** | ✅ wired (same `OPENROUTER_API_KEY`), **experimental** | `google/lyria-3-pro-preview`: full songs, $0.08/song. `google/lyria-3-clip-preview`: 30s clips, $0.04. OpenRouter's pages document only a plain chat call, so the app requests audio the way OpenRouter's chat-audio models work and accepts any of the documented response shapes. Confirm with one real generation. |
| **Google Lyria 3.5, via Google** | ✅ wired (`GEMINI_API_KEY`) | Not on OpenRouter. Full tracks of a couple of minutes, ~$0.08/song, paid Gemini tier. Also the fallback route for Lyria 3 Pro/Clip when there's no OpenRouter key. |
| Upload your own | ✅ wired (Music bed → upload on the project) | Best for licensed spa libraries; looped the same way. |
| OpenAI gpt-audio | not a music model | Speech in/out. Available under **Voice**. |
| Mubert API, Stable Audio | not integrated | Would slot in behind the same `MusicProvider` interface. |

Every bed (45–90s, or a couple of minutes with Lyria) is crossfaded into a
seamless loop under the scene. Prompt presets: spa, zen, soft piano,
singing bowls, rain, acoustic.

## Nature sound

A second audio bed (rain, thunderstorm, crackling fire, ocean waves,
stream, waterfall, forest birds, night crickets, soft wind), mixed under the
music at its own volume, or on its own with music set to "No music".
"Match the scene" picks one from the weather, then the scene preset.

| Provider | Status in this app | Notes |
| --- | --- | --- |
| **ElevenLabs Sound Effects** | ✅ wired (`ELEVENLABS_API_KEY`) | `POST /v1/sound-generation`, one 30 s clip with `loop: true`. Model pinned to `eleven_text_to_sound_v3`, because the API's default (`…_v2`) is deprecated and removed 2027-03-01. About 40 credits/s; the app logs `COST_SOUND_PER_GENERATION` (default $0.10) as a rate-card estimate. |
| Demo mode | placeholder | Filtered brown noise, so the full audio path renders without an API call. |

The clip is crossfaded into a seamless loop at render, like the music bed.

### ElevenLabs Music licensing (for ambient channels)

From ElevenLabs' Music Terms (Exhibit A, updated 9 Oct 2026). Check the
current terms before you publish:

- **Free**: no API access. **Starter**: streaming-platform distribution is
  *prohibited*. **Creator and up**: distribution permitted, no attribution
  needed.
- Commercial use is allowed except film, TV, radio and Studio Games
  (Enterprise excepted).
- **Building a "Music Library" from output is prohibited** below
  Enterprise. Decide whether a channel of ambient tracks counts before
  relying on ElevenLabs beds at scale.
- Monthly generation and download minutes are capped per plan (for
  example, Creator: 62 min generated / 250 min downloaded).
- Prompts can't name artists, songs, albums or labels.

## Switching models (Settings → Models)

The **Models** panel lists what's available *right now*, polled from
OpenRouter (`/models`, `/images/models`, `/videos/models`) and, with a
Gemini key, Google's model list. It refreshes every
`MODEL_CATALOG_REFRESH_HOURS` (default 6) and on **Refresh now**; models that
appeared since the previous poll get a **new** badge. Pick one per kind and
it applies to the next job, no restart needed. `.env` values are the
defaults that **Reset to default** returns to.

| Kind | Options |
| --- | --- |
| Planner | Any OpenRouter text model, Claude included. JSON mode is used only where the model advertises `response_format`; otherwise the reply's JSON is extracted from plain text. |
| Image | OpenRouter Image API models (`POST /images`). |
| Video | Any OpenRouter video generator; loop planning uses each model's limits. |
| Voice | **ElevenLabs** (per-avatar voices), OpenRouter **speech** models (`/audio/speech`: gpt-4o-mini-tts, Gemini TTS, HeyGen Voice, …), or **gpt-audio** models (streamed chat audio). The voice is chosen in the panel. |
| Music | **ElevenLabs** (`music_v2_5` / `music_v2`), **Lyria 3 Pro / Clip via OpenRouter**, or **Lyria 3.5 via Google**. |
| Lip-sync | Off, or **heygen/avatar-iv**: Talking Head shots are made from the avatar's hero photo plus the voiceover. **Experimental**: OpenRouter doesn't document how Avatar IV receives speech, so the app sends the portrait, the voiceover (as an audio reference) and the script, and falls back to the regular video model if the job fails. |

## Settings

```bash
MOCK_PROVIDERS=false
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_VIDEO_MODEL=alibaba/wan-3.0      # or per scene: step 2 → Video model
OPENROUTER_IMAGE_MODEL=<image model id>     # Studio images (and scene stills if AMBIENT_STILL_MODEL is empty)
AMBIENT_STILL_MODEL=openai/gpt-image-2.6-sunburst          # scene stills; per scene: step 2 → Picture model
AMBIENT_STILL_FALLBACK_MODEL=google/gemini-nano-banana-2.1 # retried if a still fails
ELEVENLABS_API_KEY=...                      # ElevenLabs voices / music
GEMINI_API_KEY=...                          # Lyria 3.5 (Lyria 3 Pro/Clip work via OpenRouter)
PUBLIC_BASE_URL=https://your-host           # providers fetch anchor/first frames from here
AMBIENT_DEFAULT_MAX_CLIP_SECONDS=10         # only used if a model's limits can't be read
```

`PUBLIC_BASE_URL` must be reachable from the internet: video providers
download the anchor and chained first-frame images from
`/api/public-ambient-frames/{token}`, the same way they fetch cast
reference images.

## What the app sends (for debugging)

Each clip request is `POST /api/v1/videos` with `duration` snapped to a
value the model supports, `aspect_ratio`, `generate_audio: false` (clip
audio is replaced by the music bed, so paying for it is waste), and for
chained or anchored clips:

```json
"frame_images": [
  {"type": "image_url", "image_url": {"url": "https://host/api/public-ambient-frames/…"}, "frame_type": "first_frame"},
  {"type": "image_url", "image_url": {"url": "https://host/api/public-ambient-frames/…"}, "frame_type": "last_frame"}
]
```

`last_frame` is only sent on the final clip, and only to models that list it.

## Sogni (image + video, paid in Spark)

With `SOGNI_API_KEY` set, every image and video model Sogni's workflow API
accepts appears in the pickers as `sogni/<selector>`. Sogni has no endpoint
that lists them; the list comes from its published tool schemas
(`SOGNI_TOOLS_MANIFEST_URL`, synced with the model catalog, with a copy
bundled in `backend/app/providers/sogni_tools.json`). Our notes and documented
prices are in `backend/app/providers/sogni.py`; a newly listed model shows up
with estimated defaults until it gets its own notes. The synced list is a file
in `DATA_DIR`, so every worker process picks up a sync; a manifest missing any
of the three tools' model lists is refused and the last good list kept (the
catalog shows the error). An empty `SOGNI_TOOLS_MANIFEST_URL` uses only the
bundled copy.

- **Next-Gen Diffusion** (Sogni's own category, listed first): Krea 2 Turbo,
  MOSS-Image (50-step and 8-step), Dark Beast Krea 2 / Z-Image Turbo,
  Z-Image (Turbo), Chroma (v48 Detail, 1 HD, v46 Flash), Qwen Image 2512.
  Then FLUX.3 Klein, the vendor models, and SDXL community checkpoints.
- **Free vs Premium Spark.** Sogni's own models run on Free Spark. Vendor
  models (GPT Image, Seedream, Seedance, HappyHorse, Wan 3.0 via Sogni) need
  bought Premium Spark; Free Spark gets a 402 "requires Premium Spark Points".
  The "uncensored" vendor models also need the one-time likeness agreement in
  the Sogni app.
- **Closed loops on Free Spark.** LTX 2.5 (`sogni/ltx25`) and the MiniMax H3
  first + last frame models (`sogni/minimax-h3-*flf2v*`) pin both ends of a
  clip (`animate_photo`, `frameRole: "both"`), so ambient loops close on the
  anchor still. Tested Nov 2026: the last frame came back within 3.0x (LTX)
  and 1.9x (H3 FastH3 flf2v) of a normal frame step, as close as Wan 3.0.
- **Which tool lists a model decides how it's used:** `animate_photo` models
  take an exact first frame; models listed only there need a starting picture
  (the app says so instead of sending an invalid request). MiniMax H3 `r2v`
  models need at least one reference picture. Ambient scenes handle these:
  a picture-only model starts from the scene's picture or a still made first;
  an `r2v` model gets that picture or still as its (loose) reference; a first
  + last frame only model makes one closed clip (up to 15 s) instead of a
  chain, since the open middle clips of a chain have no last frame.
- **Request fields follow the schema's per-model rules:** `negativePrompt`
  only to LTX and Wan 2.2 (not MiniMax H3, Seedance, HappyHorse or Wan 3),
  and no `generateAudio` to Wan 2.2 or HappyHorse. HappyHorse 1.1 `i2v`
  needs a first frame and `r2v` reference pictures, although Sogni lists
  both under text-to-video too. The "uncensored" tag follows the schema's
  list of NSFW-capable image models, and the video models named uncensored /
  spicy.
- **No image-to-image yet.** The schema documents a source image for Z-Image,
  Z-Image Turbo and Krea 2 Turbo, but through the workflow API it isn't
  applied (tested Nov 2026 on three models at two strengths: nothing of the
  source came through, and Sogni's billing request showed
  `startingImageStrength: null`). So Sogni picture models get text only, and
  an ambient scene's "same place, other time of day" stills try the fallback
  picture model (`AMBIENT_STILL_FALLBACK_MODEL`) first, which takes the
  reference, and the scene's Sogni model (from the text alone) if that fails.
  The estimate prices those stills at the fallback model.
- **Spend** is recorded from Sogni's price table (1 Spark = $0.005) as an
  estimate: Sogni's workflow results carry no cost. Free-Spark jobs didn't
  change the account balance (`/v2/account/balance`) in our tests.

