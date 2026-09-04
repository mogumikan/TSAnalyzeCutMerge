"""TSファイルのパケットサイズ(188/192/204バイト)を判定するユーティリティ。

TsSplitter / rplsinfo はどちらも 188/192 バイトの TS を自動判別して読めることを
実機検証済み(2026-09-04)。204バイト(FEC付き)は両ツールとも未対応の可能性が
あるため、検出できた場合は警告を出し、必要なら 188 バイトへ変換してから渡す。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

SYNC_BYTE = 0x47

# (パケットサイズ, 同期バイトのオフセット)
CANDIDATES = [
    (188, 0),   # 素の TS (188バイト)
    (192, 4),   # M2TS / BDAV 等 (4バイトタイムスタンプ + 188バイトTS)
    (204, 0),   # FEC付き (188 + 16バイトのRS符号)
]


@dataclass
class ProbeResult:
    packet_size: Optional[int]   # 188 / 192 / 204 / None(判定不可)
    sync_offset: int             # パケット内での同期バイト位置
    confidence: float            # 0.0-1.0
    detail: str


def probe_file(path: str, sample_bytes: int = 4 * 1024 * 1024) -> ProbeResult:
    """ファイル先頭を読み取りパケットサイズを推定する。"""
    try:
        with open(path, "rb") as f:
            data = f.read(sample_bytes)
    except OSError as e:
        return ProbeResult(None, 0, 0.0, f"読み込み失敗: {e}")

    if len(data) < 204 * 8:
        return ProbeResult(None, 0, 0.0, "ファイルが小さすぎて判定できません")

    best = None
    for pkt_size, off in CANDIDATES:
        n = 0
        hit = 0
        i = off
        while i < len(data):
            n += 1
            if data[i] == SYNC_BYTE:
                hit += 1
            i += pkt_size
        if n == 0:
            continue
        ratio = hit / n
        if best is None or ratio > best[2]:
            best = (pkt_size, off, ratio, n)

    if best is None:
        return ProbeResult(None, 0, 0.0, "同期バイトのパターンを検出できません")

    pkt_size, off, ratio, n = best
    if ratio < 0.98:
        return ProbeResult(None, 0, ratio, f"同期率が低く判定不可 (最良候補 {pkt_size}B ratio={ratio:.3f})")

    return ProbeResult(pkt_size, off, ratio, f"{pkt_size}バイトTSと判定 (同期率{ratio:.1%}, サンプル{n}パケット)")


def convert_204_to_188(src: str, dst: str, chunk_packets: int = 20000) -> None:
    """204バイト(FEC)パケットから末尾16バイトを除去し188バイトTSへ変換する。"""
    pkt_in = 204
    pkt_out = 188
    buf_size = pkt_in * chunk_packets
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(buf_size)
            if not chunk:
                break
            n_full = len(chunk) // pkt_in
            out = bytearray(n_full * pkt_out)
            for i in range(n_full):
                s = i * pkt_in
                out[i * pkt_out:(i + 1) * pkt_out] = chunk[s:s + pkt_out]
            fout.write(out)


def extract_byte_range(src: str, dst: str, start_byte: int, end_byte: int,
                        packet_size: int = 188, chunk_size: int = 8 * 1024 * 1024) -> int:
    """srcの[start_byte, end_byte)をパケット境界に合わせてdstへ生バイトコピーする。

    再パケット化・再エンコードは一切行わない完全ロスレスの範囲コピー。
    戻り値は実際に書き出したバイト数。
    """
    start_byte = (start_byte // packet_size) * packet_size
    end_byte = (end_byte // packet_size) * packet_size
    if end_byte <= start_byte:
        return 0
    written = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fin.seek(start_byte)
        remaining = end_byte - start_byte
        while remaining > 0:
            chunk = fin.read(min(chunk_size, remaining))
            if not chunk:
                break
            fout.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    return written


def append_byte_range(src: str, fout, start_byte: int, end_byte: int,
                       packet_size: int = 188, chunk_size: int = 8 * 1024 * 1024) -> int:
    """srcの[start_byte, end_byte)をパケット境界に合わせ、開いている出力ファイル
    ハンドルfoutに追記する(複数ファイルの結合出力用)。戻り値は書き出したバイト数。
    """
    start_byte = (start_byte // packet_size) * packet_size
    end_byte = (end_byte // packet_size) * packet_size
    if end_byte <= start_byte:
        return 0
    written = 0
    with open(src, "rb") as fin:
        fin.seek(start_byte)
        remaining = end_byte - start_byte
        while remaining > 0:
            chunk = fin.read(min(chunk_size, remaining))
            if not chunk:
                break
            fout.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    return written


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"
