#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TSAnalyzeCutMerge GUI

TsSplitter + rplsinfo を組み合わせ、TS/M2TSファイルから
番組情報(局名・番組名・放送日時)を読み取りながら自動分割し、
分かりやすいファイル名で保存するツール。

対応:
  - 拡張子: .ts / .m2ts
  - パケットサイズ: 188バイト / 192バイト (204バイトFECは自動変換して対応)
  - 1本のTSに複数番組・複数局・日付またぎのデータが混在する場合の分割
    (D-VHS等でキャプチャしたパーシャルTSを主な対象として検証済み)
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ts_tools import config, naming, pipeline, thumbnail
from ts_tools.pipeline import CoarseSegment, PipelineOptions, process_all
from ts_tools.review_ui import ReviewWindow
from ts_tools.splitter import SPLIT_MODES, SplitOptions

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

APP_TITLE = "TSAnalyzeCutMerge - TS番組情報自動分割・結合ツール"

_BaseTk = TkinterDnD.Tk if DND_AVAILABLE else tk.Tk


class App(_BaseTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x760")

        self.files: list[str] = []
        self.log_queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = False

        self._build_ui()
        self._load_settings()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        main_tab = ttk.Frame(nb)
        opt_tab = ttk.Frame(nb)
        nb.add(main_tab, text="ファイル / 実行")
        nb.add(opt_tab, text="詳細オプション")

        self._build_main_tab(main_tab)
        self._build_option_tab(opt_tab)

    def _build_main_tab(self, parent):
        drop_hint = "(ここにファイル/フォルダをドラッグ&ドロップできます)" if DND_AVAILABLE else \
                    "(tkinterdnd2未導入のためドラッグ&ドロップは無効です)"
        frm_files = ttk.LabelFrame(parent, text=f"入力ファイル (.ts / .m2ts) {drop_hint}")
        frm_files.pack(fill="both", expand=False, padx=4, pady=4)

        self.listbox = tk.Listbox(frm_files, height=8, selectmode="extended")
        self.listbox.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        sb = ttk.Scrollbar(frm_files, command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        btns = ttk.Frame(frm_files)
        btns.pack(side="left", fill="y", padx=4, pady=4)
        ttk.Button(btns, text="ファイル追加...", command=self._add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="フォルダ追加...", command=self._add_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="選択を削除", command=self._remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="全て削除", command=self._clear_files).pack(fill="x", pady=2)

        if DND_AVAILABLE:
            for widget in (frm_files, self.listbox):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop_files)

        frm_out = ttk.LabelFrame(parent, text="出力先")
        frm_out.pack(fill="x", padx=4, pady=4)
        self.output_mode = tk.StringVar(value="subfolder")
        ttk.Radiobutton(frm_out, text="各入力ファイルと同じ場所に「元ファイル名_split」フォルダを作る(推奨)",
                         variable=self.output_mode, value="subfolder",
                         command=self._update_output_state).pack(anchor="w")
        row = ttk.Frame(frm_out)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="共通の出力フォルダを指定:", variable=self.output_mode,
                         value="common", command=self._update_output_state).pack(side="left")
        self.output_dir_var = tk.StringVar()
        self.output_dir_entry = ttk.Entry(row, textvariable=self.output_dir_var, state="disabled")
        self.output_dir_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.output_dir_btn = ttk.Button(row, text="参照...", command=self._choose_output_dir, state="disabled")
        self.output_dir_btn.pack(side="left")

        row_im = ttk.Frame(frm_out)
        row_im.pack(fill="x", pady=(4, 0))
        ttk.Label(row_im, text="使い終わった中間ファイル(粗分割の元ファイル・ログ):").pack(side="left")
        self.intermediate_var = tk.StringVar(value="subfolder")
        ttk.Radiobutton(row_im, text="_workフォルダへ移動(推奨)", variable=self.intermediate_var,
                         value="subfolder").pack(side="left")
        ttk.Radiobutton(row_im, text="削除する", variable=self.intermediate_var,
                         value="delete").pack(side="left")
        ttk.Radiobutton(row_im, text="そのまま残す", variable=self.intermediate_var,
                         value="keep").pack(side="left")

        self.confirm_before_start = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_out, text="実行前に出力先を確認する",
                         variable=self.confirm_before_start).pack(anchor="w", pady=(4, 0))

        frm_mode = ttk.LabelFrame(parent, text="動作モード")
        frm_mode.pack(fill="x", padx=4, pady=4)
        self.detailed_analysis = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm_mode,
            text="詳細解析して番組ごとに候補(サムネイル付き)を確認してから抽出する(推奨)",
            variable=self.detailed_analysis,
        ).pack(anchor="w")
        ttk.Label(
            frm_mode,
            text="※ オフにすると、TsSplitterのチャンネル/PMT単位の分割結果をそのまま採用する簡易・高速モードになります"
                 "(同一チャンネルで番組だけが切り替わる境目は検出されません)。",
            foreground="#555", wraplength=880,
        ).pack(anchor="w", padx=4)

        frm_run = ttk.LabelFrame(parent, text="実行")
        frm_run.pack(fill="x", padx=4, pady=4)
        row2 = ttk.Frame(frm_run)
        row2.pack(fill="x", pady=2)
        self.start_btn = ttk.Button(row2, text="分割開始", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(row2, text="キャンセル", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        self.file_progress_label = ttk.Label(frm_run, text="待機中")
        self.file_progress_label.pack(anchor="w", padx=4)
        self.file_progress = ttk.Progressbar(frm_run, mode="determinate", maximum=100)
        self.file_progress.pack(fill="x", padx=4, pady=(0, 4))

        frm_log = ttk.LabelFrame(parent, text="ログ")
        frm_log.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_text = scrolledtext.ScrolledText(frm_log, height=16, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)
        ttk.Button(frm_log, text="ログを保存...", command=self._save_log).pack(anchor="e", pady=2)

    def _build_option_tab(self, parent):
        # 分割モード
        frm_mode = ttk.LabelFrame(parent, text="分割モード")
        frm_mode.pack(fill="x", padx=4, pady=4)
        self.split_mode_var = tk.StringVar(value="SEP2")
        for key in ("SEP2", "SEP3", "SEP", "NONE"):
            ttk.Radiobutton(frm_mode, text=f"{key}: {SPLIT_MODES[key]}",
                             variable=self.split_mode_var, value=key).pack(anchor="w")

        # 保持オプション
        frm_keep = ttk.LabelFrame(parent, text="保持するデータ(元データにできるだけ近づける)")
        frm_keep.pack(fill="x", padx=4, pady=4)
        self.keep_eit = tk.BooleanVar(value=True)
        self.keep_ecm = tk.BooleanVar(value=True)
        self.keep_emm = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_keep, text="EIT等(番組情報/NIT〜TOT)を保持", variable=self.keep_eit).pack(anchor="w")
        ttk.Checkbutton(frm_keep, text="ECMを保持", variable=self.keep_ecm).pack(anchor="w")
        ttk.Checkbutton(frm_keep, text="EMMを保持", variable=self.keep_emm).pack(anchor="w")
        self.preserve_all_pids = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_keep, text="PMT記載の全PIDを自動検出して保持する"
                                        "(dデータ放送のデータカルーセル等、既定でTsSplitterが削除する"
                                        "データも保持・推奨)",
                         variable=self.preserve_all_pids).pack(anchor="w")
        ttk.Label(frm_keep, text="※ パーシャルTSの番組情報PID(0x1F: SIT)は常に自動で保持されます",
                  foreground="#555").pack(anchor="w", padx=4)

        row = ttk.Frame(frm_keep)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="追加で保持するPID(16進数,カンマ区切り 例: 30,31):").pack(side="left")
        self.extra_pids_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.extra_pids_var, width=20).pack(side="left", padx=4)

        # 出力する画質種別
        frm_quality = ttk.LabelFrame(parent, text="出力する種別")
        frm_quality.pack(fill="x", padx=4, pady=4)
        self.out_hd = tk.BooleanVar(value=True)
        self.out_sd = tk.BooleanVar(value=True)
        self.out_1seg = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_quality, text="HD", variable=self.out_hd).pack(side="left", padx=8)
        ttk.Checkbutton(frm_quality, text="SD (SD1-3)", variable=self.out_sd).pack(side="left", padx=8)
        ttk.Checkbutton(frm_quality, text="1SEG", variable=self.out_1seg).pack(side="left", padx=8)
        self.cs_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_quality, text="CS放送として処理する(-CS)", variable=self.cs_mode).pack(side="left", padx=8)

        frm_pts = ttk.LabelFrame(parent, text="タイムライン(PTS/PCR)")
        frm_pts.pack(fill="x", padx=4, pady=4)
        self.reset_pts = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_pts, text="PTS/PCRを1:00:00基準に振り直す(-PTS)。一部プレーヤーで再生できなくなる場合があるので注意",
                         variable=self.reset_pts).pack(anchor="w")
        ttk.Label(frm_pts, text="※ 複数のD-VHSダビング元を1本にマージ/タイムライン修復したい場合は、"
                                 "TSCutter.GUI (https://github.com/nilaoda/TSCutter.GUI) 等の専用ツールの併用も検討してください。",
                  foreground="#555", wraplength=880).pack(anchor="w", padx=4)

        frm_analysis = ttk.LabelFrame(parent, text="詳細解析の設定")
        frm_analysis.pack(fill="x", padx=4, pady=4)
        row_a = ttk.Frame(frm_analysis)
        row_a.pack(fill="x", pady=2)
        ttk.Label(row_a, text="サンプリング点数(多いほど精密・低速):").pack(side="left")
        self.analysis_points_var = tk.IntVar(value=25)
        ttk.Spinbox(row_a, from_=10, to=100, textvariable=self.analysis_points_var, width=6).pack(side="left", padx=4)
        self.thumbnail_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_a, text="サムネイルを生成する(ffmpegが必要)", variable=self.thumbnail_enabled).pack(side="left", padx=12)
        self.snap_keyframe_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_analysis, text="解析で検出した区切りをキーフレーム(GOP先頭)に位置合わせする(推奨)",
                         variable=self.snap_keyframe_var).pack(anchor="w")

        # rplsinfo設定
        frm_info = ttk.LabelFrame(parent, text="番組情報の取得 (rplsinfo)")
        frm_info.pack(fill="x", padx=4, pady=4)
        self.make_sidecar = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_info, text="各出力ファイルに番組情報テキスト(.info.txt)を添付する",
                         variable=self.make_sidecar).pack(anchor="w")
        row2 = ttk.Frame(frm_info)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="探索開始位置(0-99, 既定50):").pack(side="left")
        self.rpls_pos_var = tk.IntVar(value=50)
        ttk.Spinbox(row2, from_=0, to=99, textvariable=self.rpls_pos_var, width=6).pack(side="left", padx=4)
        self.rpls_sweep = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="失敗時に他の位置も自動的に試す", variable=self.rpls_sweep).pack(side="left", padx=8)
        row3 = ttk.Frame(frm_info)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="探索リミット(MB, 巨大ファイルでの時間短縮用):").pack(side="left")
        self.rpls_limit_var = tk.IntVar(value=200)
        ttk.Spinbox(row3, from_=10, to=5000, increment=10, textvariable=self.rpls_limit_var, width=8).pack(side="left", padx=4)

        # リネームパターン
        frm_name = ttk.LabelFrame(parent, text="リネームパターン")
        frm_name.pack(fill="x", padx=4, pady=4)
        ttk.Label(frm_name, text="使えるトークン: {date} {time} {station} {channel} {title} {orig} {suffix} {ext}",
                  foreground="#555").pack(anchor="w")
        row4 = ttk.Frame(frm_name)
        row4.pack(fill="x", pady=2)
        ttk.Label(row4, text="情報取得成功時:").pack(side="left")
        self.pattern_ok_var = tk.StringVar(value=naming.DEFAULT_PATTERN_OK)
        ttk.Entry(row4, textvariable=self.pattern_ok_var).pack(side="left", fill="x", expand=True, padx=4)
        row5 = ttk.Frame(frm_name)
        row5.pack(fill="x", pady=2)
        ttk.Label(row5, text="情報取得失敗時:").pack(side="left")
        self.pattern_ng_var = tk.StringVar(value=naming.DEFAULT_PATTERN_NG)
        ttk.Entry(row5, textvariable=self.pattern_ng_var).pack(side="left", fill="x", expand=True, padx=4)

        # ツールパス
        frm_tools = ttk.LabelFrame(parent, text="外部ツールのパス")
        frm_tools.pack(fill="x", padx=4, pady=4)
        row6 = ttk.Frame(frm_tools)
        row6.pack(fill="x", pady=2)
        ttk.Label(row6, text="TsSplitter.exe:", width=16).pack(side="left")
        self.tssplitter_var = tk.StringVar(value=config.DEFAULT_TSSPLITTER_EXE)
        ttk.Entry(row6, textvariable=self.tssplitter_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row6, text="参照...", command=lambda: self._choose_exe(self.tssplitter_var)).pack(side="left")
        row7 = ttk.Frame(frm_tools)
        row7.pack(fill="x", pady=2)
        ttk.Label(row7, text="rplsinfo.exe:", width=16).pack(side="left")
        self.rplsinfo_var = tk.StringVar(value=config.DEFAULT_RPLSINFO_EXE)
        ttk.Entry(row7, textvariable=self.rplsinfo_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row7, text="参照...", command=lambda: self._choose_exe(self.rplsinfo_var)).pack(side="left")

    # ------------------------------------------------------------- helpers
    def _choose_exe(self, var: tk.StringVar):
        p = filedialog.askopenfilename(title="実行ファイルを選択", filetypes=[("実行ファイル", "*.exe")])
        if p:
            var.set(p)

    def _update_output_state(self):
        state = "normal" if self.output_mode.get() == "common" else "disabled"
        self.output_dir_entry.config(state=state)
        self.output_dir_btn.config(state=state)

    def _choose_output_dir(self):
        p = filedialog.askdirectory(title="出力フォルダを選択")
        if p:
            self.output_dir_var.set(p)

    def _add_file_path(self, path: str) -> bool:
        """1個のファイルパスを追加する。対象拡張子でなければFalseを返す。"""
        if not path.lower().endswith(config.SUPPORTED_EXTS):
            return False
        if path not in self.files:
            self.files.append(path)
            self.listbox.insert("end", path)
        return True

    def _add_folder_path(self, folder: str) -> int:
        """フォルダ直下の対象ファイルをすべて追加し、追加件数を返す。"""
        added = 0
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            return 0
        for fn in entries:
            full = os.path.join(folder, fn)
            if os.path.isfile(full) and self._add_file_path(full):
                added += 1
        return added

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="TS/M2TSファイルを選択",
            filetypes=[("TS/M2TS", "*.ts *.m2ts"), ("すべてのファイル", "*.*")],
        )
        for p in paths:
            self._add_file_path(p)

    def _add_folder(self):
        d = filedialog.askdirectory(title="フォルダを選択(直下の.ts/.m2tsを追加します)")
        if not d:
            return
        if self._add_folder_path(d) == 0:
            messagebox.showinfo(APP_TITLE, "対象ファイル(.ts/.m2ts)が見つかりませんでした。")

    def _on_drop_files(self, event):
        paths = self.tk.splitlist(event.data)
        added = 0
        skipped = 0
        for p in paths:
            if os.path.isdir(p):
                added += self._add_folder_path(p)
            elif os.path.isfile(p):
                if self._add_file_path(p):
                    added += 1
                else:
                    skipped += 1
        if added == 0 and skipped > 0:
            messagebox.showinfo(APP_TITLE, "対象ファイル(.ts/.m2ts)ではないため追加されませんでした。")
        elif skipped > 0:
            self._append_log(f"(ドラッグ&ドロップ: 対象外の拡張子のため{skipped}件をスキップしました)")

    def _remove_selected(self):
        for idx in reversed(self.listbox.curselection()):
            del self.files[idx]
            self.listbox.delete(idx)

    def _clear_files(self):
        self.files.clear()
        self.listbox.delete(0, "end")

    def _append_log(self, text: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _save_log(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("テキスト", "*.txt")])
        if not p:
            return
        content = self.log_text.get("1.0", "end")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    # -------------------------------------------------------------- run
    def _build_options(self) -> PipelineOptions:
        extra_pids = [x for x in self.extra_pids_var.get().split(",") if x.strip()]
        split_opts = SplitOptions(
            split_mode=self.split_mode_var.get(),
            keep_eit=self.keep_eit.get(),
            keep_ecm=self.keep_ecm.get(),
            keep_emm=self.keep_emm.get(),
            out_hd=self.out_hd.get(),
            out_sd=self.out_sd.get(),
            out_1seg=self.out_1seg.get(),
            cs_mode=self.cs_mode.get(),
            extra_pids_hex=extra_pids,
            reset_pts=self.reset_pts.get(),
        )
        output_root = self.output_dir_var.get() if self.output_mode.get() == "common" else None
        ffmpeg_exe = thumbnail.find_ffmpeg() if self.thumbnail_enabled.get() else None
        return PipelineOptions(
            tssplitter_exe=self.tssplitter_var.get(),
            rplsinfo_exe=self.rplsinfo_var.get(),
            split_opts=split_opts,
            output_root=output_root,
            per_file_subfolder=(self.output_mode.get() == "subfolder"),
            make_sidecar_info=self.make_sidecar.get(),
            rename_pattern_ok=self.pattern_ok_var.get() or naming.DEFAULT_PATTERN_OK,
            rename_pattern_ng=self.pattern_ng_var.get() or naming.DEFAULT_PATTERN_NG,
            rplsinfo_position=self.rpls_pos_var.get(),
            rplsinfo_sweep=self.rpls_sweep.get(),
            rplsinfo_limit_mb=self.rpls_limit_var.get(),
            ffmpeg_exe=ffmpeg_exe,
            analysis_points=self.analysis_points_var.get(),
            snap_to_keyframe=self.snap_keyframe_var.get(),
            preserve_all_pmt_pids=self.preserve_all_pids.get(),
            intermediate_handling=self.intermediate_var.get(),
        )

    def _start(self):
        if not self.files:
            messagebox.showwarning(APP_TITLE, "入力ファイルを追加してください。")
            return
        if not os.path.exists(self.tssplitter_var.get()):
            messagebox.showerror(APP_TITLE, "TsSplitter.exeが見つかりません。パスを確認してください。")
            return
        if not os.path.exists(self.rplsinfo_var.get()):
            messagebox.showerror(APP_TITLE, "rplsinfo.exeが見つかりません。パスを確認してください。")
            return
        if self.output_mode.get() == "common" and not self.output_dir_var.get():
            messagebox.showwarning(APP_TITLE, "共通出力フォルダを指定してください。")
            return

        self._save_settings()
        opts = self._build_options()
        files = list(self.files)

        if self.confirm_before_start.get():
            preview_lines = []
            for f in files[:8]:
                out_dir = opts.output_root if opts.output_root else os.path.dirname(f)
                if opts.per_file_subfolder:
                    out_dir = os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + "_split")
                preview_lines.append(f"{os.path.basename(f)}\n  -> {out_dir}")
            if len(files) > 8:
                preview_lines.append(f"...他 {len(files) - 8} ファイル")
            msg = "以下の出力先で処理を開始します。よろしいですか?\n\n" + "\n".join(preview_lines)
            if not messagebox.askyesno(APP_TITLE, msg):
                return

        self.cancel_flag = False
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._append_log(f"===== 処理開始: {len(files)}ファイル =====")

        def on_log(s):
            self.log_queue.put(("log", s))

        def on_progress(name, cur, total, pct):
            self.log_queue.put(("progress", name, cur, total, pct))

        def should_cancel():
            return self.cancel_flag

        if self.detailed_analysis.get():
            def worker():
                all_segments: list[CoarseSegment] = []
                for src in files:
                    if should_cancel():
                        on_log("キャンセルされました。")
                        break
                    segs, errs = pipeline.split_coarse(src, opts, on_log, on_progress)
                    for e in errs:
                        on_log("  ! " + e)
                    if segs:
                        pipeline.analyze_coarse_segments(segs, opts, on_log)
                        all_segments.extend(segs)
                self.log_queue.put(("review", all_segments, opts))

            self.worker = threading.Thread(target=worker, daemon=True)
            self.worker.start()
        else:
            def worker():
                result = process_all(files, opts, on_log, on_progress, should_cancel)
                self.log_queue.put(("done", result))

            self.worker = threading.Thread(target=worker, daemon=True)
            self.worker.start()

    def _open_review_window(self, segments: list[CoarseSegment], opts: PipelineOptions):
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.file_progress_label.config(text="解析完了 - 候補を確認してください")
        total_candidates = sum(len(s.candidates) for s in segments)
        if total_candidates == 0:
            messagebox.showinfo(APP_TITLE, "番組候補が見つかりませんでした。")
            return
        self._append_log(f"===== 解析完了: {len(segments)}区間 / 候補{total_candidates}件 "
                          f"=====\n候補確認ウィンドウを開きます。")
        ReviewWindow(self, segments,
                     on_extract=lambda segs, merge: self._start_extraction(segs, opts, merge))

    def _start_extraction(self, segments: list[CoarseSegment], opts: PipelineOptions, merge: bool = False):
        self.cancel_flag = False
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._append_log(f"===== 選択項目の抽出を開始{'(放送局ごとに結合)' if merge else ''} =====")

        def on_log(s):
            self.log_queue.put(("log", s))

        def should_cancel():
            return self.cancel_flag

        def worker():
            result = pipeline.extract_selected(segments, opts, on_log, should_cancel, merge_by_station=merge)
            self.log_queue.put(("done", result))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _cancel(self):
        self.cancel_flag = True
        self.cancel_btn.config(state="disabled")
        self._append_log("キャンセル要求を送信しました(現在の分割処理が終わり次第停止します)")

    def _poll_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "progress":
                    _, name, cur, total, pct = item
                    self.file_progress_label.config(text=f"処理中: {name}  ({cur:,}MB / {total:,}MB)")
                    self.file_progress["value"] = pct
                elif kind == "done":
                    result = item[1]
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.file_progress_label.config(text="完了")
                    self.file_progress["value"] = 0
                    ok_n = sum(1 for r in result.reports if r.ok_info)
                    self._append_log(f"===== 完了: 出力{len(result.reports)}件 (情報取得成功{ok_n}件) "
                                      f"エラー{len(result.errors)}件 =====")
                    for e in result.errors:
                        self._append_log("  ! " + e)
                    if result.summary_csv:
                        self._append_log(f"サマリー: {result.summary_csv}")
                elif kind == "review":
                    _, segments, opts = item
                    self._open_review_window(segments, opts)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        if not os.path.exists(config.SETTINGS_PATH):
            return
        try:
            with open(config.SETTINGS_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self.tssplitter_var.set(s.get("tssplitter_exe", config.DEFAULT_TSSPLITTER_EXE))
        self.rplsinfo_var.set(s.get("rplsinfo_exe", config.DEFAULT_RPLSINFO_EXE))
        self.split_mode_var.set(s.get("split_mode", "SEP2"))
        self.pattern_ok_var.set(s.get("pattern_ok", naming.DEFAULT_PATTERN_OK))
        self.pattern_ng_var.set(s.get("pattern_ng", naming.DEFAULT_PATTERN_NG))

    def _save_settings(self):
        s = {
            "tssplitter_exe": self.tssplitter_var.get(),
            "rplsinfo_exe": self.rplsinfo_var.get(),
            "split_mode": self.split_mode_var.get(),
            "pattern_ok": self.pattern_ok_var.get(),
            "pattern_ng": self.pattern_ng_var.get(),
        }
        try:
            with open(config.SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "処理中です。終了しますか?"):
                return
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
