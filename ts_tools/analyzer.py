"""分割済み(TsSplitterによる粗い区切り)の1ファイルの中身をさらに解析し、
番組情報(タイトル/日時)が変化する地点を検出して「番組候補」の一覧を作る。

同一チャンネル・同一PMTのまま複数の番組が連続して録画されているケース
(TsSplitterのPMT基準分割では検出できない境界)に対応するための機能。

手法: rplsinfoの -F(0-99の位置指定)を使い、ファイルを等間隔にサンプリングして
タイトルが変化する区間を見つけ、隣接サンプル間で二分探索して境界を絞り込む。
実測(2026-09-04)で 267MBのファイルに対し25点サンプリング+絞り込みが1秒未満で
完了することを確認済み。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import rplsinfo as rplsinfo_mod

QUICK_FIELDS = ["d", "t", "p", "c", "n", "b"]


@dataclass
class ProgramCandidate:
    start_pct: int
    end_pct: int
    start_byte: int
    end_byte: int
    info: dict = field(default_factory=dict)   # 空dictなら情報取得できなかった区間
    selected: bool = True
    thumbnail_path: Optional[str] = None
    group_override: Optional[str] = None   # レビュー画面で手動指定した結合グループ名
    order_override: Optional[int] = None   # レビュー画面で手動指定したグループ内の結合順

    @property
    def size(self) -> int:
        return max(0, self.end_byte - self.start_byte)

    def signature(self):
        return (self.info.get("date"), self.info.get("start_time"), self.info.get("title"))


def _signature(data: Optional[dict]):
    if not data:
        return None
    return (data.get("date"), data.get("start_time"), data.get("title"))


def _sample(rplsinfo_exe: str, path: str, pct: int, limit_mb: int) -> Optional[dict]:
    info = rplsinfo_mod.get_program_info(
        rplsinfo_exe, path, position=pct, sweep=False, limit_mb=limit_mb, fields=QUICK_FIELDS,
    )
    return info.data if info.ok else None


def analyze_segment(rplsinfo_exe: str, path: str, num_points: int = 25,
                     limit_mb: int = 20, min_gap_pct: int = 1,
                     on_log: Optional[Callable[[str], None]] = None) -> list[ProgramCandidate]:
    """pathの中身を粗くサンプリングし、番組候補の区間リスト(pct単位)を返す。"""
    on_log = on_log or (lambda s: None)
    size = os.path.getsize(path)
    if size <= 0:
        return []

    num_points = max(3, num_points)
    step = max(1, 99 // (num_points - 1))
    pcts = sorted(set(list(range(0, 99, step)) + [99]))

    cache: dict[int, Optional[dict]] = {}

    def get(pct: int) -> Optional[dict]:
        if pct not in cache:
            cache[pct] = _sample(rplsinfo_exe, path, pct, limit_mb)
        return cache[pct]

    for p in pcts:
        get(p)
    on_log(f"    粗サンプリング {len(pcts)}点完了")

    # 隣接サンプル間で信号(タイトル等)が変わる場所を二分探索で絞り込む
    boundaries: list[int] = [0]
    for a, b in zip(pcts, pcts[1:]):
        sig_a = _signature(get(a))
        sig_b = _signature(get(b))
        if sig_a == sig_b:
            continue
        lo, hi = a, b
        while hi - lo > min_gap_pct:
            mid = (lo + hi) // 2
            sig_mid = _signature(get(mid))
            if sig_mid == sig_a:
                lo = mid
            else:
                hi = mid
        if hi not in boundaries:
            boundaries.append(hi)
    boundaries.append(100)
    boundaries = sorted(set(boundaries))
    on_log(f"    境界候補: {boundaries}")

    candidates: list[ProgramCandidate] = []
    for start_pct, end_pct in zip(boundaries, boundaries[1:]):
        probe_pct = min(99, start_pct)
        data = get(probe_pct) or {}
        start_byte = int(size * start_pct / 100)
        end_byte = int(size * end_pct / 100) if end_pct < 100 else size
        if end_byte <= start_byte:
            continue
        candidates.append(ProgramCandidate(
            start_pct=start_pct, end_pct=end_pct,
            start_byte=start_byte, end_byte=end_byte, info=data,
        ))

    # 隣接する候補の信号が同じなら統合する(サンプリングの取りこぼし対策)
    merged: list[ProgramCandidate] = []
    for c in candidates:
        if merged and merged[-1].signature() == c.signature():
            merged[-1].end_pct = c.end_pct
            merged[-1].end_byte = c.end_byte
        else:
            merged.append(c)

    merged = _merge_dropout_gaps(merged, on_log)
    return merged


def _merge_dropout_gaps(candidates: list[ProgramCandidate],
                         on_log: Callable[[str], None]) -> list[ProgramCandidate]:
    """テープのドロップアウト等による瞬間的な欠測(情報取得できない短い区間)が
    同一番組の途中に挟まっているだけの場合、それを分割点とみなさず前後の
    番組候補に統合する。

    (前後で番組情報が一致している=分割の必要が無い、という前提。
     前後で番組情報が異なる場合はチャンネル切替等の本当の境目の可能性が
     あるためそのまま残す。)
    """
    changed = True
    while changed:
        changed = False
        for i in range(1, len(candidates) - 1):
            prev_c, cur_c, next_c = candidates[i - 1], candidates[i], candidates[i + 1]
            if not cur_c.info and prev_c.info and next_c.info and prev_c.signature() == next_c.signature():
                on_log(f"    ドロップアウト等による短い欠測区間({cur_c.start_pct}-{cur_c.end_pct}%)を"
                       f"前後と同一番組とみなして統合しました")
                prev_c.end_pct = next_c.end_pct
                prev_c.end_byte = next_c.end_byte
                del candidates[i:i + 2]
                changed = True
                break
    return candidates
