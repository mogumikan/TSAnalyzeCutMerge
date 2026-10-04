"""分割済み(TsSplitterによる粗い区切り)の1ファイルの中身をさらに解析し、
番組情報(タイトル/日時)が変化する地点を検出して「番組候補」の一覧を作る。

同一チャンネル・同一PMTのまま複数の番組が連続して録画されているケース
(TsSplitterのPMT基準分割では検出できない境界)に対応するための機能。

手法: rplsinfoの -F(0-99の位置指定)を使い、ファイルを等間隔にサンプリングして
タイトルが変化する区間を見つけ、隣接サンプル間で二分探索して境界を絞り込む。
実測(2026-09-04)で 267MBのファイルに対し25点サンプリング+絞り込みが1秒未満で
完了することを確認済み。

ドロップ(テープのドロップアウト・パケット欠損)への方針:
  番組の頭から終わりまでの間にドロップがあっても、同じ番組(日付・開始時刻・
  タイトルが一致)として1つの候補にまとめる。そのため
    * 番組情報を取得できなかったサンプル(ドロップ地点に当たった等)は「不明」として
      境界判定に使わず、取得できたサンプルだけで番組の切り替わりを判定する
    * 二分探索中に取得できない地点は近傍の位置で取り直す
    * 番組情報が化けて取れた(前後と食い違う)サンプルが1つ挟まっていても、その前後が
      同じ番組ならまとめて同一番組とみなす
    * ファイルの先頭/末尾側で取得できなかった区間は、隣接する番組に含める
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
                     on_log: Optional[Callable[[str], None]] = None,
                     tolerate_dropouts: bool = True,
                     merge_same_program: bool = True) -> list[ProgramCandidate]:
    """pathの中身を粗くサンプリングし、番組候補の区間リスト(pct単位)を返す。

    tolerate_dropouts: 番組情報を取得できなかったサンプルを「不明」として境界判定に使わず、
        二分探索で近傍を取り直し、先頭/末尾の取得できない区間を隣の番組に含める。
        Falseなら取得失敗も1つの状態として扱う従来動作(失敗地点が境界になりうる)。
    merge_same_program: 前後が同じ番組(日付・開始時刻・タイトル一致)なら、間に化けた
        情報等が挟まっていても1つの番組にまとめる。"""
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

    def get_near(pct: int, lo: int, hi: int) -> Optional[dict]:
        """pctで取得できなければ、(lo,hi)の範囲内で近傍(±1,±2)を取り直す
        (ドロップ地点に当たっただけの取得失敗を、境界と誤認しないため)。"""
        if not tolerate_dropouts:
            return get(pct)
        for off in (0, 1, -1, 2, -2):
            p = pct + off
            if off != 0 and not (lo < p < hi):
                continue
            data = get(p)
            if data:
                return data
        return None

    for p in pcts:
        get(p)
    on_log(f"    粗サンプリング {len(pcts)}点完了")

    # 取得できたサンプルだけで番組の切り替わりを判定する(取得できなかった=不明は
    # ドロップ等の可能性があるため境界とはみなさない)
    if tolerate_dropouts:
        known = [(p, _signature(cache[p])) for p in pcts if cache[p]]
    else:
        known = [(p, _signature(cache[p])) for p in pcts]
    n_unknown = sum(1 for p in pcts if not cache[p])
    if n_unknown and tolerate_dropouts:
        on_log(f"    番組情報を取得できなかったサンプル {n_unknown}点(ドロップ等の可能性。境界とはみなしません)")

    boundaries: list[int] = [0]
    for (a, sig_a), (b, sig_b) in zip(known, known[1:]):
        if sig_a == sig_b:
            continue
        lo, hi = a, b
        while hi - lo > min_gap_pct:
            mid = (lo + hi) // 2
            sig_mid = _signature(get_near(mid, lo, hi))
            if sig_mid == sig_a or (tolerate_dropouts and sig_mid is None):
                # 近傍でも取得できない地点は、前の番組側に含める(境界は取得できた側へ寄せる)
                lo = mid
            else:
                hi = mid
        if hi not in boundaries:
            boundaries.append(hi)
    boundaries.append(100)
    boundaries = sorted(set(boundaries))
    on_log(f"    境界候補: {boundaries}")

    def info_for(start_pct: int, end_pct: int) -> dict:
        """候補区間内で最初に取得できたサンプルの番組情報(先頭がドロップでも取れるように)。"""
        if not tolerate_dropouts:
            return get(min(99, start_pct)) or {}
        for p in sorted(k for k in cache if start_pct <= k < end_pct):
            if cache[p]:
                return cache[p] or {}
        for p in sorted((k for k in cache if k < start_pct), reverse=True):
            if cache[p]:
                return cache[p] or {}
        return {}

    candidates: list[ProgramCandidate] = []
    for start_pct, end_pct in zip(boundaries, boundaries[1:]):
        start_byte = int(size * start_pct / 100)
        end_byte = int(size * end_pct / 100) if end_pct < 100 else size
        if end_byte <= start_byte:
            continue
        candidates.append(ProgramCandidate(
            start_pct=start_pct, end_pct=end_pct,
            start_byte=start_byte, end_byte=end_byte, info=info_for(start_pct, end_pct),
        ))

    merged = _merge_same_program(candidates, on_log) if merge_same_program else _merge_adjacent_same(candidates)
    merged = _merge_dropout_gaps(merged, on_log)
    return merged


def _merge_adjacent_same(candidates: list[ProgramCandidate]) -> list[ProgramCandidate]:
    """隣り合う候補の番組情報が同じなら統合する(従来動作)。"""
    merged: list[ProgramCandidate] = []
    for c in candidates:
        if merged and merged[-1].signature() == c.signature():
            merged[-1].end_pct = c.end_pct
            merged[-1].end_byte = c.end_byte
        else:
            merged.append(c)
    return merged


def _merge_same_program(candidates: list[ProgramCandidate],
                         on_log: Callable[[str], None]) -> list[ProgramCandidate]:
    """日付・開始時刻・タイトルが同じ候補は同一番組として統合する。
    間に別の情報が挟まっていても(ドロップで番組情報が化けて取れた場合など)、
    その前後が同じ番組なら、間も含めて1つの番組にまとめる
    (同じ日付・開始時刻の番組が別番組を挟んで再登場することは無いため)。"""
    out: list[ProgramCandidate] = []
    i = 0
    while i < len(candidates):
        c = candidates[i]
        sig = c.signature()
        if sig != (None, None, None) and c.info:
            last = None
            for j in range(len(candidates) - 1, i, -1):
                if candidates[j].info and candidates[j].signature() == sig:
                    last = j
                    break
            if last is not None:
                span = candidates[i:last + 1]
                if len(span) > 1:
                    on_log(f"    同一番組の途中にドロップ等と思われる区間があるため、"
                           f"{c.start_pct}-{candidates[last].end_pct}%を1つの番組として統合しました"
                           f"(候補{len(span)}件→1件)")
                c.end_pct = candidates[last].end_pct
                c.end_byte = candidates[last].end_byte
                i = last + 1
                out.append(c)
                continue
        out.append(c)
        i += 1
    return out


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
