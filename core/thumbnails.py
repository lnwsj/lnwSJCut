from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Optional


def _file_fingerprint(src_path: Path) -> Optional[str]:
    try:
        st = src_path.stat()
        return f"{src_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except Exception:
        return None


def _cache_png_path(cache_dir: Path, key: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return cache_dir / f"{digest}.png"


def _run_ffmpeg(cmd: list[str]) -> bool:
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return True
    except Exception:
        return False


def generate_thumbnail(
    ffmpeg_path: str,
    src: str,
    in_sec: float,
    cache_dir: Path,
    width: int = 320,
) -> Optional[str]:
    """
    Generate (or reuse) a cached video thumbnail PNG for the given source/time.
    """
    src_path = Path(src)
    if not src_path.exists():
        return None

    fp = _file_fingerprint(src_path)
    if not fp:
        return None

    try:
        t = max(0.0, float(in_sec))
    except Exception:
        t = 0.0
    w = max(16, int(width or 320))

    out = _cache_png_path(cache_dir, f"thumb|{fp}|{t:.3f}|{w}")
    try:
        if out.exists() and out.stat().st_size > 0:
            return str(out)
    except Exception:
        pass

    cmd = [
        ffmpeg_path,
        "-y",
        "-ss",
        f"{t:.6f}",
        "-i",
        str(src_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={w}:-1:flags=lanczos",
        str(out),
    ]
    if not _run_ffmpeg(cmd):
        try:
            if out.exists():
                out.unlink()
        except Exception:
            pass
        return None
    try:
        return str(out) if out.exists() and out.stat().st_size > 0 else None
    except Exception:
        return None


def generate_waveform(
    ffmpeg_path: str,
    src: str,
    in_sec: float,
    duration: float,
    cache_dir: Path,
    width: int = 320,
    height: int = 48,
    color_hex: str = "0x84D1FF",
) -> Optional[str]:
    """
    Generate (or reuse) a cached waveform PNG for the selected clip segment.
    """
    src_path = Path(src)
    if not src_path.exists():
        return None

    fp = _file_fingerprint(src_path)
    if not fp:
        return None

    try:
        t = max(0.0, float(in_sec))
    except Exception:
        t = 0.0
    try:
        dur = max(0.0, float(duration))
    except Exception:
        dur = 0.0
    if dur <= 0.01:
        return None

    w = max(32, int(width or 320))
    h = max(12, int(height or 48))
    color = str(color_hex or "0x84D1FF")

    out = _cache_png_path(cache_dir, f"wave|{fp}|{t:.3f}|{dur:.3f}|{w}|{h}|{color}")
    try:
        if out.exists() and out.stat().st_size > 0:
            return str(out)
    except Exception:
        pass

    filter_expr = f"aformat=channel_layouts=mono,showwavespic=s={w}x{h}:colors={color}"
    cmd = [
        ffmpeg_path,
        "-y",
        "-ss",
        f"{t:.6f}",
        "-t",
        f"{dur:.6f}",
        "-i",
        str(src_path),
        "-frames:v",
        "1",
        "-filter_complex",
        filter_expr,
        str(out),
    ]
    if not _run_ffmpeg(cmd):
        try:
            if out.exists():
                out.unlink()
        except Exception:
            pass
        return None
    try:
        return str(out) if out.exists() and out.stat().st_size > 0 else None
    except Exception:
        return None

