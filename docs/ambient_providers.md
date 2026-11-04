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

Any OpenRouter image model works (`OPENROUTER_IMAGE_MODEL`). For anchors,
pick one that is strong on photoreal landscapes at 16:9. The anchor only
needs to be a convincing frame of the scene. I haven't verified this
month's OpenRouter image catalogue, so choose from its image-model listing.

## Music bed

| Provider | Status in this app | Notes |
| --- | --- | --- |
| **ElevenLabs Music** | ✅ wired (`ELEVENLABS_API_KEY`) | Generates a 45–90s bed per scene; the renderer crossfades it into a seamless loop. Prompt presets: spa, zen, soft piano, singing bowls, rain, acoustic. |
| Upload your own | ✅ wired (Music bed → upload on the project) | Best for licensed spa libraries; looped the same way. |
| Google Lyria 3 / 3.5 | not integrated | Full tracks of ~2–3 min (reported ~$0.04–0.08 per request), so the bed repeats less over an hour. Lyria 2 makes 30s clips; Lyria RealTime streams continuously. Gemini API / Vertex AI. **Best candidate to add next.** |
| OpenAI gpt-audio | not a music model | Speech in/out (voice), not music. Could replace ElevenLabs TTS for voiceovers in storyboard projects. |
| Mubert API | not integrated | Built for long-form generative ambient; worth adding if a 90s bed repeats too audibly over an hour. |
| Stable Audio | not integrated | Alternatives behind the same `MusicProvider` interface. |

## Settings

```bash
MOCK_PROVIDERS=false
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_VIDEO_MODEL=alibaba/wan-3.0      # or per scene: Advanced → Video model
OPENROUTER_IMAGE_MODEL=<image model id>     # anchor stills + Still+drift
ELEVENLABS_API_KEY=...                      # music bed
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
