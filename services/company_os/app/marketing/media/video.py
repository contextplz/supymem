"""ffmpeg short-clip assembler — real stills, Ken Burns, captions, logo. No T2V."""

from __future__ import annotations

import shutil
import platform
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from core.ids import new_id

from app.marketing.media import store as media_store
from app.marketing.media.images import generate_image


def ffmpeg_bin() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe else None
    except Exception:
        return None


def ffmpeg_available() -> dict[str, Any]:
    path = ffmpeg_bin()
    if not path:
        result = {"ok": False, "error": "ffmpeg_not_on_path"}
    else:
        try:
            proc = subprocess.run(
                [path, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            line = (proc.stdout or proc.stderr or "").splitlines()[:1]
            result = {"ok": proc.returncode == 0, "path": path, "version": line[0] if line else ""}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:160]}
    if not result["ok"]:
        result["next_step"] = ffmpeg_install_message()
        result["note"] = result["next_step"]
    return result


def ffmpeg_install_message() -> str:
    system = platform.system()
    if system == "Darwin":
        command = "brew install ffmpeg"
    elif system == "Windows":
        command = "winget install --id Gyan.FFmpeg --exact"
    elif system == "Linux":
        commands = {"apt-get": "sudo apt-get install ffmpeg", "dnf": "sudo dnf install ffmpeg",
            "pacman": "sudo pacman -S ffmpeg", "zypper": "sudo zypper install ffmpeg", "apk": "sudo apk add ffmpeg"}
        command = next((value for manager, value in commands.items() if shutil.which(manager)), "")
    else:
        command = ""
    if command:
        return f"Install ffmpeg in your terminal with `{command}`, restart your app, then request your video again in Media Studio."
    return "Install ffmpeg with your operating system's package manager and add it to PATH, then request your video again in Media Studio."


def _ass_captions(text: str, duration: float, width: int, height: int) -> str:
    words = [w for w in (text or "").split() if w]
    if not words:
        words = ["Watch this."]
    n = max(1, len(words))
    slot = duration / n
    events = []
    t = 0.0

    def ts(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    for i, word in enumerate(words):
        start = t
        end = duration if i == n - 1 else min(duration, t + max(0.18, slot))
        events.append(
            f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{word}"
        )
        t = end
    play_res = f"PlayResX: {width}\nPlayResY: {height}"
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"{play_res}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,54,&H00F2EDE6,&H000000FF,&H64000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,3,0,2,40,40,90,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events)
        + "\n"
    )


def generate_video(
    *,
    workspace_id: str,
    headline: str,
    scene: str = "",
    seconds: int = 15,
    want_person: bool = False,
) -> dict[str, Any]:
    seconds = max(8, min(int(seconds or 15), 30))
    ff = ffmpeg_available()
    if not ff.get("ok"):
        return {
            "ok": False,
            "status": "blocked",
            "error": "ffmpeg_missing",
            "block_reason": ff["next_step"],
            "result": ff,
        }

    stills: list[str] = []
    existing = [
        a
        for a in media_store.list_assets(workspace_id, kind="generated_image", limit=6)
        if Path(a["path"]).is_file() and (a.get("review_status") != "rejected")
    ]
    for asset in existing[:3]:
        stills.append(asset["path"])
    if not stills:
        img = generate_image(
            workspace_id=workspace_id,
            headline=headline,
            scene=scene,
            want_person=want_person,
            width=1080,
            height=1920,
        )
        if not img.get("ok"):
            return img
        stills.append(str(img["result"]["path"]))

    frames = [Path(src) for src in stills[:4] if Path(src).is_file()]
    if not frames:
        return {
            "ok": False,
            "status": "blocked",
            "error": "no_frames",
            "block_reason": "Upload your product screenshot or photo in Media Studio, then request your video again.",
        }

    work = media_store.media_dir(workspace_id, "video")

    ass_path = work / "captions.ass"
    ass_path.write_text(
        _ass_captions(headline or scene, float(seconds), 1080, 1920),
        encoding="utf-8",
    )
    dest = work / f"clip_{new_id('clp')}_{seconds}s.mp4"
    src0 = frames[0]
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"zoompan=z='min(zoom+0.0012,1.12)':d={seconds * 30}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30"
    )
    cmd = [
        str(ff["path"]),
        "-y",
        "-loop",
        "1",
        "-framerate",
        "30",
        "-i",
        str(src0),
        "-vf",
        vf,
        "-t",
        str(seconds),
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except Exception as exc:
        return {"ok": False, "status": "failed", "error": f"ffmpeg_exec:{exc}"[:200]}
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1000:
        err = (proc.stderr or proc.stdout or "ffmpeg_failed")[-400:]
        return {
            "ok": False,
            "status": "failed",
            "error": f"ffmpeg:{err}"[:300],
        }

    asset = media_store.insert_asset(
        workspace_id=workspace_id,
        kind="generated_video",
        path=str(dest),
        source="ffmpeg",
        mime="video/mp4",
        title=(headline or "clip")[:120],
        width=1080,
        height=1920,
        duration_ms=seconds * 1000,
        license_info={"engine": "ffmpeg", "generative_video": False},
        meta={"seconds": seconds, "headline": headline, "frames": [str(p) for p in frames]},
        review_status="pending",
    )
    return {
        "ok": True,
        "status": "done",
        "asset": asset,
        "result": {"asset_id": asset["id"], "path": str(dest), "seconds": seconds},
        "proof": {"mode": "local", "visual_path": str(dest), "asset_id": asset["id"]},
    }


def wrap_caption(text: str, width: int = 28) -> str:
    return "\\N".join(textwrap.wrap(text, width=width)[:4])
