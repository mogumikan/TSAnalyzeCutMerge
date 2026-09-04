"""結合時の重複バイナリデータ検出。

同じ放送を複数回ダビングした場合など、TSファイルの末尾と次のファイルの先頭が
バイト単位で重複していることがある。これを検出して二重に含めないようにする。

アルゴリズムは nilaoda/TSMerge (https://github.com/nilaoda/TSMerge, MIT License)
の設計を参考にPythonで再実装したもの: 次ファイル先頭の一定サイズ(パターン)を
切り出し、既に書き出し済みの出力データの末尾側から後方一致検索する。見つかれば
その一致位置で出力を切り詰めてから次ファイルの全体を書き足す(パターン一致より
後ろに書かれていた分は次ファイルの内容と重複するため破棄される)。
"""
from __future__ import annotations

from typing import Optional


def find_overlap_position(output_tail: bytes, pattern: bytes) -> int:
    """output_tail(出力データ末尾側の一部)の中からpatternと完全一致する開始位置を
    後方から探す。見つからなければ-1を返す。"""
    if not pattern:
        return -1
    return output_tail.rfind(pattern)


def append_with_overlap_check(fout, src_path: str, start_byte: int, end_byte: int,
                               packet_size: int, pattern_mb: float, search_window_mb: int,
                               on_log) -> int:
    """fout(既存の出力ファイルハンドル、書き込み用)にsrc_pathの[start_byte,end_byte)を
    追記する。追記前に、src側の先頭パターンがfout側の末尾に既に存在していないかを
    調べ、存在すればその重複区間を破棄してから追記する(=単純結合より高精度)。
    戻り値は実際に新しく書き足したバイト数。
    """
    start_byte = (start_byte // packet_size) * packet_size
    end_byte = (end_byte // packet_size) * packet_size
    if end_byte <= start_byte:
        return 0

    pattern_size = int(pattern_mb * 1024 * 1024)
    pattern_size = min(pattern_size, end_byte - start_byte)
    pattern_size = max(packet_size, (pattern_size // packet_size) * packet_size)

    with open(src_path, "rb") as fin:
        fin.seek(start_byte)
        pattern = fin.read(pattern_size)

    fout.flush()
    cur_pos = fout.tell()
    window = min(int(search_window_mb * 1024 * 1024), cur_pos)
    if window >= len(pattern) and pattern:
        with open(fout.name, "rb") as frd:
            frd.seek(cur_pos - window)
            tail = frd.read(window)
        idx = find_overlap_position(tail, pattern)
        if idx != -1:
            match_abs_pos = (cur_pos - window) + idx
            on_log(f"    重複データを検出: 出力側 {match_abs_pos:,} バイト以降"
                   f"({cur_pos - match_abs_pos:,} バイト分)を破棄して結合します"
                   f"(一致パターン {len(pattern):,} バイト)")
            fout.seek(match_abs_pos)
            fout.truncate()

    written = 0
    with open(src_path, "rb") as fin:
        fin.seek(start_byte)
        remaining = end_byte - start_byte
        while remaining > 0:
            chunk = fin.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                break
            fout.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    return written
