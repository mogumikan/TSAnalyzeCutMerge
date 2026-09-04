"""番組情報からファイル名を組み立てるユーティリティ。"""
from __future__ import annotations

import re

_FORBIDDEN = '\\/:*?"<>|'
_TRANS = str.maketrans({c: "_" for c in _FORBIDDEN})


def sanitize(name: str, max_len: int = 100) -> str:
    """Windowsのファイル名として不正な文字を除去し、長さを制限する。"""
    if not name:
        return ""
    name = name.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    name = name.translate(_TRANS)
    name = re.sub(r"\s+", " ", name).strip()
    # 末尾のピリオド/空白はWindowsで問題になるため除去
    name = name.rstrip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


DEFAULT_PATTERN_OK = "{date}_{time}_{station}_{title}_{suffix}"
DEFAULT_PATTERN_NG = "{orig}_{suffix}_情報不明"


def render(pattern: str, info: dict, title_max_len: int = 60, station_max_len: int = 30) -> str:
    """パターン文字列内の {token} を info の値で置換する。存在しないキーは空文字。"""
    safe_info = dict(info)
    if "title" in safe_info:
        safe_info["title"] = sanitize(safe_info.get("title") or "", title_max_len)
    if "station" in safe_info:
        safe_info["station"] = sanitize(safe_info.get("station") or "", station_max_len)

    def repl(m):
        key = m.group(1)
        val = safe_info.get(key, "")
        return sanitize(str(val)) if key not in ("suffix", "orig", "ext") else str(val)

    out = re.sub(r"\{(\w+)\}", repl, pattern)
    out = re.sub(r"_+", "_", out)
    out = out.strip("_ ")
    return out


def unique_path(dirpath: str, filename: str, ext: str, existing: set) -> str:
    """同名ファイルが既にある場合は連番を付けて重複を回避する。"""
    import os
    base = filename
    candidate = f"{base}.{ext}"
    n = 2
    while candidate.lower() in existing or os.path.exists(os.path.join(dirpath, candidate)):
        candidate = f"{base}_{n}.{ext}"
        n += 1
    existing.add(candidate.lower())
    return candidate
