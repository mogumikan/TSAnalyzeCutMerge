"""TsSplitter.exe のラッパー。

重要: D-VHS等のパーシャルTSは標準EIT(PID 0x12)を持たず、番組情報は
PID 0x1F (SIT: Selection Information Table, ARIB STD-B10のパーシャルTS拡張)に
格納されている。TsSplitterの -EIT オプションはNIT〜TOT(0x10-0x14)は保持するが
0x1Fは対象外のため、分割後にrplsinfoが情報を読めなくなる。
これを避けるため、本ツールは常に -PID1f を付与してSITを保持する
(実機検証: 2026-09-04)。
"""
from __future__ import annotations

import csv
import io
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

PROGRESS_RE = re.compile(r"\(\s*([\d,]+)\s*MB/\s*([\d,]+)\s*MB\)\s*(\d+)%")

SPLIT_MODES = {
    "SEP2": "番組情報+PMT単位で分割(推奨・チャンネル切替やパーシャルTSに強い)",
    "SEP3": "PMT単位で分割(チャンネル/番組切替のみで分割、CM等を含む)",
    "SEP": "番組情報(EIT)単位で分割(標準EITがある完全なTS向け)",
    "NONE": "分割しない(不要データ削除のみ)",
}


@dataclass
class SplitOptions:
    split_mode: str = "SEP2"          # SEP / SEP2 / SEP3 / NONE
    keep_eit: bool = True
    keep_ecm: bool = True
    keep_emm: bool = True
    out_hd: bool = True
    out_sd: bool = True
    out_1seg: bool = True
    cs_mode: bool = False
    extra_pids_hex: list[str] = field(default_factory=list)   # 常に "1f" が追加される
    timecut: str = ""                 # "23:59:49,24:00:01" 形式(カンマ区切り)
    cut: str = ""                     # "00:30:00,01:00:00" 形式
    buff_mb: Optional[int] = None
    reset_pts: bool = False           # -PTS: 映像/音声/PCRのPTSを1:00:00から振り直す(タイムライン修復)


@dataclass
class SplitResult:
    ok: bool
    returncode: int
    created_files: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    message: str = ""


def _build_args(exe: str, src: str, out_dir: str, opts: SplitOptions) -> list[str]:
    args = [exe]
    if opts.keep_eit:
        args.append("-EIT")
    if opts.keep_ecm:
        args.append("-ECM")
    if opts.keep_emm:
        args.append("-EMM")
    if not opts.out_hd:
        args.append("-HD")
    if not opts.out_sd:
        args.append("-SD")
    if not opts.out_1seg:
        args.append("-1SEG")
    if opts.cs_mode:
        args.append("-CS")
    if opts.reset_pts:
        args.append("-PTS")

    pids = {"1f"}  # SIT: パーシャルTSの番組情報保持に必須
    for p in opts.extra_pids_hex:
        p = p.strip().lower().lstrip("0x")
        if p:
            pids.add(p)
    args.append("-PID" + ",".join(sorted(pids)))

    if opts.buff_mb is not None:
        args += ["-BUFF", str(opts.buff_mb)]

    if opts.split_mode in ("SEP", "SEP2", "SEP3"):
        args.append("-" + opts.split_mode)

    if opts.timecut.strip():
        args.append("-TIMECUT" + opts.timecut.strip())
    if opts.cut.strip():
        args.append("-CUT" + opts.cut.strip())

    args += ["-OUT", out_dir, "-LOGFILE"]
    args.append(src)
    return args


def _stream_process(args: list[str], on_line: Callable[[str], None],
                     on_progress: Callable[[int, int, int], None]) -> int:
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    buf = bytearray()
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            idx_r = buf.find(b"\r")
            idx_n = buf.find(b"\n")
            candidates = [i for i in (idx_r, idx_n) if i != -1]
            if not candidates:
                break
            idx = min(candidates)
            line_bytes = bytes(buf[:idx])
            del buf[:idx + 1]
            text = line_bytes.decode("cp932", errors="replace").strip()
            if not text:
                continue
            m = PROGRESS_RE.search(text)
            if m:
                cur = int(m.group(1).replace(",", ""))
                total = int(m.group(2).replace(",", ""))
                pct = int(m.group(3))
                on_progress(cur, total, pct)
            else:
                on_line(text)
    if buf:
        text = bytes(buf).decode("cp932", errors="replace").strip()
        if text:
            on_line(text)
    proc.wait()
    return proc.returncode


def _parse_logfile(log_path: str) -> list[str]:
    if not os.path.exists(log_path):
        return []
    with open(log_path, "rb") as f:
        raw = f.read()
    text = raw.decode("cp932", errors="replace")
    files = []
    for row in csv.reader(io.StringIO(text)):
        if row:
            files.append(row[0])
    return files


def run_split(exe: str, src: str, out_dir: str, opts: SplitOptions,
              on_line: Optional[Callable[[str], None]] = None,
              on_progress: Optional[Callable[[int, int, int], None]] = None) -> SplitResult:
    on_line = on_line or (lambda s: None)
    on_progress = on_progress or (lambda a, b, c: None)

    os.makedirs(out_dir, exist_ok=True)
    args = _build_args(exe, src, out_dir, opts)
    on_line("実行コマンド: " + " ".join(f'"{a}"' if " " in a else a for a in args))

    log_lines: list[str] = []

    def _capture(s: str):
        log_lines.append(s)
        on_line(s)

    try:
        rc = _stream_process(args, _capture, on_progress)
    except OSError as e:
        return SplitResult(False, -1, message=f"TsSplitter実行エラー: {e}")

    stem = os.path.splitext(os.path.basename(src))[0]
    log_path = os.path.join(out_dir, stem + ".log")
    created = _parse_logfile(log_path)

    if rc != 0:
        return SplitResult(False, rc, created_files=created, log_lines=log_lines,
                            message=f"TsSplitterが異常終了しました(code={rc})")

    if not created:
        # -LOGFILEのログが見つからない/空でも、フォルダ内をベースファイル名で
        # 走査してフォールバックする
        prefix = stem + "_"
        ext_orig = os.path.splitext(src)[1]
        for fn in os.listdir(out_dir):
            full = os.path.join(out_dir, fn)
            if fn.startswith(prefix) and fn.lower().endswith(ext_orig.lower()) and os.path.isfile(full):
                created.append(full)
    return SplitResult(True, rc, created_files=created, log_lines=log_lines,
                        message=f"分割完了: {len(created)}ファイル作成")
