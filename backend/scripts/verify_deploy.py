"""Verify a *live* AvatarVideoStudio deployment end-to-end over real HTTP.

Unlike scripts.smoke (which drives the in-process app via TestClient), this hits
a running instance — local Docker Compose, or a deployed EC2/Nginx host — exactly
as a browser would: real auth cookie, real async render polling, real file
download. Use it to confirm a deploy actually works, not just that the code
imports.

Run:
    python -m scripts.verify_deploy --base-url http://localhost:8000 --password changeme
    # or via env:
    BASE_URL=https://avs.example.com MVP_PASSWORD=... python -m scripts.verify_deploy

Exits non-zero on any failure, so it can gate a deploy pipeline.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from typing import NoReturn

import httpx

STEPS: list[str] = []


def _fail(msg: str) -> NoReturn:
    print(f"VERIFY FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _step(label: str) -> None:
    STEPS.append(label)
    print(f"  ✓ {label}")


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (60, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify a live AvatarVideoStudio deploy.")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--password", default=os.environ.get("MVP_PASSWORD", "changeme"))
    ap.add_argument("--timeout", type=int, default=600, help="max seconds to wait for the render")
    ap.add_argument(
        "--keep",
        action="store_true",
        help="leave the verification project in the DB (default: delete it)",
    )
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print(f"AvatarVideoStudio deploy verification → {base}")
    # cookies=True persists the auth cookie across requests, like a browser.
    with httpx.Client(base_url=base, timeout=60.0, follow_redirects=True) as c:
        # 1. health (unauthenticated)
        try:
            r = c.get("/health")
        except httpx.HTTPError as e:
            _fail(f"cannot reach {base}/health: {e}")
        if r.status_code != 200:
            _fail(f"/health returned {r.status_code}")
        _step(f"health ok ({r.json().get('service', '?')})")

        # 2. auth
        r = c.post("/api/auth/login", json={"password": args.password})
        if r.status_code != 200:
            _fail(f"login failed ({r.status_code}) — check --password / MVP_PASSWORD")
        _step("authenticated")

        # 3. system info — confirms DB reachable + ffmpeg present on the host
        info = c.get("/api/system/info")
        if info.status_code != 200:
            _fail(f"/api/system/info returned {info.status_code}")
        sysinfo = info.json()
        if not sysinfo.get("ffmpeg_available"):
            _fail("ffmpeg is NOT available on the deployed host — renders will fail")
        _step(
            f"system info ok (v{sysinfo.get('version')}, "
            f"{'mock' if sysinfo.get('mock_providers') else 'live'} providers, ffmpeg present)"
        )

        project_id = None
        try:
            # 4. create an avatar + hero asset
            aid = c.post("/api/avatars", json={"name": "DeployCheck", "persona": "verify"}).json()["id"]
            up = c.post(
                f"/api/avatars/{aid}/assets",
                files={"file": ("hero.png", _png_bytes(), "image/png")},
                data={"asset_type": "hero", "rights_confirmed": "true"},
            )
            if up.status_code != 201:
                _fail(f"asset upload failed ({up.status_code}): {up.text[:200]}")
            _step("avatar + hero asset created")

            # 5. project
            project_id = c.post(
                "/api/projects",
                json={
                    "title": "Deploy Verification",
                    "original_script": "Automated deploy verification of the full render pipeline.",
                    "mode": "reel_montage",
                    "cta_text": "Deploy OK",
                    "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
                },
            ).json()["id"]
            _step(f"project #{project_id} created")

            # 6. preflight
            pf = c.get(f"/api/projects/{project_id}/preflight").json()
            if not pf["ok"]:
                _fail(f"preflight failed: {pf}")
            _step("preflight ok")

            # 7. plan (async → poll until shots exist or status leaves planning)
            if c.post(f"/api/projects/{project_id}/generate-plan").status_code != 202:
                _fail("generate-plan not accepted")
            _poll_until(
                c,
                project_id,
                args.timeout,
                done=lambda st, proj: proj["status"] in ("planned", "failed") and bool(proj["shots"]),
                what="storyboard plan",
            )
            proj = c.get(f"/api/projects/{project_id}").json()
            if proj["status"] == "failed" or len(proj["shots"]) < 2:
                _fail(f"planning did not produce shots (status={proj['status']})")
            _step(f"storyboard planned ({len(proj['shots'])} shots)")

            # 8. video (async → poll the render to completion)
            if c.post(f"/api/projects/{project_id}/generate-video").status_code != 202:
                _fail("generate-video not accepted")
            st = _poll_until(
                c,
                project_id,
                args.timeout,
                done=lambda st, proj: st["project_status"] in ("completed", "failed"),
                what="render",
            )
            if st["project_status"] != "completed":
                render = st.get("latest_render") or {}
                _fail(f"render failed: {render.get('error') or st['project_status']}")
            render = st["latest_render"]
            _step("video rendered")

            # 9. download the MP4 and sanity-check the bytes
            rid = render["id"]
            dl = c.get(f"/api/renders/{rid}/download")
            if dl.status_code != 200 or dl.headers.get("content-type") != "video/mp4":
                _fail(f"download failed ({dl.status_code}, {dl.headers.get('content-type')})")
            if len(dl.content) < 1000 or dl.content[4:8] != b"ftyp":
                _fail("downloaded file is not a valid MP4 (missing ftyp box)")
            _step(f"final MP4 downloaded ({len(dl.content) // 1024} KB)")

            # 10. public share link works WITHOUT the auth cookie
            token = render["share_token"]
            with httpx.Client(base_url=base, timeout=60.0) as anon:
                sr = anon.get(f"/api/public-renders/{token}")
                if sr.status_code != 200:
                    _fail(f"public share link unreachable ({sr.status_code})")
            _step("public share link serves the video (no auth)")
        finally:
            if project_id and not args.keep:
                c.delete(f"/api/projects/{project_id}")
                _step("cleaned up verification project")

    print(f"\nVERIFY PASS — {len(STEPS)} steps against {base}")


def _poll_until(client, project_id, timeout, *, done, what: str) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        st = client.get(f"/api/projects/{project_id}/status").json()
        proj = client.get(f"/api/projects/{project_id}").json()
        last = st
        if done(st, proj):
            return st
        time.sleep(2)
    _fail(f"timed out after {timeout}s waiting for {what} (last status: {last})")


if __name__ == "__main__":
    main()
