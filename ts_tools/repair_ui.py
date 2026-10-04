"""複数ソース補完(同じ放送を録画した複数のTS/M2TSを比較してドロップを埋める)のGUI画面。"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from . import config, mpegts, probe, repair
from .packet_viewer_ui import render_hex_dump

try:
    from tkinterdnd2 import DND_FILES
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

STREAM_TYPE_LABELS = {
    0x01: "映像(MPEG-1)", 0x02: "映像(MPEG-2)", 0x1B: "映像(H.264)", 0x24: "映像(H.265)",
    0x03: "音声(MPEG-1)", 0x04: "音声(MPEG-2)", 0x0F: "音声(AAC)", 0x11: "音声(AAC LATM)",
    0x81: "音声(AC-3)", 0x06: "字幕/データ", 0x0D: "データカルーセル",
}


class RepairPanel(ttk.Frame):
    """「複数ソース補完」タブの中身。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.primary_var = tk.StringVar()
        self.secondary_files: list[str] = []
        self.output_var = tk.StringVar()
        self.pid_vars: list[tuple[int, int, tk.BooleanVar]] = []   # (pid, stream_type, var)
        self._stream_info = None
        self._packet_size = 188
        self._sync_offset = 0
        self.worker: Optional[threading.Thread] = None
        self._gap_candidates: list = []          # repair.GapCandidate のリスト(検出したPID分)
        self._gap_preview_pid: Optional[int] = None
        self.cancel_flag = False

        self._build_ui()

    def _build_ui(self):
        dnd_hint = "" if DND_AVAILABLE else "(tkinterdnd2未導入のためドラッグ&ドロップは無効です)"
        intro = ttk.Label(
            self,
            text="同じ放送を別々に録画した(またはダビングした)2つ以上のTS/M2TSファイルを比較し、\n"
                 "主ファイルのドロップ(パケット欠損)を、他の健全なファイルのデータで補完します。\n"
                 f"元ファイルは変更しません。全ての候補に欠損がある箇所は補完できずそのまま残ります。{dnd_hint}",
            foreground="#555", justify="left",
        )
        intro.pack(anchor="w", padx=8, pady=8)

        frm_files = ttk.LabelFrame(self, text="入力ファイル")
        frm_files.pack(fill="x", padx=8, pady=4)

        row1 = ttk.Frame(frm_files)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="主(メイン):", width=14).pack(side="left")
        self.primary_entry = ttk.Entry(row1, textvariable=self.primary_var)
        self.primary_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="参照...", command=self._choose_primary).pack(side="left")

        ttk.Label(frm_files, text="副(補完元。複数選択可。ファイルをドラッグ&ドロップでも追加できます):"
                  ).pack(anchor="w", pady=(6, 0))
        row2 = ttk.Frame(frm_files)
        row2.pack(fill="x", pady=2)
        self.secondary_listbox = tk.Listbox(row2, height=4, selectmode="extended")
        self.secondary_listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(row2, command=self.secondary_listbox.yview)
        sb.pack(side="left", fill="y")
        self.secondary_listbox.config(yscrollcommand=sb.set)
        btns2 = ttk.Frame(row2)
        btns2.pack(side="left", padx=4)
        ttk.Button(btns2, text="追加...", command=self._add_secondary_files).pack(fill="x", pady=1)
        ttk.Button(btns2, text="選択を削除", command=self._remove_secondary_selected).pack(fill="x", pady=1)
        ttk.Button(btns2, text="全て削除", command=self._clear_secondary).pack(fill="x", pady=1)

        if DND_AVAILABLE:
            self.primary_entry.drop_target_register(DND_FILES)
            self.primary_entry.dnd_bind("<<Drop>>", self._on_drop_primary)
            for w in (self.secondary_listbox, row2):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop_secondary)

        ttk.Button(frm_files, text="解析(対象PIDを表示)", command=self._analyze).pack(anchor="w", pady=4)

        frm_pids = ttk.LabelFrame(self, text="補完するPID(通常は映像PIDのみでOK)")
        frm_pids.pack(fill="both", expand=True, padx=8, pady=4)
        self.pid_list_frame = ttk.Frame(frm_pids)
        self.pid_list_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self.pid_hint = ttk.Label(frm_pids, text="(先に「解析」を押してください)", foreground="#777")
        self.pid_hint.pack(anchor="w", padx=4)

        frm_preview = ttk.LabelFrame(self, text="ドロップ箇所をHEXで見ながら確認・選択")
        frm_preview.pack(fill="both", expand=True, padx=8, pady=4)

        row_pv = ttk.Frame(frm_preview)
        row_pv.pack(fill="x", pady=2)
        ttk.Label(row_pv, text="対象PID:").pack(side="left")
        self.preview_pid_var = tk.StringVar()
        self.preview_pid_combo = ttk.Combobox(row_pv, textvariable=self.preview_pid_var,
                                               state="readonly", width=24)
        self.preview_pid_combo.pack(side="left", padx=4)
        ttk.Button(row_pv, text="ドロップ箇所を検出", command=self._detect_gaps).pack(side="left", padx=4)
        ttk.Button(row_pv, text="選択行を切替(含める/除外)", command=self._toggle_selected_gap
                   ).pack(side="left", padx=4)
        ttk.Button(row_pv, text="このPIDの確認内容で補完を実行", command=self._apply_previewed
                   ).pack(side="left", padx=4)
        self.gap_count_label = ttk.Label(row_pv, text="")
        self.gap_count_label.pack(side="left", padx=8)

        body = ttk.Frame(frm_preview)
        body.pack(fill="both", expand=True, pady=4)

        list_frame = ttk.Frame(body)
        list_frame.pack(side="left", fill="both", expand=False)
        columns = ("state", "pos", "missing", "source")
        self.gap_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=14)
        for col, text, w in (("state", "状態", 50), ("pos", "primary位置", 100),
                              ("missing", "推定欠損数", 80), ("source", "補完元", 160)):
            self.gap_tree.heading(col, text=text)
            self.gap_tree.column(col, width=w, anchor="w")
        self.gap_tree.pack(side="left", fill="both", expand=True)
        gap_sb = ttk.Scrollbar(list_frame, command=self.gap_tree.yview)
        gap_sb.pack(side="left", fill="y")
        self.gap_tree.config(yscrollcommand=gap_sb.set)
        self.gap_tree.bind("<<TreeviewSelect>>", lambda e: self._show_gap_preview())
        self.gap_tree.bind("<Double-1>", lambda e: self._toggle_selected_gap())

        hex_frame = ttk.Frame(body)
        hex_frame.pack(side="left", fill="both", expand=True, padx=(6, 0))
        ttk.Label(hex_frame, text="主(primary) - ドロップ前後(緑=直前の正常データ, 赤=CCが飛んだ直後)",
                  foreground="#555").pack(anchor="w")
        self.gap_hex_primary = tk.Text(hex_frame, height=8, wrap="none", font=("Consolas", 9), state="disabled")
        self.gap_hex_primary.pack(fill="both", expand=True)
        self.gap_hex_primary.tag_configure("before", background="#c8f0c8")
        self.gap_hex_primary.tag_configure("after", background="#f8c8c8")
        ttk.Label(hex_frame, text="補完候補(secondaryから挿入されるデータ)", foreground="#555"
                  ).pack(anchor="w", pady=(4, 0))
        self.gap_hex_patch = tk.Text(hex_frame, height=6, wrap="none", font=("Consolas", 9), state="disabled")
        self.gap_hex_patch.pack(fill="both", expand=True)
        self.gap_hex_patch.tag_configure("patch", background="#c8d8f8")

        frm_out = ttk.LabelFrame(self, text="出力先")
        frm_out.pack(fill="x", padx=8, pady=4)
        row3 = ttk.Frame(frm_out)
        row3.pack(fill="x", pady=2)
        ttk.Entry(row3, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row3, text="参照...", command=self._choose_output).pack(side="left")

        row4 = ttk.Frame(self)
        row4.pack(fill="x", padx=8, pady=4)
        self.run_btn = ttk.Button(row4, text="補完を実行", command=self._start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row4, text="キャンセル", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.status_label = ttk.Label(row4, text="")
        self.status_label.pack(side="left", padx=8)

        row5 = ttk.Frame(self)
        row5.pack(fill="x", padx=8, pady=(0, 4))
        self.progress_label = ttk.Label(row5, text="")
        self.progress_label.pack(anchor="w")
        self.progress_bar = ttk.Progressbar(row5, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x")

        frm_log = ttk.LabelFrame(self, text="ログ")
        frm_log.pack(fill="both", expand=True, padx=8, pady=4)
        self.log_text = scrolledtext.ScrolledText(frm_log, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # --------------------------------------------------------------- 入力
    def _is_ts_file(self, path: str) -> bool:
        return os.path.isfile(path) and path.lower().endswith(config.SUPPORTED_EXTS)

    def _choose_primary(self):
        p = filedialog.askopenfilename(title="主ファイルを選択",
                                        filetypes=[("TS/M2TS", "*.ts *.m2ts"), ("すべてのファイル", "*.*")])
        if p:
            self._set_primary(p)

    def _set_primary(self, p: str):
        self.primary_var.set(p)
        if not self.output_var.get():
            stem, ext = os.path.splitext(p)
            self.output_var.set(f"{stem}_repaired{ext}")

    def _add_secondary_files(self):
        paths = filedialog.askopenfilenames(title="補完元ファイルを選択(複数可)",
                                             filetypes=[("TS/M2TS", "*.ts *.m2ts"), ("すべてのファイル", "*.*")])
        for p in paths:
            self._add_secondary_path(p)

    def _add_secondary_path(self, p: str) -> bool:
        if p not in self.secondary_files:
            self.secondary_files.append(p)
            self.secondary_listbox.insert("end", p)
            return True
        return False

    def _remove_secondary_selected(self):
        for idx in reversed(self.secondary_listbox.curselection()):
            del self.secondary_files[idx]
            self.secondary_listbox.delete(idx)

    def _clear_secondary(self):
        self.secondary_files.clear()
        self.secondary_listbox.delete(0, "end")

    def _on_drop_primary(self, event):
        paths = self.tk.splitlist(event.data)
        for p in paths:
            if self._is_ts_file(p):
                self._set_primary(p)
                break

    def _on_drop_secondary(self, event):
        paths = self.tk.splitlist(event.data)
        added = 0
        for p in paths:
            if self._is_ts_file(p) and self._add_secondary_path(p):
                added += 1
        if added == 0:
            messagebox.showinfo("複数ソース補完", "対象ファイル(.ts/.m2ts)ではないため追加されませんでした。")

    def _choose_output(self):
        p = filedialog.asksaveasfilename(title="出力先を指定",
                                          filetypes=[("TS/M2TS", "*.ts *.m2ts"), ("すべてのファイル", "*.*")])
        if p:
            self.output_var.set(p)

    def _append_log(self, text: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _cancel(self):
        self.cancel_flag = True
        self.cancel_btn.config(state="disabled")
        self._append_log("キャンセル要求を送信しました(区切りの良いところで停止します)")

    def _update_progress(self, label: str, cur: int, total: int):
        pct = int(cur * 100 / total) if total else 0
        # 「補完候補を検索中」フェーズはバイト位置ではなくドロップ箇所の件数で
        # 進捗を報告するため、単位表記を変える(それ以外はバイト単位)。
        unit = "件" if "補完候補を検索中" in label else "バイト"
        self.progress_label.config(text=f"{label} ({cur:,} / {total:,} {unit})")
        self.progress_bar["value"] = pct

    def _make_progress_callback(self):
        def on_progress(label, cur, total):
            self.after(0, self._update_progress, label, cur, total)
        return on_progress

    def _should_cancel(self) -> bool:
        return self.cancel_flag

    # ------------------------------------------------------------- 解析
    def _analyze(self):
        primary = self.primary_var.get()
        if not primary or not os.path.exists(primary):
            messagebox.showwarning("複数ソース補完", "主ファイルを選択してください。")
            return
        pr = probe.probe_file(primary)
        if pr.packet_size not in (188, 192):
            messagebox.showerror("複数ソース補完", f"パケットサイズを判定できませんでした: {pr.detail}")
            return
        self._packet_size = pr.packet_size
        self._sync_offset = 4 if pr.packet_size == 192 else 0
        info = mpegts.get_stream_info(primary, self._packet_size, self._sync_offset)
        self._stream_info = info

        for child in self.pid_list_frame.winfo_children():
            child.destroy()
        self.pid_vars.clear()

        if not info.all_pids:
            self.pid_hint.config(text="PMTを解析できませんでした。")
            return
        self.pid_hint.config(text=f"PCR PID: 0x{info.pcr_pid:04x}" if info.pcr_pid else "")

        for pid in info.all_pids:
            default_check = (pid == info.video_pid)
            var = tk.BooleanVar(value=default_check)
            stream_type = info.pid_types.get(pid)
            type_label = STREAM_TYPE_LABELS.get(stream_type, f"種別0x{stream_type:02x}" if stream_type is not None else "")
            label = f"PID 0x{pid:04x}" + (f" - {type_label}" if type_label else "")
            ttk.Checkbutton(self.pid_list_frame, text=label, variable=var).pack(anchor="w")
            self.pid_vars.append((pid, stream_type or 0, var))

        pid_labels = [f"0x{pid:04x}" for pid in info.all_pids]
        self.preview_pid_combo.config(values=pid_labels)
        if info.video_pid is not None:
            self.preview_pid_var.set(f"0x{info.video_pid:04x}")
        elif pid_labels:
            self.preview_pid_var.set(pid_labels[0])
        self.gap_tree.delete(*self.gap_tree.get_children())
        self._gap_candidates = []
        self.gap_count_label.config(text="")

    # --------------------------------------------------------- ドロップ確認
    def _detect_gaps(self):
        primary = self.primary_var.get()
        secondaries = list(self.secondary_files)
        if not self._stream_info or not primary or not secondaries:
            messagebox.showwarning("複数ソース補完", "先に主・副ファイルを指定して「解析」を押してください。")
            return
        pid_str = self.preview_pid_var.get()
        if not pid_str:
            messagebox.showwarning("複数ソース補完", "対象PIDを選んでください。")
            return
        target_pid = int(pid_str, 16)
        pcr_pid = self._stream_info.pcr_pid
        if pcr_pid is None:
            messagebox.showerror("複数ソース補完", "PCR PIDが見つからないため検出できません。")
            return

        self.gap_tree.delete(*self.gap_tree.get_children())
        self._gap_candidates = []
        self.gap_count_label.config(text="検出中...")

        packet_size, sync_offset = self._packet_size, self._sync_offset
        self.cancel_flag = False
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")

        def worker():
            try:
                cands = repair.find_gap_candidates(
                    primary, secondaries, packet_size, sync_offset, target_pid, pcr_pid,
                    on_log=lambda s: None, on_progress=self._make_progress_callback(),
                    should_cancel=self._should_cancel,
                )
                self._gap_candidates = cands
                self._gap_preview_pid = target_pid

                def show_result():
                    for i, cand in enumerate(cands):
                        self._insert_gap_row(i, cand)
                    n_patchable = sum(1 for c in cands if c.patchable)
                    self.gap_count_label.config(text=f"検出: {len(cands)}件(補完候補あり: {n_patchable}件)")
                self.after(0, show_result)
            except mpegts.ScanCancelled:
                self.after(0, lambda: self.gap_count_label.config(text="キャンセルされました"))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("複数ソース補完", f"検出中にエラーが発生しました: {e}"))
            finally:
                self.after(0, lambda: self.run_btn.config(state="normal"))
                self.after(0, lambda: self.cancel_btn.config(state="disabled"))
                self.after(0, lambda: self.progress_label.config(text=""))
                self.after(0, lambda: self.progress_bar.config(value=0))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _insert_gap_row(self, i: int, cand):
        state = "含める" if (cand.patchable and cand.selected) else ("除外" if cand.patchable else "補完不可")
        source = os.path.basename(cand.source_path) if cand.source_path else "-"
        self.gap_tree.insert("", "end", iid=str(i),
                              values=(state, f"{cand.gap.pos_before:,}", cand.expected_count, source))

    def _refresh_gap_row(self, i: int):
        cand = self._gap_candidates[i]
        state = "含める" if (cand.patchable and cand.selected) else ("除外" if cand.patchable else "補完不可")
        source = os.path.basename(cand.source_path) if cand.source_path else "-"
        self.gap_tree.item(str(i), values=(state, f"{cand.gap.pos_before:,}", cand.expected_count, source))

    def _selected_gap_index(self) -> Optional[int]:
        sel = self.gap_tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def _toggle_selected_gap(self):
        idx = self._selected_gap_index()
        if idx is None or idx >= len(self._gap_candidates):
            return
        cand = self._gap_candidates[idx]
        if not cand.patchable:
            return
        cand.selected = not cand.selected
        self._refresh_gap_row(idx)

    def _show_gap_preview(self):
        idx = self._selected_gap_index()
        if idx is None or idx >= len(self._gap_candidates):
            return
        cand = self._gap_candidates[idx]
        primary = self.primary_var.get()
        target_pid = self._gap_preview_pid

        before = repair._extract_pid_packets(primary, self._packet_size, self._sync_offset, target_pid,
                                              max(0, cand.gap.pos_before - 400_000), cand.gap.pos_before + self._packet_size)
        before = before[-3:] if before else []
        after = repair._extract_pid_packets(primary, self._packet_size, self._sync_offset, target_pid,
                                             cand.gap.pos_after, cand.gap.pos_after + 400_000)
        after = after[:3] if after else []

        self.gap_hex_primary.config(state="normal")
        self.gap_hex_primary.delete("1.0", "end")
        data = b"".join(bytes(p) for p in before) + b"".join(bytes(p) for p in after)
        positions = render_hex_dump(self.gap_hex_primary, data)
        before_len = sum(len(p) for p in before)
        for byte_idx, (line, c0, c1) in positions.items():
            tag = "before" if byte_idx < before_len else "after"
            self.gap_hex_primary.tag_add(tag, f"{line}.{c0}", f"{line}.{c1}")
        self.gap_hex_primary.config(state="disabled")

        self.gap_hex_patch.config(state="normal")
        self.gap_hex_patch.delete("1.0", "end")
        if cand.patchable:
            patch_data = b"".join(bytes(p) for p in cand.patch_packets)
            positions2 = render_hex_dump(self.gap_hex_patch, patch_data)
            for byte_idx, (line, c0, c1) in positions2.items():
                self.gap_hex_patch.tag_add("patch", f"{line}.{c0}", f"{line}.{c1}")
        else:
            self.gap_hex_patch.insert("1.0", "(補完候補が見つかりませんでした。全ての補完元にも同じ箇所に欠損があります)")
        self.gap_hex_patch.config(state="disabled")

    def _apply_previewed(self):
        if not self._gap_candidates or self._gap_preview_pid is None:
            messagebox.showwarning("複数ソース補完", "先に「ドロップ箇所を検出」を押してください。")
            return
        out_path = self.output_var.get()
        if not out_path:
            messagebox.showwarning("複数ソース補完", "出力先を指定してください。")
            return
        if os.path.exists(out_path):
            if not messagebox.askyesno("複数ソース補完", f"出力先が既に存在します。上書きしますか?\n{out_path}"):
                return

        primary = self.primary_var.get()
        secondaries = list(self.secondary_files)
        target_pid = self._gap_preview_pid
        pcr_pid = self._stream_info.pcr_pid
        candidates = self._gap_candidates

        self.cancel_flag = False
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_label.config(text="実行中...")
        n_selected = sum(1 for c in candidates if c.patchable and c.selected)
        self._append_log(f"===== 確認済みの内容で補完を開始(PID 0x{target_pid:04x}, "
                          f"{n_selected}/{len(candidates)}件を適用) =====")

        def on_log(s):
            self.after(0, self._append_log, s)

        def worker():
            try:
                report = repair.repair_pid(primary, secondaries, out_path, self._packet_size, self._sync_offset,
                                            target_pid, pcr_pid, on_log=on_log, candidates=candidates,
                                            on_progress=self._make_progress_callback(),
                                            should_cancel=self._should_cancel)
                on_log(f"===== 完了: {report.gaps_patched}/{report.gaps_found}箇所を補完しました =====")
                on_log(f"出力: {out_path}")
            except mpegts.ScanCancelled:
                on_log("===== キャンセルされました =====")
            except Exception as e:  # noqa: BLE001
                on_log(f"エラー: {e}")
            finally:
                self.after(0, lambda: self.run_btn.config(state="normal"))
                self.after(0, lambda: self.cancel_btn.config(state="disabled"))
                self.after(0, lambda: self.status_label.config(text="完了"))
                self.after(0, lambda: self.progress_label.config(text=""))
                self.after(0, lambda: self.progress_bar.config(value=0))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    # -------------------------------------------------------------- 実行
    def _start(self):
        primary = self.primary_var.get()
        secondaries = list(self.secondary_files)
        out_path = self.output_var.get()
        if not primary or not secondaries or not out_path:
            messagebox.showwarning("複数ソース補完", "主ファイル・補完元ファイル(1つ以上)・出力先を指定してください。")
            return
        if not self._stream_info:
            messagebox.showwarning("複数ソース補完", "先に「解析」を押してPIDを表示してください。")
            return
        selected_pids = [pid for pid, _st, var in self.pid_vars if var.get()]
        if not selected_pids:
            messagebox.showwarning("複数ソース補完", "補完するPIDを1つ以上選んでください。")
            return
        if os.path.exists(out_path):
            if not messagebox.askyesno("複数ソース補完", f"出力先が既に存在します。上書きしますか?\n{out_path}"):
                return
        pcr_pid = self._stream_info.pcr_pid
        if pcr_pid is None:
            messagebox.showerror("複数ソース補完", "PCR PIDが見つからないため実行できません。")
            return

        self.cancel_flag = False
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_label.config(text="実行中...")
        self._append_log(f"===== 補完を開始(補完元 {len(secondaries)}件) =====")

        packet_size, sync_offset = self._packet_size, self._sync_offset

        def on_log(s):
            self.after(0, self._append_log, s)

        def worker():
            try:
                reports = repair.repair_multi(primary, secondaries, out_path, packet_size, sync_offset,
                                               selected_pids, pcr_pid, on_log=on_log,
                                               on_progress=self._make_progress_callback(),
                                               should_cancel=self._should_cancel)
                total_found = sum(r.gaps_found for r in reports)
                total_patched = sum(r.gaps_patched for r in reports)
                on_log(f"===== 完了: 合計 {total_patched}/{total_found} 箇所を補完しました =====")
                on_log(f"出力: {out_path}")
            except mpegts.ScanCancelled:
                on_log("===== キャンセルされました =====")
            except Exception as e:  # noqa: BLE001
                on_log(f"エラー: {e}")
            finally:
                self.after(0, lambda: self.run_btn.config(state="normal"))
                self.after(0, lambda: self.cancel_btn.config(state="disabled"))
                self.after(0, lambda: self.status_label.config(text="完了"))
                self.after(0, lambda: self.progress_label.config(text=""))
                self.after(0, lambda: self.progress_bar.config(value=0))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()
