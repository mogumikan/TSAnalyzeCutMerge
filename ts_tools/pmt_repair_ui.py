"""再生修復(PMT補完)・不要ファイル整理タブのGUI画面。"""
from __future__ import annotations

import os
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from . import config, mpegts, pmt_repair, probe

try:
    from tkinterdnd2 import DND_FILES
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

STATUS_LABELS = {
    "ok": "OK(正常)",
    "repairable": "PMT欠落(修復可能)",
    "unrepairable": "PMT欠落(手がかり無し)",
    "no_pat": "PATが見つからない",
    "error": "読み込みエラー",
}

KIND_LABEL_JA = {"video": "映像", "audio": "音声", "unknown": "不明(データ)"}


class FileCheckItem:
    def __init__(self, path: str):
        self.path = path
        self.size = 0
        self.status = "unknown"
        self.detail = ""
        self.diag = None
        self.packet_size = 188
        self.sync_offset = 0


class PmtRepairPanel(ttk.Frame):
    """「再生修復・整理」タブの中身。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.items: dict = {}   # path -> FileCheckItem
        self.worker: Optional[threading.Thread] = None
        self.cancel_flag = False
        self._build_ui()

    def _build_ui(self):
        dnd_hint = "" if DND_AVAILABLE else "(tkinterdnd2未導入のためドラッグ&ドロップは無効です)"
        intro = ttk.Label(
            self,
            text="TsSplitterでの分割時にPMT(番組の映像/音声PIDの対応表)が欠落してしまい、\n"
                 "TVTest等の一部プレーヤーで再生できなくなったファイルを診断・修復します。\n"
                 "この修復は元のPMTを正確に復元するものではなく、ファイルに実際に残っている\n"
                 "映像/音声のPIDから最小限のPMTを合成する、あくまでベストエフォートの処置です\n"
                 "(映像/音声のデータ自体は変更しません)。元ファイルは残したまま別名で保存します。\n"
                 f"あわせて、不要なファイルを削除ではなく「_trash」フォルダへ移動して整理できます。{dnd_hint}",
            foreground="#555", justify="left",
        )
        intro.pack(anchor="w", padx=8, pady=8)

        frm_files = ttk.LabelFrame(self, text="対象ファイル(ここにファイル/フォルダをドラッグ&ドロップできます)")
        frm_files.pack(fill="both", expand=False, padx=8, pady=4)

        row1 = ttk.Frame(frm_files)
        row1.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree = ttk.Treeview(row1, columns=("size", "status"), show="tree headings", height=8,
                                  selectmode="extended")
        self.tree.heading("#0", text="ファイル")
        self.tree.column("#0", width=460, anchor="w")
        self.tree.heading("size", text="サイズ")
        self.tree.column("size", width=90, anchor="e")
        self.tree.heading("status", text="状態")
        self.tree.column("status", width=190, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(row1, command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.config(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())

        if DND_AVAILABLE:
            for w in (frm_files, self.tree):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        btns = ttk.Frame(frm_files)
        btns.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(btns, text="ファイル追加...", command=self._add_files).pack(side="left", padx=2)
        ttk.Button(btns, text="フォルダ追加...", command=self._add_folder).pack(side="left", padx=2)
        ttk.Button(btns, text="選択を削除", command=self._remove_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="全て削除", command=self._clear_files).pack(side="left", padx=2)
        ttk.Button(btns, text="診断", command=self._diagnose).pack(side="left", padx=12)

        frm_detail = ttk.LabelFrame(self, text="診断詳細(選択中のファイル)")
        frm_detail.pack(fill="both", expand=True, padx=8, pady=4)
        self.detail_text = tk.Text(frm_detail, height=8, wrap="word", state="disabled")
        self.detail_text.pack(fill="both", expand=True, padx=4, pady=4)

        row2 = ttk.Frame(self)
        row2.pack(fill="x", padx=8, pady=4)
        self.repair_btn = ttk.Button(row2, text="選択したファイルを修復(PMTを補完・別名保存)",
                                      command=self._start_repair)
        self.repair_btn.pack(side="left")
        self.trash_btn = ttk.Button(row2, text="選択したファイルを_trashへ移動...", command=self._move_to_trash)
        self.trash_btn.pack(side="left", padx=8)
        self.cancel_btn = ttk.Button(row2, text="キャンセル", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        row3 = ttk.Frame(self)
        row3.pack(fill="x", padx=8, pady=(0, 4))
        self.progress_label = ttk.Label(row3, text="")
        self.progress_label.pack(anchor="w")
        self.progress_bar = ttk.Progressbar(row3, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x")

        frm_log = ttk.LabelFrame(self, text="ログ")
        frm_log.pack(fill="both", expand=True, padx=8, pady=4)
        self.log_text = scrolledtext.ScrolledText(frm_log, height=10, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # --------------------------------------------------------------- 入力
    def _is_ts_file(self, path: str) -> bool:
        return os.path.isfile(path) and path.lower().endswith(config.SUPPORTED_EXTS)

    def _add_path(self, path: str):
        if path in self.items:
            return
        item = FileCheckItem(path)
        try:
            item.size = os.path.getsize(path)
        except OSError:
            item.size = 0
        self.items[path] = item
        self.tree.insert("", "end", iid=path, text=path,
                          values=(probe.human_size(item.size), "(未診断)"))

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="TS/M2TSファイルを選択", filetypes=[("TS/M2TS", "*.ts *.m2ts"), ("すべてのファイル", "*.*")])
        for p in paths:
            self._add_path(p)

    def _add_folder(self):
        d = filedialog.askdirectory(title="フォルダを選択(直下の.ts/.m2tsを追加します)")
        if not d:
            return
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        for fn in entries:
            full = os.path.join(d, fn)
            if self._is_ts_file(full):
                self._add_path(full)

    def _on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        for p in paths:
            if os.path.isdir(p):
                try:
                    entries = sorted(os.listdir(p))
                except OSError:
                    continue
                for fn in entries:
                    full = os.path.join(p, fn)
                    if self._is_ts_file(full):
                        self._add_path(full)
            elif self._is_ts_file(p):
                self._add_path(p)

    def _remove_selected(self):
        for path in self.tree.selection():
            self.tree.delete(path)
            self.items.pop(path, None)

    def _clear_files(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()

    def _append_log(self, text: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _cancel(self):
        self.cancel_flag = True
        self.cancel_btn.config(state="disabled")
        self._append_log("キャンセル要求を送信しました(区切りの良いところで停止します)")

    def _make_progress_callback(self):
        def on_progress(label, cur, total):
            self.after(0, self._update_progress, label, cur, total)
        return on_progress

    def _update_progress(self, label: str, cur: int, total: int):
        pct = int(cur * 100 / total) if total else 0
        self.progress_label.config(text=f"{label} ({cur:,} / {total:,} バイト)")
        self.progress_bar["value"] = pct

    def _should_cancel(self) -> bool:
        return self.cancel_flag

    def _show_detail(self):
        sel = self.tree.selection()
        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", "end")
        if sel:
            item = self.items.get(sel[0])
            if item:
                self.detail_text.insert("end", item.detail or "(まだ診断していません。「診断」を押してください)")
        self.detail_text.config(state="disabled")

    # --------------------------------------------------------------- 診断
    def _diagnose(self):
        targets = list(self.items.values())
        if not targets:
            messagebox.showinfo("再生修復・整理", "対象ファイルを追加してください。")
            return
        self.cancel_flag = False
        self.repair_btn.config(state="disabled")
        self.trash_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._append_log(f"===== 診断開始: {len(targets)}ファイル =====")

        def worker():
            try:
                for item in targets:
                    if self._should_cancel():
                        self.after(0, self._append_log, "キャンセルされました。")
                        break
                    self.after(0, self._append_log, f"診断中: {os.path.basename(item.path)}")
                    try:
                        pr = probe.probe_file(item.path, sample_bytes=4 * 1024 * 1024)
                        if not pr.packet_size:
                            item.status = "error"
                            item.detail = f"パケットサイズを判定できませんでした: {pr.detail}"
                            self.after(0, self._apply_item_result, item)
                            continue
                        item.packet_size = pr.packet_size
                        item.sync_offset = 4 if pr.packet_size == 192 else 0
                        gui_progress = self._make_progress_callback()
                        label = f"診断中: {os.path.basename(item.path)}"

                        def diag_progress(cur, total, _label=label):
                            gui_progress(_label, cur, total)

                        diag = pmt_repair.diagnose(
                            item.path, item.packet_size, item.sync_offset,
                            on_progress=diag_progress,
                            should_cancel=self._should_cancel,
                        )
                        item.diag = diag
                        item.detail = self._format_detail(diag)
                        if not diag.pat_found:
                            item.status = "no_pat"
                        elif not diag.has_missing_pmt:
                            item.status = "ok"
                        elif any(o.kind in ("video", "audio") for o in diag.orphan_pids):
                            item.status = "repairable"
                        else:
                            item.status = "unrepairable"
                    except mpegts.ScanCancelled:
                        self.after(0, self._append_log, "キャンセルされました。")
                        break
                    except OSError as e:
                        item.status = "error"
                        item.detail = f"読み込みエラー: {e}"
                    except Exception as e:  # noqa: BLE001 - 想定外のエラーでもUIを固まらせない
                        item.status = "error"
                        item.detail = f"予期しないエラー: {e!r}"
                        self.after(0, self._append_log, f"! {os.path.basename(item.path)}: 予期しないエラー: {e!r}")
                    self.after(0, self._apply_item_result, item)
            finally:
                self.after(0, self._diagnose_done)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _format_detail(self, diag) -> str:
        lines = []
        if not diag.pat_found:
            lines.append("PAT自体が見つかりませんでした。")
            return "\n".join(lines)
        for p in diag.programs:
            state = "OK" if p.pmt_present else "PMTのパケットが1つも見つかりません(欠落)"
            lines.append(f"program_number={p.program_number}  PMT PID=0x{p.pmt_pid:04x}  {state}")
        if diag.has_missing_pmt:
            if diag.orphan_pids:
                lines.append("")
                lines.append("修復に使えそうな(どのPMTにも属していない)PID:")
                for o in diag.orphan_pids:
                    kind = KIND_LABEL_JA.get(o.kind, o.kind)
                    lines.append(f"  0x{o.pid:04x}: {kind} ({o.packet_count:,}パケット)")
            else:
                lines.append("")
                lines.append("手がかりになる映像/音声らしきPIDが見つからず、修復できません。")
        return "\n".join(lines)

    def _apply_item_result(self, item: FileCheckItem):
        self.tree.item(item.path, values=(probe.human_size(item.size),
                                           STATUS_LABELS.get(item.status, item.status)))
        self._show_detail()

    def _diagnose_done(self):
        self.repair_btn.config(state="normal")
        self.trash_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.progress_label.config(text="")
        self.progress_bar["value"] = 0
        n_repairable = sum(1 for it in self.items.values() if it.status == "repairable")
        self._append_log(f"===== 診断完了: 修復可能{n_repairable}件 =====")

    # --------------------------------------------------------------- 修復
    def _start_repair(self):
        targets = [self.items[p] for p in self.tree.selection()
                   if p in self.items and self.items[p].status == "repairable"]
        if not targets:
            messagebox.showinfo("再生修復・整理",
                                 "「診断」を実行し、状態が「PMT欠落(修復可能)」のファイルを選択してください。")
            return
        self.cancel_flag = False
        self.repair_btn.config(state="disabled")
        self.trash_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._append_log(f"===== 修復開始: {len(targets)}ファイル =====")

        def worker():
            try:
                for item in targets:
                    if self._should_cancel():
                        self.after(0, self._append_log, "キャンセルされました。")
                        break
                    stem, ext = os.path.splitext(item.path)
                    out_path = f"{stem}_repaired{ext}"
                    n = 2
                    while os.path.exists(out_path):
                        out_path = f"{stem}_repaired_{n}{ext}"
                        n += 1
                    self.after(0, self._append_log,
                               f"修復中: {os.path.basename(item.path)} -> {os.path.basename(out_path)}")
                    try:
                        report = pmt_repair.repair_missing_pmt(
                            item.path, out_path, item.packet_size, item.sync_offset,
                            on_log=lambda s: self.after(0, self._append_log, s),
                            on_progress=self._make_progress_callback(),
                            should_cancel=self._should_cancel,
                        )
                        self.after(0, self._append_log,
                                   f"  -> {report.programs_repaired}件のプログラムを修復しました: {out_path}")
                    except mpegts.ScanCancelled:
                        self.after(0, self._append_log, "キャンセルされました。")
                        break
                    except OSError as e:
                        self.after(0, self._append_log, f"  ! 修復失敗: {e}")
                    except Exception as e:  # noqa: BLE001 - 想定外のエラーでもUIを固まらせない
                        self.after(0, self._append_log,
                                   f"  ! {os.path.basename(item.path)}: 予期しないエラー: {e!r}")
            finally:
                self.after(0, self._repair_done)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _repair_done(self):
        self.repair_btn.config(state="normal")
        self.trash_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.progress_label.config(text="")
        self.progress_bar["value"] = 0
        self._append_log("===== 修復完了 =====")

    # --------------------------------------------------------------- 整理
    def _move_to_trash(self):
        """選択ファイルを削除するのではなく、同じ場所の「_trash」フォルダへ移動する。
        (誤操作でも元に戻せるよう、既存の中間ファイル整理と同じ考え方で「削除しない」
        方式にしている。)"""
        paths = [p for p in self.tree.selection() if p in self.items]
        if not paths:
            messagebox.showinfo("再生修復・整理", "移動するファイルを選択してください。")
            return
        total = sum(self.items[p].size for p in paths)
        names = "\n".join(f"  {os.path.basename(p)} ({probe.human_size(self.items[p].size)})"
                           for p in paths[:12])
        more = f"\n  ...他{len(paths) - 12}件" if len(paths) > 12 else ""
        msg = (f"以下の{len(paths)}件(合計{probe.human_size(total)})を、それぞれのファイルと同じ場所の"
               f"「_trash」フォルダへ移動します(削除ではありません。元に戻したい場合は_trashフォルダから"
               f"手動で戻してください)。よろしいですか?\n\n{names}{more}")
        if not messagebox.askyesno("再生修復・整理", msg):
            return
        moved = 0
        for p in paths:
            try:
                trash_dir = os.path.join(os.path.dirname(p), "_trash")
                os.makedirs(trash_dir, exist_ok=True)
                dst = os.path.join(trash_dir, os.path.basename(p))
                base, ext = os.path.splitext(dst)
                n = 2
                while os.path.exists(dst):
                    dst = f"{base}_{n}{ext}"
                    n += 1
                shutil.move(p, dst)
                self._append_log(f"_trashへ移動: {p} -> {dst}")
                moved += 1
                self.tree.delete(p)
                self.items.pop(p, None)
                sidecar = os.path.splitext(p)[0] + ".info.txt"
                if os.path.exists(sidecar):
                    try:
                        shutil.move(sidecar, os.path.join(trash_dir, os.path.basename(sidecar)))
                    except OSError:
                        pass
            except OSError as e:
                self._append_log(f"! 移動失敗: {p}: {e}")
        self._append_log(f"===== {moved}件を_trashへ移動しました =====")
