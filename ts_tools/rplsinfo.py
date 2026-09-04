"""rplsinfo.exe のラッパー。

rplsinfo は TS ファイル中の指定位置(-F 0=先頭 ... 99=終端)から番組情報(SIT/EIT)を
探索して1件分の番組情報をCSV等で出力するツール。D-VHS等のパーシャルTSでは
標準EIT(PID 0x12)が存在せず、代わりにPID 0x1F(SIT)に番組情報が入っている
ことを実機検証で確認済み(2026-09-04)。TsSplitterで分割する際は必ず -PID1f を
付与してSITを保持しておくこと(ts_tools.splitter 側で対応済み)。

1本のTS内に複数番組/複数局/日付またぎのデータが混在する場合、-F の探索位置に
よって得られる番組情報が変わる。分割後の各ファイルに対し、まず既定位置(50)で
取得を試み、失敗した場合は複数位置をスイープして最初に成功した結果を採用する。
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Optional

# rplsinfo の -f... スイッチ文字と項目名の対応(出力順もこの並びに従う)
FIELD_MAP = [
    ("f", "filename"),
    ("k", "filesize"),
    ("d", "date"),
    ("t", "start_time"),
    ("p", "duration"),
    ("z", "timezone"),
    ("c", "station"),
    ("n", "channel"),
    ("b", "title"),
    ("i", "summary"),
    ("g", "genre"),
    ("e", "detail"),
]

QUICK_FIELDS = ["f", "d", "t", "c", "n", "b"]
FULL_FIELDS = [k for k, _ in FIELD_MAP]

# 探索位置のスイープ順(まず既定の50、失敗したら周辺→端の順に試す)
SWEEP_POSITIONS = [50, 30, 70, 10, 90, 0, 99]


@dataclass
class RplsInfoResult:
    ok: bool
    position: Optional[int] = None
    data: dict = field(default_factory=dict)
    raw_row: list = field(default_factory=list)
    message: str = ""


def _build_field_switch(fields: list[str]) -> str:
    return "-" + "".join(fields)


def _run_once(exe: str, src: str, out_path: str, fields: list[str], position: int,
              limit_mb: Optional[int], timeout: int) -> tuple[int, str, str]:
    args = [exe, src, out_path, "-C", _build_field_switch(fields), "-F", str(position)]
    if limit_mb:
        args += ["-l", str(limit_mb)]
    proc = subprocess.run(
        args, capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = proc.stdout.decode("cp932", errors="replace")
    err = proc.stderr.decode("cp932", errors="replace")
    return proc.returncode, out, err


def _parse_csv_utf16(path: str, fields: list[str]) -> Optional[list]:
    with open(path, "rb") as f:
        raw = f.read()
    if not raw:
        return None
    text = raw.decode("utf-16", errors="replace")
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if row:
            return row
    return None


def get_program_info(exe: str, src: str, position: Optional[int] = 50,
                      sweep: bool = True, limit_mb: Optional[int] = 100,
                      fields: Optional[list[str]] = None,
                      sidecar_path: Optional[str] = None,
                      timeout: int = 120) -> RplsInfoResult:
    """srcの番組情報を取得する。

    position: まず試す探索位置(0-99)。Noneの場合は既定スイープの先頭(50)を使う。
    sweep: 失敗時に他の位置を順に試すかどうか。
    sidecar_path: 指定すると、成功した呼び出しの生CSV(全項目)をこのパスに保存する
                  (番組情報テキストとして分割ファイルに添付する用途)。
    """
    use_fields = fields if fields is not None else FULL_FIELDS
    positions = [position] if position is not None else []
    if sweep:
        for p in SWEEP_POSITIONS:
            if p not in positions:
                positions.append(p)
    if not positions:
        positions = [50]

    last_msg = ""
    tmpdir = tempfile.gettempdir()
    for pos in positions:
        tmp_out = os.path.join(tmpdir, f"rplsinfo_{uuid.uuid4().hex}.txt")
        try:
            rc, out, err = _run_once(exe, src, tmp_out, use_fields, pos, limit_mb, timeout)
        except subprocess.TimeoutExpired:
            last_msg = f"position={pos}: タイムアウト"
            continue
        except OSError as e:
            return RplsInfoResult(False, message=f"rplsinfo実行エラー: {e}")

        if rc != 0 or not os.path.exists(tmp_out):
            last_msg = f"position={pos}: {(out + err).strip() or '失敗'}"
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except OSError:
                    pass
            continue

        row = _parse_csv_utf16(tmp_out, use_fields)
        if not row:
            last_msg = f"position={pos}: 出力が空でした"
            try:
                os.remove(tmp_out)
            except OSError:
                pass
            continue

        key_names = [dict(FIELD_MAP)[c] for c in use_fields]
        data = {k: (row[i] if i < len(row) else "") for i, k in enumerate(key_names)}

        if sidecar_path:
            try:
                with open(tmp_out, "rb") as fin, open(sidecar_path, "wb") as fout:
                    fout.write(fin.read())
            except OSError:
                pass
        try:
            os.remove(tmp_out)
        except OSError:
            pass

        return RplsInfoResult(True, position=pos, data=data, raw_row=row,
                               message=f"position={pos}で取得成功")

    return RplsInfoResult(False, message=last_msg or "全ての探索位置で失敗しました")
