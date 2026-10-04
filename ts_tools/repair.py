"""同一放送を別々に録画した(2つ以上の)ファイル群を比較し、主ファイルの
連続性カウンタ(continuity_counter)の欠番=ドロップ箇所を、他の健全な
データで補完する。

手法(MPEG-2 Systems, ISO/IEC 13818-1の公開仕様のみに基づく一般的な手法。
特定の商用/GPLソフトウェアの実装は参照していない):

  1. 全ファイルのPCR(Program Clock Reference)を記録する。PCRは放送局側が
     ストリームに埋め込む値なので、同じ放送を別々に受信・録画しても
     同じ瞬間には(ほぼ)同じPCR値が乗っている。これを複数ファイル間の
     時刻合わせの基準に使う。
  2. 主ファイル(A)側で対象PID(映像・音声等)のcontinuity_counterが
     連番から外れている箇所(ドロップ)を検出する。
  3. ドロップ箇所の前後のPCR値を、他の各ファイル(候補)側のPCR記録と
     突き合わせて対応するバイト範囲を求める。
  4. 候補を順番に試し、その箇所が健全な(ドロップが無い)最初の候補から
     対象PIDのパケットを取り出し、continuity_counterをAの続きから
     振り直したうえで、Aのドロップ箇所に挿入する。
  5. どの候補も同じ箇所にドロップがある場合は補完できないため、その旨を
     記録してAの欠損をそのまま残す(嘘のデータで埋めることはしない)。

完全な修復を保証するものではなく、あくまで「他のどれかに健全なデータが
あれば埋める」ベストエフォートの補完である。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import mpegts

LogFn = Callable[[str], None]


@dataclass
class RepairReport:
    pid: int
    gaps_found: int = 0
    gaps_patched: int = 0
    gaps_unpatchable: int = 0
    patched_packets: int = 0
    patched_by_source: dict = field(default_factory=dict)   # {ソースファイルパス: 補完件数}


def _extract_pid_packets_fh(f, packet_size: int, sync_offset: int, target_pid: int,
                             start_byte: int, end_byte: int) -> list[bytearray]:
    """_extract_pid_packetsと同じだが、既に開いているファイルオブジェクトfを使い回す
    (find_gap_candidatesのように同じファイルから極めて多数回・小範囲ずつ抽出する
    ケースで、1件ごとにopen()するオーバーヘッドを避けるため)。"""
    out = []
    for pos, pkt in mpegts._iter_packets_fh(f, packet_size, sync_offset, start_byte=start_byte,
                                             max_bytes=max(0, end_byte - start_byte)):
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pid != target_pid:
            continue
        afc = (pkt[3] >> 4) & 0x3
        if afc not in (1, 3):
            continue
        out.append(bytearray(pkt))
    return out


def _extract_pid_packets(path: str, packet_size: int, sync_offset: int, target_pid: int,
                          start_byte: int, end_byte: int) -> list[bytearray]:
    """[start_byte, end_byte) の範囲からtarget_pidのペイロード付きパケットのみを
    取り出す(188バイトに正規化して返す)。1回限りの呼び出し用(GUIのプレビュー等)。"""
    with open(path, "rb") as f:
        return _extract_pid_packets_fh(f, packet_size, sync_offset, target_pid, start_byte, end_byte)


def _renumber_cc(packets: list[bytearray], start_cc: int) -> int:
    """packets(同一PID・ペイロード持ち)のcontinuity_counterをstart_ccから
    連番で振り直す。振り直した最後のCC値を返す。"""
    cc = start_cc
    for pkt in packets:
        cc = (cc + 1) & 0x0F
        pkt[3] = (pkt[3] & 0xF0) | cc
    return cc


@dataclass
class GapCandidate:
    """1つのドロップ箇所と、その補完候補(見つかった場合)。GUIでのプレビュー
    (HEXを見ながら確認する)と、実際の適用(repair_pid)の両方で共有する。"""
    gap: "mpegts.CCGap"
    expected_count: int
    source_path: Optional[str] = None
    patch_packets: Optional[list] = None   # bytearrayのリスト(見つかった場合)
    used_pts: bool = False
    b_start: Optional[int] = None
    b_end: Optional[int] = None
    selected: bool = True   # GUIでユーザーがこの箇所の適用可否を切り替えるためのフラグ

    @property
    def patchable(self) -> bool:
        return self.patch_packets is not None and len(self.patch_packets) > 0


ProgressFn = Callable[[str, int, int], None]   # (ラベル, 現在バイト位置, 全体バイト数)


def _prepare_sources(secondary_paths: list[str], packet_size: int, sync_offset: int,
                      target_pid: int, pcr_pid: int, on_log: LogFn,
                      on_progress: Optional[ProgressFn] = None,
                      should_cancel: Optional[Callable[[], bool]] = None) -> list[dict]:
    """各secondaryを1回スキャンし、あわせてファイルを開いたまま保持する
    (sources[i]["fh"])。resolve_gap_candidateが大量に呼ばれる際、1件ごとに
    open()し直すオーバーヘッドを避けるため。呼び出し側はfinallyで
    close_sources()を呼ぶこと。"""
    sources = []
    try:
        for sec_path in secondary_paths:
            label = f"補完元解析中: {os.path.basename(sec_path)}"

            def _progress(cur, total, _label=label):
                if on_progress:
                    on_progress(_label, cur, total)

            scan = mpegts.scan_stream(sec_path, packet_size, sync_offset, target_pid, pcr_pid=pcr_pid,
                                       on_progress=_progress, should_cancel=should_cancel)
            if not scan.pcr_positions:
                on_log(f"    警告: {sec_path} のPCRが見つからないため、この候補は使用しません。")
                continue
            gap_ranges = [(g.pos_before, g.pos_after) for g in scan.gaps]
            sources.append({"path": sec_path, "pcr": scan.pcr_positions, "pts": scan.pts_positions,
                             "gap_ranges": gap_ranges, "fh": open(sec_path, "rb")})
    except BaseException:
        # 途中(キャンセル・エラー等)で抜ける場合も、既に開いたファイルは閉じておく
        _close_sources(sources)
        raise
    return sources


def _close_sources(sources: list[dict]) -> None:
    for src in sources:
        fh = src.get("fh")
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


def _map_via(table_a, table_b, pos_before, pos_after):
    """primary側のpos_before/pos_afterをtable_a(バイト位置->値)で値に変換し、
    table_b(候補側)で対応するバイト位置へ逆変換する。線形補間により、
    1回分のサンプル間隔よりずっと小さい欠損の位置も精密に特定できる。"""
    if not table_a or not table_b:
        return None, None
    v_before = mpegts.interpolate_value_at_position(table_a, pos_before)
    v_after = mpegts.interpolate_value_at_position(table_a, pos_after)
    if v_before is None or v_after is None:
        return None, None
    bs = mpegts.interpolate_position_at_value(table_b, v_before)
    be = mpegts.interpolate_position_at_value(table_b, v_after)
    return bs, be


def _has_gap_in(gap_ranges, start_byte: int, end_byte: int) -> bool:
    return any(not (be <= start_byte or bs >= end_byte) for bs, be in gap_ranges)


def resolve_gap_candidate(g, pts_a, pcr_a, sources: list[dict],
                           packet_size: int, sync_offset: int, target_pid: int) -> GapCandidate:
    """1つのドロップ箇所(g)について、sources(secondary候補、優先順)を順番に
    試し、最初に見つかった健全な補完データをGapCandidateとして返す
    (見つからなければpatch_packets=Noneのまま)。ファイルへの書き込みは行わない
    (プレビュー・適用の両方から呼べる純粋な判定関数)。"""
    expected_count = (g.cc_after - g.cc_before - 1) & 0x0F
    cand = GapCandidate(gap=g, expected_count=expected_count)
    for src in sources:
        b_start, b_end = _map_via(pts_a, src["pts"], g.pos_before, g.pos_after)
        used_pts = b_start is not None and b_end is not None and b_end > b_start
        if not used_pts:
            b_start, b_end = _map_via(pcr_a, src["pcr"], g.pos_before, g.pos_after)
        if b_start is None or b_end is None or b_end <= b_start:
            continue
        if _has_gap_in(src["gap_ranges"], b_start, b_end):
            continue
        fh = src.get("fh")
        if fh is not None:
            patch_packets = _extract_pid_packets_fh(fh, packet_size, sync_offset,
                                                     target_pid, b_start, b_end)
        else:
            patch_packets = _extract_pid_packets(src["path"], packet_size, sync_offset,
                                                  target_pid, b_start, b_end)
        if not patch_packets:
            continue
        if len(patch_packets) > expected_count:
            patch_packets = patch_packets[:expected_count]
        cand.source_path = src["path"]
        cand.patch_packets = patch_packets
        cand.used_pts = used_pts
        cand.b_start, cand.b_end = b_start, b_end
        break
    return cand


def find_gap_candidates(primary_path: str, secondary_paths: list[str],
                         packet_size: int, sync_offset: int, target_pid: int, pcr_pid: int,
                         on_log: Optional[LogFn] = None,
                         on_progress: Optional[ProgressFn] = None,
                         should_cancel: Optional[Callable[[], bool]] = None) -> list[GapCandidate]:
    """ファイルを一切変更せず、ドロップ箇所と各々の補完候補の一覧を返す
    (GUIでHEXを見ながら確認するためのプレビュー用)。"""
    on_log = on_log or (lambda s: None)
    label = f"主ファイル解析中: {os.path.basename(primary_path)}"

    def _progress(cur, total, _label=label):
        if on_progress:
            on_progress(_label, cur, total)

    scan_a = mpegts.scan_stream(primary_path, packet_size, sync_offset, target_pid, pcr_pid=pcr_pid,
                                 on_progress=_progress, should_cancel=should_cancel)
    if not scan_a.pcr_positions:
        on_log("    primary側のPCRが見つからないため検出できません。")
        return []
    if not scan_a.gaps:
        return []
    sources = _prepare_sources(secondary_paths, packet_size, sync_offset, target_pid, pcr_pid, on_log,
                                on_progress=on_progress, should_cancel=should_cancel)
    try:
        total_gaps = len(scan_a.gaps)
        label2 = f"補完候補を検索中: {os.path.basename(primary_path)} ({total_gaps}件のドロップ)"
        results = []
        for i, g in enumerate(scan_a.gaps):
            if should_cancel is not None and should_cancel():
                raise mpegts.ScanCancelled()
            results.append(resolve_gap_candidate(g, scan_a.pts_positions, scan_a.pcr_positions, sources,
                                                   packet_size, sync_offset, target_pid))
            # ドロップが数万件規模になる巨大ファイルでも、GUIが「止まって見える」ことの
            # ないよう一定件数ごとに進捗を報告する(以前はこのループに進捗報告が無く、
            # 実データ(18GB超のファイル)で「検出中のまま動かない」ように見えるバグがあった)。
            if on_progress is not None and (i % 20 == 0 or i == total_gaps - 1):
                on_progress(label2, i + 1, total_gaps)
        return results
    finally:
        _close_sources(sources)


def repair_pid(primary_path: str, secondary_paths: list[str], out_path: str,
                packet_size: int, sync_offset: int, target_pid: int, pcr_pid: int,
                on_log: Optional[LogFn] = None,
                candidates: Optional[list[GapCandidate]] = None,
                on_progress: Optional[ProgressFn] = None,
                should_cancel: Optional[Callable[[], bool]] = None) -> RepairReport:
    """primary_pathをベースに、target_pidのドロップ箇所をsecondary_paths
    (1つ以上)のデータで補完してout_pathへ書き出す(他のPIDはprimaryのまま)。

    各ドロップ箇所について、secondary_pathsを順番に試し、その箇所が健全な
    最初の候補のデータを採用する。candidatesを渡すと(GUIでプレビュー済みの
    一覧を編集した場合など)、それをそのまま適用する(再検出しない)。
    candidates中の要素のうちgap.selectedがFalseのもの、またはpatch_packetsが
    Noneのものは適用しない。

    should_cancel()がTrueを返すと mpegts.ScanCancelled を送出して中断する
    (out_pathへの書き込みはまだ行われていない状態で中断される)。"""
    on_log = on_log or (lambda s: None)
    report = RepairReport(pid=target_pid)

    if candidates is None:
        candidates = find_gap_candidates(primary_path, secondary_paths, packet_size, sync_offset,
                                          target_pid, pcr_pid, on_log=on_log,
                                          on_progress=on_progress, should_cancel=should_cancel)
    report.gaps_found = len(candidates)
    if not candidates:
        on_log("    ドロップは検出されませんでした。")
        import shutil
        shutil.copyfile(primary_path, out_path)
        return report

    n_pts_used = 0
    n_pcr_used = 0

    # 挿入計画を作る: primaryのpos_before(ドロップ直前の位置)ごとに、挿入するパケット列を用意
    insertions: dict[int, list[bytearray]] = {}
    for cand in candidates:
        if not cand.patchable or not cand.selected:
            report.gaps_unpatchable += 1
            continue
        g = cand.gap
        patch_packets = cand.patch_packets
        _renumber_cc(patch_packets, g.cc_before)
        # pos_before(ドロップ直前の正常パケット)の直後に挿入する。pos_afterに
        # 挿入すると「Aが再開した後に挿入」という順序になってしまうバグがあった
        # (実機データでの検証中に判明): 継ぎ目のCCが大きく食い違う原因だった。
        insertions[g.pos_before] = patch_packets
        report.gaps_patched += 1
        report.patched_packets += len(patch_packets)
        report.patched_by_source[cand.source_path] = report.patched_by_source.get(cand.source_path, 0) + 1
        if cand.used_pts:
            n_pts_used += 1
        else:
            n_pcr_used += 1
        on_log(f"    -> 補完: {len(patch_packets)}パケットを"
               f"{os.path.basename(cand.source_path)}から挿入")

    on_log(f"    対応付け方式の内訳: PTS(精密) {n_pts_used}件 / PCR(粗) {n_pcr_used}件")
    if report.patched_by_source:
        breakdown = ", ".join(f"{os.path.basename(p)}: {n}件" for p, n in report.patched_by_source.items())
        on_log(f"    補完元の内訳: {breakdown}")
    on_log(f"    出力ファイルを作成中...")
    write_label = f"書き出し中: {os.path.basename(out_path)}"
    total_size = None
    if on_progress is not None:
        try:
            total_size = os.path.getsize(primary_path)
        except OSError:
            total_size = None
    with open(primary_path, "rb") as fin, open(out_path, "wb") as fout:
        pos = 0
        chunk_size = 8 * 1024 * 1024
        chunk_size = (chunk_size // packet_size) * packet_size
        while True:
            if should_cancel is not None and should_cancel():
                raise mpegts.ScanCancelled()
            if on_progress is not None and total_size:
                on_progress(write_label, pos, total_size)
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            n_pkts = len(chunk) // packet_size
            for i in range(n_pkts):
                p = pos + i * packet_size
                fout.write(chunk[i * packet_size:(i + 1) * packet_size])
                if p in insertions:
                    for ins_pkt in insertions[p]:
                        if packet_size == 192:
                            fout.write(b"\x00\x00\x00\x00" + bytes(ins_pkt))
                        else:
                            fout.write(bytes(ins_pkt))
            pos += n_pkts * packet_size
    if on_progress is not None and total_size:
        on_progress(write_label, total_size, total_size)

    on_log(f"    完了: {report.gaps_patched}/{report.gaps_found}箇所を補完"
           f"({report.patched_packets}パケット), 補完不可 {report.gaps_unpatchable}箇所")
    return report


def repair_multi(primary_path: str, secondary_paths: list[str], out_path: str,
                  packet_size: int, sync_offset: int, target_pids: list[int], pcr_pid: int,
                  on_log: Optional[LogFn] = None,
                  on_progress: Optional[ProgressFn] = None,
                  should_cancel: Optional[Callable[[], bool]] = None) -> list[RepairReport]:
    """複数のPIDを順番に補完する(1PIDずつrepair_pidを実行し、前段の出力を
    次段の入力にする)。secondary_pathsは1つ以上の補完元候補のリスト。

    should_cancel()がTrueになった時点でmpegts.ScanCancelledが送出される
    (途中のPIDまでの一時ファイルは片付けてから中断する)。"""
    on_log = on_log or (lambda s: None)
    reports: list[RepairReport] = []

    import tempfile
    import uuid

    cur_primary = primary_path
    tmp_files: list[str] = []
    try:
        for i, pid in enumerate(target_pids):
            is_last = (i == len(target_pids) - 1)
            step_out = out_path if is_last else os.path.join(
                tempfile.gettempdir(), f"repair_step_{uuid.uuid4().hex}.tmp")
            on_log(f"  [{i + 1}/{len(target_pids)}] PID 0x{pid:04x} を補完中...")

            def _progress(label, cur, total, _i=i, _n=len(target_pids)):
                if on_progress:
                    on_progress(f"[{_i + 1}/{_n}] {label}", cur, total)

            report = repair_pid(cur_primary, secondary_paths, step_out,
                                 packet_size, sync_offset, pid, pcr_pid, on_log=on_log,
                                 on_progress=_progress, should_cancel=should_cancel)
            reports.append(report)
            if cur_primary != primary_path:
                tmp_files.append(cur_primary)
            cur_primary = step_out
    finally:
        for p in tmp_files:
            try:
                os.remove(p)
            except OSError:
                pass

    return reports
