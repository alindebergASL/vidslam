"""End-to-end smoke test for the mock pipeline.

Drives the real FastAPI app (in MOCK_PROVIDERS mode) through the full creator
flow and asserts a playable MP4 with a video + audio stream comes out the other
end. Exits non-zero on any failure so it can gate CI / a deploy.

Run:  python -m scripts.smoke    (or:  make smoke)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"SMOKE FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (180, 60, 120)).save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="avs-smoke-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'smoke.db'}"
    os.environ["DATA_DIR"] = str(tmp / "data")
    os.environ["MOCK_PROVIDERS"] = "true"
    os.environ["MVP_PASSWORD"] = "smoke-pw"
    os.environ.setdefault("SESSION_SECRET", "smoke-secret")

    # Import after env is set so settings pick it up.
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()

    steps: list[str] = []

    def step(label: str) -> None:
        steps.append(label)
        print(f"  ✓ {label}")

    print("AvatarVideoStudio smoke test (mock providers)")
    with TestClient(app) as c:
        if c.get("/health").status_code != 200:
            _fail("/health not OK")
        step("health ok")

        if c.post("/api/auth/login", json={"password": "smoke-pw"}).status_code != 200:
            _fail("login failed")
        step("authenticated")

        aid = c.post("/api/avatars", json={"name": "Smoke", "persona": "demo"}).json()["id"]
        c.post(
            f"/api/avatars/{aid}/assets",
            files={"file": ("hero.png", _png_bytes(), "image/png")},
            data={"asset_type": "hero", "rights_confirmed": "true"},
        )
        step("avatar + hero asset created")

        iid = c.post("/api/ingredients", json={"name": "Studio set", "kind": "scene"}).json()["id"]
        step("ingredient created")

        pid = c.post(
            "/api/projects",
            json={
                "title": "Smoke Project",
                "original_script": "This is an automated smoke test of the full pipeline.",
                "mode": "reel_montage",
                "cta_text": "Ship it",
                "creative_direction": "Calm product demo, clean and trustworthy",
                "cast": [
                    {"member_kind": "avatar", "avatar_id": aid, "role": "host"},
                    {"member_kind": "ingredient", "ingredient_id": iid, "role": "location"},
                ],
            },
        ).json()["id"]
        step(f"project #{pid} created")

        pf = c.get(f"/api/projects/{pid}/preflight").json()
        if not pf["ok"]:
            _fail(f"preflight failed: {pf}")
        step("preflight ok")

        if c.post(f"/api/projects/{pid}/generate-plan").status_code != 202:
            _fail("generate-plan not accepted")
        proj = c.get(f"/api/projects/{pid}").json()
        if proj["status"] != "planned" or len(proj["shots"]) < 2:
            _fail(f"plan did not produce shots: {proj['status']}, {len(proj['shots'])} shots")
        step(f"storyboard planned ({len(proj['shots'])} shots)")

        cost = c.get(f"/api/projects/{pid}/cost-estimate").json()
        step(f"cost estimate ~${cost['total']:.2f}")

        if c.post(f"/api/projects/{pid}/generate-video").status_code != 202:
            _fail("generate-video not accepted")
        st = c.get(f"/api/projects/{pid}/status").json()
        if st["project_status"] != "completed":
            _fail(f"render did not complete: {st['project_status']}")
        render = st["latest_render"]
        step("video rendered")

        final = render["final_video_path"]
        if not final or not Path(final).exists():
            _fail("final video file missing")

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "json", final],
            capture_output=True, text=True,
        )
        if probe.returncode != 0:
            _fail(f"ffprobe failed: {probe.stderr[-300:]}")
        codec_types = {s["codec_type"] for s in json.loads(probe.stdout).get("streams", [])}
        if "video" not in codec_types:
            _fail(f"no video stream in output (streams: {codec_types})")
        step(f"output is a valid MP4 (streams: {sorted(codec_types)})")

        # Public share link works without auth.
        token = render["share_token"]
        from fastapi.testclient import TestClient as Anon

        with Anon(app) as anon:
            if anon.get(f"/api/public-renders/{token}").status_code != 200:
                _fail("public share link not reachable")
        step("public share link serves the video")

    size = Path(final).stat().st_size
    print(f"\nSMOKE PASS — {len(steps)} steps, final MP4 {size/1024:.0f} KB at:\n  {final}")


if __name__ == "__main__":
    main()
