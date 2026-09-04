"""全体の処理フロー: 判定 -> 分割(TsSplitter) -> (詳細解析) -> 番組情報取得(rplsinfo) -> リネーム。

2つのモードを提供する:

  * 簡易モード (process_all):
      TsSplitterのPMT/EIT基準分割結果をそのまま最終ファイルとして採用する。
      高速だが、同一チャンネル・同一PMTのまま複数番組が連続している場合は
      分割されない。

  * 詳細解析モード (split_coarse -> analyze_coarse_segments -> extract_selected):
      TsSplitterで粗く分割した後、各ファイルをさらにサンプリング解析して
      番組候補(サムネイル・タイトル・日時つき)の一覧を作る。GUIでユーザーが
      候補を確認・選択してから、選択分だけをバイト単位でロスレス抽出する。
      (PEGASYS TMPGEnc MPEG Smart RendererのTS解析ウィザードに近いワークフロー)

方針:
  * 元データは一切変更・削除しない。出力は必ず別ファイル(別フォルダも可)に作成する。
  * 情報取得の成否に関わらずデータは保全する(失敗しても簡易名で保存)。
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Callable, Optional

from . import analyzer, mpegts, naming, overlap, probe, rplsinfo as rplsinfo_mod, thumbnail
from .analyzer import ProgramCandidate
from .splitter import SplitOptions, run_split


@dataclass
class PipelineOptions:
    tssplitter_exe: str
    rplsinfo_exe: str
    split_opts: SplitOptions
    output_root: Optional[str] = None      # Noneなら各入力ファイルと同じフォルダ
    per_file_subfolder: bool = True        # 入力ファイル名でサブフォルダを作る
    make_sidecar_info: bool = True         # 各出力に番組情報txtを添付する
    rename_pattern_ok: str = naming.DEFAULT_PATTERN_OK
    rename_pattern_ng: str = naming.DEFAULT_PATTERN_NG
    rplsinfo_position: Optional[int] = 50
    rplsinfo_sweep: bool = True
    rplsinfo_limit_mb: Optional[int] = 200
    convert_204: bool = True               # 204バイト(FEC)を検出したら188へ変換してから処理
    preserve_all_pmt_pids: bool = True     # データ放送等、PMT記載の全PIDを事前検出して保持する
    intermediate_handling: str = "subfolder"   # "subfolder"(_workへ移動) / "delete" / "keep"
    ffmpeg_exe: Optional[str] = None       # サムネイル生成用(Noneなら自動検出/スキップ)
    analysis_points: int = 25              # 詳細解析のサンプリング点数
    snap_to_keyframe: bool = True          # 解析で見つけた内部境界をGOP(キーフレーム)先頭に合わせる
    merge_detect_overlap: bool = True      # 結合時に重複バイナリデータを検出して除去する
    merge_pattern_mb: float = 1.0          # 重複検出に使う先頭パターンサイズ(MB)
    merge_search_window_mb: int = 256      # 重複検出で遡って探す範囲(MB)


@dataclass
class FileReport:
    src: str
    out_path: str
    ok_info: bool
    date: str = ""
    start_time: str = ""
    station: str = ""
    channel: str = ""
    title: str = ""
    note: str = ""


@dataclass
class PipelineResult:
    reports: list[FileReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary_csv: Optional[str] = None


@dataclass
class CoarseSegment:
    src: str
    out_dir: str
    orig_stem: str
    ext: str
    path: str
    suffix: str
    packet_size: int = 188
    candidates: list[ProgramCandidate] = field(default_factory=list)
    video_pid: Optional[int] = None
    _video_pid_probed: bool = False

    def get_video_pid(self) -> Optional[int]:
        if not self._video_pid_probed:
            sync_offset = 4 if self.packet_size == 192 else 0
            info = mpegts.get_stream_info(self.path, self.packet_size, sync_offset)
            self.video_pid = info.video_pid
            self._video_pid_probed = True
        return self.video_pid


LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int, int], None]   # (現在処理中のファイル名, cur, total, pct)


def _resolve_out_dir(src: str, opts: PipelineOptions) -> str:
    base = opts.output_root if opts.output_root else os.path.dirname(src)
    if opts.per_file_subfolder:
        stem = os.path.splitext(os.path.basename(src))[0]
        base = os.path.join(base, stem + "_split")
    return base


def _finalize_output(created_path: str, out_dir: str, orig_stem: str, suffix: str, ext: str,
                      opts: PipelineOptions, on_log: LogFn,
                      known_info: Optional[dict] = None) -> tuple[Optional[FileReport], Optional[str]]:
    """rplsinfoで番組情報を取得(または既知の情報を再確認)し、リネームして最終化する。"""
    if not os.path.exists(created_path):
        return None, f"想定した出力ファイルが見つかりません: {created_path}"

    size = os.path.getsize(created_path)
    on_log(f"  番組情報取得中: {os.path.basename(created_path)} ({probe.human_size(size)})")
    sidecar_tmp = created_path + ".info.txt" if opts.make_sidecar_info else None
    info = rplsinfo_mod.get_program_info(
        opts.rplsinfo_exe, created_path,
        position=opts.rplsinfo_position, sweep=opts.rplsinfo_sweep,
        limit_mb=opts.rplsinfo_limit_mb, sidecar_path=sidecar_tmp,
    )

    note = "非常に小さいセグメント(過渡的な断片の可能性)" if size < 200 * 1024 else ""

    if info.ok:
        render_info = dict(info.data)
        render_info["orig"] = orig_stem
        render_info["suffix"] = suffix
        render_info["ext"] = ext
        new_base = naming.render(opts.rename_pattern_ok, render_info) or f"{orig_stem}_{suffix}"
        on_log(f"    -> {info.message}: {info.data.get('date')} {info.data.get('start_time')} "
               f"{info.data.get('station')} 「{info.data.get('title')}」")
    elif known_info:
        # rplsinfoの再取得には失敗したが、解析フェーズで判明していた情報があれば使う
        render_info = dict(known_info)
        render_info["orig"] = orig_stem
        render_info["suffix"] = suffix
        render_info["ext"] = ext
        new_base = naming.render(opts.rename_pattern_ok, render_info) or f"{orig_stem}_{suffix}"
        on_log("    -> 再取得は失敗しましたが解析時点の情報でリネームします")
    else:
        render_info = {"orig": orig_stem, "suffix": suffix, "ext": ext}
        new_base = naming.render(opts.rename_pattern_ng, render_info) or f"{orig_stem}_{suffix}_情報不明"
        on_log(f"    -> 番組情報取得失敗 ({info.message})。ファイルはそのまま保持し、名前は簡易命名にします。")

    final_name = naming.unique_path(out_dir, new_base, ext, set())
    final_path = os.path.join(out_dir, final_name)
    try:
        if final_path != created_path:
            os.replace(created_path, final_path)
        if sidecar_tmp and os.path.exists(sidecar_tmp):
            final_sidecar = os.path.splitext(final_path)[0] + ".info.txt"
            if sidecar_tmp != final_sidecar:
                os.replace(sidecar_tmp, final_sidecar)
    except OSError as e:
        return None, f"{created_path}: リネーム失敗: {e}"

    used = info.data if info.ok else (known_info or {})
    report = FileReport(
        src=created_path, out_path=final_path, ok_info=info.ok or bool(known_info),
        date=used.get("date", ""), start_time=used.get("start_time", ""),
        station=used.get("station", ""), channel=used.get("channel", ""),
        title=used.get("title", ""), note=note,
    )
    return report, None


# --------------------------------------------------------------- 簡易モード
def process_one_source(src: str, opts: PipelineOptions,
                        on_log: LogFn, on_progress: ProgressFn) -> tuple[list[FileReport], list[str]]:
    reports: list[FileReport] = []
    errors: list[str] = []

    segs, errs = split_coarse(src, opts, on_log, on_progress)
    errors.extend(errs)

    for seg in segs:
        report, err = _finalize_output(seg.path, seg.out_dir, seg.orig_stem, seg.suffix, seg.ext, opts, on_log)
        if err:
            errors.append(err)
        if report:
            report.src = src
            reports.append(report)

    _cleanup_coarse_segments(segs, opts, on_log)
    return reports, errors


def process_all(files: list[str], opts: PipelineOptions,
                 on_log: LogFn, on_progress: ProgressFn,
                 should_cancel: Optional[Callable[[], bool]] = None) -> PipelineResult:
    result = PipelineResult()
    should_cancel = should_cancel or (lambda: False)

    for src in files:
        if should_cancel():
            on_log("キャンセルされました。")
            break
        try:
            reports, errors = process_one_source(src, opts, on_log, on_progress)
            result.reports.extend(reports)
            result.errors.extend(errors)
        except Exception as e:  # noqa: BLE001 - 1ファイルの失敗で全体を止めない
            result.errors.append(f"{src}: 予期しないエラー: {e}")
            on_log(f"  エラー: {e}")

    _write_summary(result, files, opts, on_log)
    return result


# ------------------------------------------------------------- 詳細解析モード
def split_coarse(src: str, opts: PipelineOptions,
                  on_log: LogFn, on_progress: ProgressFn) -> tuple[list[CoarseSegment], list[str]]:
    """TsSplitterでチャンネル/PMT単位に粗く分割する(詳細解析モードの第1段階)。"""
    errors: list[str] = []
    on_log(f"=== 入力: {src} ===")
    pr = probe.probe_file(src)
    on_log(f"  形式判定: {pr.detail}")

    work_src = src
    tmp_converted = None
    if pr.packet_size == 204 and opts.convert_204:
        tmp_converted = src + ".conv188.tmp"
        on_log("  204バイト(FEC)を検出。188バイトTSへ変換してから処理します。")
        try:
            probe.convert_204_to_188(src, tmp_converted)
            work_src = tmp_converted
        except OSError as e:
            errors.append(f"{src}: 204->188変換失敗: {e}")
            return [], errors
    elif pr.packet_size not in (188, 192, 204):
        on_log("  警告: パケットサイズを確定できませんでした。処理は続行しますが失敗する可能性があります。")

    out_dir = _resolve_out_dir(src, opts)
    on_log(f"  出力先: {out_dir}")

    # TsSplitterは映像/音声/PCR/EIT等の既知の種別以外のPMT記載PID
    # (データ放送のデータカルーセル等, stream_type 0x0D)を既定では保持しない。
    # ファイルを複数地点でサンプリングして実際に使われている全PIDを洗い出し、
    # -PIDで明示的に保持させることで、dデータ放送等も含め元データにできるだけ
    # 近い形で残す。
    effective_split_opts = opts.split_opts
    if opts.preserve_all_pmt_pids:
        sync_offset = 4 if pr.packet_size == 192 else 0
        try:
            found_pids = mpegts.scan_all_pmt_pids(work_src, pr.packet_size or 188, sync_offset)
        except OSError:
            found_pids = set()
        if found_pids:
            extra = {p.strip().lower().lstrip("0x") for p in opts.split_opts.extra_pids_hex if p.strip()}
            extra.update(format(p, "x") for p in found_pids)
            effective_split_opts = dataclass_replace(opts.split_opts, extra_pids_hex=sorted(extra))
            on_log(f"  PMT記載PIDを検出しすべて保持対象にします: "
                   + ",".join(f"0x{p:04x}" for p in sorted(found_pids)))

    def progress_cb(cur, total, pct):
        on_progress(os.path.basename(src), cur, total, pct)

    split_res = run_split(opts.tssplitter_exe, work_src, out_dir, effective_split_opts,
                           on_line=on_log, on_progress=progress_cb)

    if tmp_converted and os.path.exists(tmp_converted):
        try:
            os.remove(tmp_converted)
        except OSError:
            pass

    if not split_res.ok:
        errors.append(f"{src}: {split_res.message}")
        return [], errors

    on_log(f"  {split_res.message}")

    ext = os.path.splitext(src)[1].lstrip(".")
    orig_stem = os.path.splitext(os.path.basename(src))[0]
    segments: list[CoarseSegment] = []
    for created_path in split_res.created_files:
        if not os.path.isabs(created_path):
            created_path = os.path.join(out_dir, created_path)
        if not os.path.exists(created_path):
            on_log(f"  警告: 想定した出力ファイルが見つかりません: {created_path}")
            continue
        created_stem = os.path.splitext(os.path.basename(created_path))[0]
        suffix = created_stem[len(orig_stem) + 1:] if created_stem.startswith(orig_stem + "_") else created_stem
        seg_pr = probe.probe_file(created_path, sample_bytes=1024 * 1024)
        segments.append(CoarseSegment(
            src=src, out_dir=out_dir, orig_stem=orig_stem, ext=ext,
            path=created_path, suffix=suffix,
            packet_size=seg_pr.packet_size or 188,
        ))
    return segments, errors


def analyze_coarse_segments(segments: list[CoarseSegment], opts: PipelineOptions,
                             on_log: LogFn,
                             on_segment_done: Optional[Callable[[CoarseSegment], None]] = None) -> None:
    """各粗分割ファイルをサンプリング解析し、候補(candidates)を埋める。サムネイルも生成する。"""
    ffmpeg_exe = opts.ffmpeg_exe or thumbnail.find_ffmpeg()
    thumb_dir = None
    if ffmpeg_exe:
        thumb_dir = os.path.join(os.path.dirname(segments[0].out_dir) if segments else ".", ".tsautosplit_thumbs")
        try:
            os.makedirs(thumb_dir, exist_ok=True)
        except OSError:
            thumb_dir = None

    for seg in segments:
        on_log(f"  解析中: {os.path.basename(seg.path)}")
        seg.candidates = analyzer.analyze_segment(
            opts.rplsinfo_exe, seg.path, num_points=opts.analysis_points, on_log=on_log,
        )
        on_log(f"    -> 候補 {len(seg.candidates)} 件")

        if ffmpeg_exe and thumb_dir:
            for i, c in enumerate(seg.candidates):
                png = os.path.join(thumb_dir, f"{os.path.basename(seg.path)}_{i}.png")
                ok = thumbnail.make_thumbnail_from_range(
                    ffmpeg_exe, seg.path, c.start_byte, png, packet_size=seg.packet_size,
                )
                if ok:
                    c.thumbnail_path = png

        if on_segment_done:
            on_segment_done(seg)


def _snap_candidate_start(seg: CoarseSegment, c: ProgramCandidate, opts: PipelineOptions,
                           on_log: LogFn) -> None:
    """解析で見つけた内部の区切り(start_pct > 0)をIフレーム(キーフレーム)の
    先頭に合わせ、途中コマから始まらないようにする(c.start_byteを書き換える)。

    MPEG-2の公開仕様(ISO/IEC 13818-2)のシーケンスヘッダを解析するのみで、
    TMPGEnc等の内部実装は参照していない。セグメントの先頭(TsSplitterが確定
    したPMT境界, start_pct==0)はそのまま使う。
    """
    if not (opts.snap_to_keyframe and c.start_pct > 0):
        return
    vpid = seg.get_video_pid()
    if vpid is None:
        return
    sync_offset = 4 if seg.packet_size == 192 else 0
    snapped = mpegts.find_next_keyframe(seg.path, seg.packet_size, sync_offset, vpid, c.start_byte)
    if snapped is not None and snapped < c.end_byte and snapped != c.start_byte:
        on_log(f"    キーフレーム境界に位置合わせ: {c.start_byte:,} -> {snapped:,} バイト")
        c.start_byte = snapped


def extract_selected(segments: list[CoarseSegment], opts: PipelineOptions,
                      on_log: LogFn,
                      should_cancel: Optional[Callable[[], bool]] = None,
                      merge_by_station: bool = False) -> PipelineResult:
    """ユーザーが選択した候補をバイト単位で切り出し、番組情報を付けて最終化する。

    merge_by_station=True の場合、選択された候補を放送局名(番組情報が無い場合は
    元ファイル単位)でグループ化し、同じ局のものを元の時系列順に1本のファイルへ
    結合する(単純追記によるロスレス結合。放送局名が異なる項目同士は結合しない)。
    """
    result = PipelineResult()
    should_cancel = should_cancel or (lambda: False)

    if merge_by_station:
        _extract_merged_by_station(segments, opts, on_log, should_cancel, result)
    else:
        for seg in segments:
            selected = [c for c in seg.candidates if c.selected]
            if not selected:
                continue
            multi = len(seg.candidates) > 1
            for idx, c in enumerate(selected):
                if should_cancel():
                    on_log("キャンセルされました。")
                    break
                sub_suffix = f"{seg.suffix}-p{idx+1}" if multi else seg.suffix
                _extract_single_candidate(seg, c, sub_suffix, opts, on_log, result)

    if segments:
        files_for_summary = list({s.src for s in segments})
        _write_summary(result, files_for_summary, opts, on_log)
    _cleanup_thumbnails(segments)
    _cleanup_coarse_segments(segments, opts, on_log)

    return result


def _extract_single_candidate(seg: CoarseSegment, c: ProgramCandidate, sub_suffix: str,
                               opts: PipelineOptions, on_log: LogFn, result: PipelineResult) -> None:
    """1個の候補をそのまま(結合せず)切り出して最終化し、resultに追記する。"""
    tmp_name = f"{seg.orig_stem}_{sub_suffix}.{seg.ext}"
    tmp_path = os.path.join(seg.out_dir, tmp_name)
    n = 2
    while os.path.exists(tmp_path):
        tmp_path = os.path.join(seg.out_dir, f"{seg.orig_stem}_{sub_suffix}_{n}.{seg.ext}")
        n += 1

    _snap_candidate_start(seg, c, opts, on_log)

    on_log(f"  抽出中: {os.path.basename(tmp_path)} "
            f"({probe.human_size(c.size)}, {c.start_pct}-{c.end_pct}%)")
    try:
        written = probe.extract_byte_range(seg.path, tmp_path, c.start_byte, c.end_byte,
                                            packet_size=seg.packet_size)
    except OSError as e:
        result.errors.append(f"{seg.path}: 抽出失敗: {e}")
        return
    if written <= 0:
        result.errors.append(f"{seg.path}: 抽出データが空でした ({c.start_pct}-{c.end_pct}%)")
        return

    report, err = _finalize_output(tmp_path, seg.out_dir, seg.orig_stem, sub_suffix,
                                    seg.ext, opts, on_log, known_info=c.info or None)
    if err:
        result.errors.append(err)
    if report:
        report.src = seg.src
        result.reports.append(report)


def _cleanup_coarse_segments(segments: list[CoarseSegment], opts: PipelineOptions, on_log: LogFn) -> None:
    """詳細解析モードで使い終わったTsSplitterの粗分割ファイル(および.log/
    _tsselect.log)を片付ける。既定では削除せず"_work"サブフォルダへ移動する
    (元データではなく本ツールが作った中間ファイルなので、選択されなかった
    範囲のデータもこの中に残る)。
    """
    mode = opts.intermediate_handling
    if mode == "keep":
        return

    # (out_dir, orig_stem) ごとに1回だけ .log/_tsselect.log をまとめて処理する
    handled_stems: set = set()

    for seg in segments:
        paths_to_handle = []
        if os.path.exists(seg.path):
            paths_to_handle.append(seg.path)

        stem_key = (seg.out_dir, seg.orig_stem)
        if stem_key not in handled_stems:
            handled_stems.add(stem_key)
            for suffix in (".log", "_tsselect.log"):
                p = os.path.join(seg.out_dir, seg.orig_stem + suffix)
                if os.path.exists(p):
                    paths_to_handle.append(p)

        if not paths_to_handle:
            continue

        if mode == "delete":
            for p in paths_to_handle:
                try:
                    os.remove(p)
                except OSError as e:
                    on_log(f"  警告: 中間ファイル削除失敗: {p} ({e})")
            continue

        # 既定: "_work" サブフォルダへ移動
        work_dir = os.path.join(seg.out_dir, "_work")
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError as e:
            on_log(f"  警告: 作業用フォルダを作成できません: {work_dir} ({e})")
            continue
        for p in paths_to_handle:
            dst = os.path.join(work_dir, os.path.basename(p))
            n = 2
            while os.path.exists(dst):
                base, ext = os.path.splitext(os.path.basename(p))
                dst = os.path.join(work_dir, f"{base}_{n}{ext}")
                n += 1
            try:
                os.replace(p, dst)
            except OSError as e:
                on_log(f"  警告: 中間ファイル移動失敗: {p} ({e})")


def _cleanup_thumbnails(segments: list[CoarseSegment]) -> None:
    thumb_dirs = {os.path.dirname(c.thumbnail_path) for s in segments for c in s.candidates if c.thumbnail_path}
    for d in thumb_dirs:
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _extract_merged_by_station(segments: list[CoarseSegment], opts: PipelineOptions,
                                on_log: LogFn, should_cancel: Callable[[], bool],
                                result: PipelineResult) -> None:
    # 選択された候補を放送局名でグループ化する。レビュー画面で手動のグループ
    # 割り当て(group_override)が指定されていればそれを優先する。放送局が不明で
    # 手動割り当ても無い項目同士は「同じ番組」である根拠が無いため結合対象にせず、
    # 必ず個別の(サイズ1の)グループにする。
    groups: dict[str, list[tuple[CoarseSegment, ProgramCandidate]]] = {}
    order: list[str] = []
    unknown_seq = 0
    natural_index: dict[int, int] = {}
    idx_counter = 0
    for seg in segments:
        for c in seg.candidates:
            if not c.selected:
                continue
            natural_index[id(c)] = idx_counter
            idx_counter += 1
            station = c.group_override or (c.info.get("station") or "").strip()
            if not station:
                unknown_seq += 1
                station = f"__unknown__{unknown_seq}"
            if station not in groups:
                groups[station] = []
                order.append(station)
            groups[station].append((seg, c))

    if not groups:
        return

    # 手動の並び順指定(order_override)があるグループは、その順に並べ替える
    for station in order:
        items = groups[station]
        if any(c.order_override is not None for _, c in items):
            items.sort(key=lambda sc: (sc[1].order_override if sc[1].order_override is not None
                                        else natural_index[id(sc[1])]))

    out_root = segments[0].out_dir
    for station in order:
        if should_cancel():
            on_log("キャンセルされました。")
            break
        items = groups[station]
        seg0, c0 = items[0]

        if len(items) == 1:
            # 結合相手がいない(局不明、または同局の選択が1件だけ)場合は通常の
            # 個別抽出と同じ扱いにする(「まとめ1件」のような紛らわしい名前にしない)。
            multi = len(seg0.candidates) > 1
            idx = next((i for i, cc in enumerate(seg0.candidates) if cc is c0), 0)
            sub_suffix = f"{seg0.suffix}-p{idx+1}" if multi else seg0.suffix
            _extract_single_candidate(seg0, c0, sub_suffix, opts, on_log, result)
            continue

        ext = seg0.ext
        dates = sorted({c.info.get("date") for _, c in items if c.info.get("date")})
        date_part = dates[0] if dates else ""
        base = naming.sanitize(f"{date_part}_{station}_まとめ{len(items)}件".strip("_"), max_len=120)
        tmp_name = f"{base}.{ext}"
        tmp_path = os.path.join(out_root, tmp_name)
        n = 2
        while os.path.exists(tmp_path):
            tmp_path = os.path.join(out_root, f"{base}_{n}.{ext}")
            n += 1

        on_log(f"  結合抽出中: {os.path.basename(tmp_path)} ({len(items)}項目, 放送局: {station})")
        total_written = 0
        try:
            with open(tmp_path, "wb") as fout:
                for i, (seg, c) in enumerate(items):
                    _snap_candidate_start(seg, c, opts, on_log)
                    on_log(f"    + {os.path.basename(seg.path)} [{c.start_pct}-{c.end_pct}%] "
                           f"{c.info.get('date','')} {c.info.get('start_time','')} 「{c.info.get('title','')}」")
                    if i > 0 and opts.merge_detect_overlap:
                        total_written += overlap.append_with_overlap_check(
                            fout, seg.path, c.start_byte, c.end_byte, seg.packet_size,
                            opts.merge_pattern_mb, opts.merge_search_window_mb, on_log,
                        )
                    else:
                        total_written += probe.append_byte_range(
                            seg.path, fout, c.start_byte, c.end_byte, packet_size=seg.packet_size,
                        )
        except OSError as e:
            result.errors.append(f"{station}: 結合失敗: {e}")
            continue
        if total_written <= 0:
            result.errors.append(f"{station}: 結合データが空でした")
            continue

        on_log(f"    -> 結合完了 ({probe.human_size(total_written)})")
        on_log("    ※ 結合の継ぎ目では、プレーヤーによっては一瞬再生が乱れる場合があります"
               "(タイムスタンプが不連続になるため。詳細オプションの-PTSや、"
               "TSCutter.GUI等のタイムライン修復ツールの利用も検討してください)。")

        if opts.make_sidecar_info:
            sidecar_path = os.path.splitext(tmp_path)[0] + ".info.txt"
            try:
                with open(sidecar_path, "w", encoding="utf-8") as f:
                    f.write(f"放送局: {station}\n結合項目数: {len(items)}\n\n")
                    for seg, c in items:
                        f.write(f"{c.info.get('date','')} {c.info.get('start_time','')}  "
                                f"{c.info.get('title','') or '(情報不明)'}  "
                                f"[{os.path.basename(seg.path)} {c.start_pct}-{c.end_pct}%]\n")
            except OSError:
                pass

        result.reports.append(FileReport(
            src=seg0.src, out_path=tmp_path, ok_info=bool(c0.info),
            date=date_part, start_time=c0.info.get("start_time", ""),
            station=station, channel=c0.info.get("channel", ""),
            title=f"({len(items)}項目を結合)", note="",
        ))


def _write_summary(result: PipelineResult, files: list[str], opts: PipelineOptions, on_log: LogFn) -> None:
    if not result.reports or not files:
        return
    summary_dir = opts.output_root if opts.output_root else os.path.dirname(files[0])
    summary_path = os.path.join(summary_dir, f"split_summary_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    try:
        with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["元ファイル", "出力ファイル", "情報取得", "日付", "開始時刻",
                        "放送局", "チャンネル", "番組名", "備考"])
            for r in result.reports:
                w.writerow([r.src, r.out_path, "成功" if r.ok_info else "失敗",
                            r.date, r.start_time, r.station, r.channel, r.title, r.note])
        result.summary_csv = summary_path
        on_log(f"サマリーCSVを出力しました: {summary_path}")
    except OSError as e:
        result.errors.append(f"サマリーCSV書き込み失敗: {e}")
