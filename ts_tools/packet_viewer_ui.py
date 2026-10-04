"""TSパケットビューア: 188/192バイトのTSファイルを1パケット単位で覗き、
ヘッダの各フィールドとHEXダンプを対応付けて表示する(TSCutter.GUIの
「TS Packet Viewer」を参考にしたUI体験。実装はMPEG-2公開仕様からの
独自コードで、TSCutter.GUI自体のコードは見ていない)。
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import config, mpegts, probe

try:
    from tkinterdnd2 import DND_FILES
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

BYTES_PER_ROW = 16


def render_hex_dump(text_widget: tk.Text, data: bytes, start_line: int = 1) -> dict[int, tuple[int, int, int]]:
    """dataの内容をtext_widget(呼び出し側で事前にstate='normal'にしておくこと)に
    HEXダンプとして追記描画し、byte_idx(dataの先頭からのオフセット) ->
    (行番号, 開始列, 終了列) のマップを返す(タグ付けによるハイライトに使う)。
    他のパネル(パケットビューア・補完プレビュー)で共通して使う。
    """
    positions: dict[int, tuple[int, int, int]] = {}
    n_rows = (len(data) + BYTES_PER_ROW - 1) // BYTES_PER_ROW
    for row in range(n_rows):
        line_no = start_line + row
        start = row * BYTES_PER_ROW
        chunk = data[start:start + BYTES_PER_ROW]
        offset_str = f"{start:06X}  "
        text_widget.insert("end", offset_str)
        col = len(offset_str)
        for i, b in enumerate(chunk):
            hex_str = f"{b:02X} "
            text_widget.insert("end", hex_str)
            positions[start + i] = (line_no, col, col + 2)
            col += len(hex_str)
        text_widget.insert("end", " " * (BYTES_PER_ROW - len(chunk)) * 3 + " ")
        ascii_repr = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        text_widget.insert("end", ascii_repr + "\n")
    return positions


class PacketViewerPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.path: Optional[str] = None
        self.packet_size = 188
        self.sync_offset = 0
        self.total_packets = 0
        self.current_index = 0
        self.fields: dict = {}
        self._hex_positions: dict[int, tuple[int, int, int]] = {}  # byte_idx -> (line, col_start, col_end)
        self._field_rows: list[tuple[str, tk.Widget]] = []

        self._build_ui()

    # --------------------------------------------------------------- UI組立
    def _build_ui(self):
        dnd_hint = "(ファイルをドラッグ&ドロップできます)" if DND_AVAILABLE else ""
        frm_file = ttk.LabelFrame(self, text=f"ファイル {dnd_hint}")
        frm_file.pack(fill="x", padx=8, pady=6)
        row = ttk.Frame(frm_file)
        row.pack(fill="x", pady=2)
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(row, textvariable=self.path_var, state="readonly")
        self.path_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="開く...", command=self._open_file).pack(side="left")
        self.info_label = ttk.Label(frm_file, text="", foreground="#555")
        self.info_label.pack(anchor="w", padx=4)

        if DND_AVAILABLE:
            for w in (self.path_entry, frm_file):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        frm_nav = ttk.LabelFrame(self, text="ナビゲーション")
        frm_nav.pack(fill="x", padx=8, pady=4)
        row2 = ttk.Frame(frm_nav)
        row2.pack(fill="x", pady=2)
        ttk.Button(row2, text="◀ 前へ", command=self._prev_packet).pack(side="left", padx=2)
        ttk.Label(row2, text="パケット#:").pack(side="left", padx=(8, 2))
        self.packet_num_var = tk.StringVar(value="0")
        entry = ttk.Entry(row2, textvariable=self.packet_num_var, width=10)
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self._go_to_packet())
        ttk.Button(row2, text="移動", command=self._go_to_packet).pack(side="left", padx=2)
        ttk.Button(row2, text="次へ ▶", command=self._next_packet).pack(side="left", padx=2)

        ttk.Label(row2, text="  バイト位置:").pack(side="left", padx=(12, 2))
        self.byte_pos_var = tk.StringVar(value="0")
        entry2 = ttk.Entry(row2, textvariable=self.byte_pos_var, width=14)
        entry2.pack(side="left")
        entry2.bind("<Return>", lambda e: self._go_to_byte())
        ttk.Button(row2, text="移動", command=self._go_to_byte).pack(side="left", padx=2)

        row3 = ttk.Frame(frm_nav)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="PID(16進, 例 100):").pack(side="left")
        self.pid_filter_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.pid_filter_var, width=10).pack(side="left", padx=4)
        ttk.Button(row3, text="次の一致PIDへ", command=lambda: self._find_pid(1)).pack(side="left", padx=2)
        ttk.Button(row3, text="前の一致PIDへ", command=lambda: self._find_pid(-1)).pack(side="left", padx=2)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        frm_fields = ttk.LabelFrame(body, text="解析結果(クリックでHEX側をハイライト)")
        frm_fields.pack(side="left", fill="y", padx=(0, 4))
        self.fields_frame = ttk.Frame(frm_fields)
        self.fields_frame.pack(fill="both", expand=True, padx=4, pady=4)

        frm_hex = ttk.LabelFrame(body, text="HEXダンプ")
        frm_hex.pack(side="left", fill="both", expand=True)
        self.hex_text = tk.Text(frm_hex, width=64, wrap="none", font=("Consolas", 10), state="disabled")
        self.hex_text.pack(fill="both", expand=True, padx=4, pady=4)
        self.hex_text.tag_configure("header", background="#cfe8ff")
        self.hex_text.tag_configure("adaptation", background="#fff3b0")
        self.hex_text.tag_configure("payload", background="")
        self.hex_text.tag_configure("select", background="#ff9933")

    # ------------------------------------------------------------- 入出力
    def _is_ts_file(self, path: str) -> bool:
        return os.path.isfile(path) and path.lower().endswith(config.SUPPORTED_EXTS)

    def _on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        for p in paths:
            if self._is_ts_file(p):
                self._load_file(p)
                return
        messagebox.showinfo("パケットビューア", "対象ファイル(.ts/.m2ts)ではありません。")

    def _open_file(self):
        p = filedialog.askopenfilename(title="TS/M2TSファイルを選択",
                                        filetypes=[("TS/M2TS", "*.ts *.m2ts"), ("すべてのファイル", "*.*")])
        if p:
            self._load_file(p)

    def _load_file(self, path: str):
        pr = probe.probe_file(path)
        if pr.packet_size not in (188, 192):
            messagebox.showerror("パケットビューア", f"パケットサイズを判定できませんでした: {pr.detail}")
            return
        self.path = path
        self.packet_size = pr.packet_size
        self.sync_offset = 4 if pr.packet_size == 192 else 0
        size = os.path.getsize(path)
        self.total_packets = size // self.packet_size
        self.path_var.set(path)
        self.info_label.config(text=f"{pr.detail} / 総パケット数: {self.total_packets:,}")
        self.current_index = 0
        self._render_packet(0)

    # --------------------------------------------------------------- 移動
    def _read_packet(self, index: int) -> Optional[bytes]:
        if self.path is None or not (0 <= index < self.total_packets):
            return None
        with open(self.path, "rb") as f:
            f.seek(index * self.packet_size + self.sync_offset)
            data = f.read(188)
        if len(data) < 188:
            return None
        return data

    def _render_packet(self, index: int):
        pkt = self._read_packet(index)
        if pkt is None:
            messagebox.showinfo("パケットビューア", "そのパケットは存在しません。")
            return
        self.current_index = index
        self.packet_num_var.set(str(index))
        self.byte_pos_var.set(str(index * self.packet_size))
        self.fields = mpegts.parse_packet_fields(pkt)
        self._render_fields()
        self._render_hex(pkt)

    def _go_to_packet(self):
        try:
            idx = int(self.packet_num_var.get())
        except ValueError:
            return
        self._render_packet(idx)

    def _go_to_byte(self):
        try:
            byte_pos = int(self.byte_pos_var.get())
        except ValueError:
            return
        self._render_packet(byte_pos // self.packet_size)

    def _prev_packet(self):
        self._render_packet(max(0, self.current_index - 1))

    def _next_packet(self):
        self._render_packet(self.current_index + 1)

    def _find_pid(self, direction: int):
        if self.path is None:
            return
        pid_str = self.pid_filter_var.get().strip()
        if not pid_str:
            messagebox.showinfo("パケットビューア", "PIDを16進数で入力してください(例: 100)。")
            return
        try:
            target_pid = int(pid_str, 16)
        except ValueError:
            messagebox.showerror("パケットビューア", "PIDは16進数で入力してください(例: 100)。")
            return
        idx = self.current_index + direction
        while 0 <= idx < self.total_packets:
            pkt = self._read_packet(idx)
            if pkt is not None:
                pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
                if pid == target_pid:
                    self._render_packet(idx)
                    return
            idx += direction
        messagebox.showinfo("パケットビューア", "見つかりませんでした。")

    # --------------------------------------------------------------- 描画
    def _render_fields(self):
        for _name, w in self._field_rows:
            w.destroy()
        self._field_rows.clear()

        def add_row(label_text: str, byte_range: Optional[tuple[int, int]]):
            row = ttk.Frame(self.fields_frame)
            row.pack(fill="x", anchor="w")
            lbl = ttk.Label(row, text=label_text, cursor="hand2" if byte_range else "")
            lbl.pack(anchor="w")
            if byte_range:
                lbl.bind("<Button-1>", lambda e, br=byte_range: self._highlight_range(br))
            self._field_rows.append((label_text, row))

        f = self.fields
        add_row(f"同期バイト: 0x{f['sync_byte']['value']:02X}", f['sync_byte']['bytes'])
        add_row(f"転送エラー指標(TEI): {f['tei']['value']}", f['tei']['bytes'])
        add_row(f"ペイロード開始指標(PUSI): {f['pusi']['value']}", f['pusi']['bytes'])
        add_row(f"転送優先度: {f['priority']['value']}", f['priority']['bytes'])
        add_row(f"PID: 0x{f['pid']['value']:04X} ({f['pid']['value']})", f['pid']['bytes'])
        add_row(f"スクランブル制御: {f['scrambling_control']['value']}", f['scrambling_control']['bytes'])
        add_row(f"アダプテーション制御: {f['adaptation_field_control']['value']} "
                f"({f['adaptation_field_control']['desc']})", f['adaptation_field_control']['bytes'])
        add_row(f"連続性カウンタ(CC): {f['continuity_counter']['value']}", f['continuity_counter']['bytes'])

        af = f.get("adaptation_field")
        if af:
            add_row(f"[アダプテーションフィールド] 長さ: {af['length']}", af['bytes'])
            if "discontinuity_indicator" in af:
                add_row(f"  不連続指標: {af['discontinuity_indicator']}", af.get('flags_bytes'))
                add_row(f"  ランダムアクセス指標: {af['random_access_indicator']}", af.get('flags_bytes'))
                add_row(f"  PCRフラグ: {af['pcr_flag']}", af.get('flags_bytes'))
                if "pcr" in af:
                    pcr = af["pcr"]
                    seconds = pcr / 27_000_000
                    add_row(f"  PCR: {pcr} (27MHz) = {seconds:.3f}秒", af.get('pcr_bytes'))

        payload = f.get("payload")
        if payload:
            add_row(f"ペイロード範囲: {payload['bytes'][0]}-{payload['bytes'][1]}", payload['bytes'])
        if "pes_pts" in f:
            pts = f["pes_pts"]["value"]
            add_row(f"PES PTS: {pts} (90kHz) = {pts/90000:.3f}秒", payload['bytes'] if payload else None)

    def _render_hex(self, pkt: bytes):
        self.hex_text.config(state="normal")
        self.hex_text.delete("1.0", "end")
        self._hex_positions = render_hex_dump(self.hex_text, pkt)

        # 種別ごとの下地の色付け(TSヘッダ/アダプテーションフィールド/ペイロード)
        self._tag_bytes("header", (0, 4))
        af = self.fields.get("adaptation_field")
        if af:
            self._tag_bytes("adaptation", (af["bytes"][0], af["bytes"][0] + 1 + af["length"]))
        payload = self.fields.get("payload")
        if payload:
            self._tag_bytes("payload", payload["bytes"])

        self.hex_text.config(state="disabled")

    def _tag_bytes(self, tag: str, byte_range: tuple[int, int]):
        start, end = byte_range
        for i in range(start, min(end, 188)):
            pos = self._hex_positions.get(i)
            if pos:
                line, c0, c1 = pos
                self.hex_text.tag_add(tag, f"{line}.{c0}", f"{line}.{c1}")

    def _highlight_range(self, byte_range: tuple[int, int]):
        self.hex_text.tag_remove("select", "1.0", "end")
        self._tag_bytes("select", byte_range)
