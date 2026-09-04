"""ffmpegを使ったサムネイル生成(任意機能)。

ffmpegが見つからない/失敗した場合はNoneを返すだけで、分割処理自体には
一切影響しない(あくまでプレビュー用の付加機能)。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from typing import Optional

DEFAULT_FFMPEG = "ffmpeg"


def find_ffmpeg(candidates: Optional[list[str]] = None) -> Optional[str]:
    import shutil
    for c in (candidates or []):
        if c and os.path.exists(c):
            return c
    found = shutil.which("ffmpeg")
    if found:
        return found
    for guess in (r"C:\bin\ffmpeg.exe",):
        if os.path.exists(guess):
            return guess
    return None


def make_thumbnail(ffmpeg_exe: str, ts_path: str, out_png: str,
                    seek_sec: float = 1.0, width: int = 160, timeout: int = 20) -> bool:
    """ts_path(通常は小さなプレビュー用抜粋ファイル)から1フレームをPNGで書き出す。"""
    args = [
        ffmpeg_exe, "-y", "-v", "error",
        "-ss", str(seek_sec), "-i", ts_path,
        "-frames:v", "1", "-vf", f"scale={width}:-2",
        out_png,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and os.path.exists(out_png) and os.path.getsize(out_png) > 0


def make_thumbnail_from_range(ffmpeg_exe: str, src_path: str, start_byte: int,
                               out_png: str, preview_bytes: int = 12 * 1024 * 1024,
                               packet_size: int = 188, width: int = 160) -> bool:
    """srcの指定バイト位置付近から小さな抜粋を作り、そこからサムネイルを生成する。"""
    from . import probe as probe_mod
    tmp_ts = os.path.join(tempfile.gettempdir(), f"thumbsrc_{uuid.uuid4().hex}.ts")
    try:
        n = probe_mod.extract_byte_range(src_path, tmp_ts, start_byte,
                                          start_byte + preview_bytes, packet_size)
        if n <= 0:
            return False
        return make_thumbnail(ffmpeg_exe, tmp_ts, out_png, seek_sec=0.5, width=width)
    finally:
        if os.path.exists(tmp_ts):
            try:
                os.remove(tmp_ts)
            except OSError:
                pass
