"""詳細解析モードの「番組候補一覧」レビュー画面(TMPGEnc風のチェックリストUI)。"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from . import probe
from .analyzer import ProgramCandidate
from .pipeline import CoarseSegment

UNKNOWN_PREFIX = "(情報不明"


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_inner_configure(self, _e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, e):
        self.canvas.itemconfig(self._win, width=e.width)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()


Item = tuple[CoarseSegment, ProgramCandidate]


class ReviewWindow(tk.Toplevel):
    """解析結果(候補一覧)を表示し、ユーザーに抽出対象・グループ・結合順を選ばせる画面。"""

    def __init__(self, master, segments: list[CoarseSegment],
                 on_extract: Callable[[list[CoarseSegment], bool], None]):
        super().__init__(master)
        self.title("番組候補の確認 - 抽出する項目を選択してください")
        self.geometry("940x720")
        self.segments = segments
        self.on_extract = on_extract
        self._images: list[tk.PhotoImage] = []
        self._vars: dict[int, tk.BooleanVar] = {}   # id(candidate) -> BooleanVar
        self.group_mode = tk.StringVar(value="station")   # "station" or "segment"
        self.merge_var = tk.BooleanVar(value=False)

        # グループ編集の唯一の状態(放送局ごと表示・結合順・グループ移動はすべてこれを介す)
        self.group_order: list[str] = []
        self.group_lists: dict[str, list[Item]] = {}
        self._init_groups()

        self._build_ui()
        self._populate()

    # --------------------------------------------------------- グループ状態
    def _init_groups(self):
        unknown_seq = 0
        for seg in self.segments:
            for c in seg.candidates:
                station = (c.info.get("station") or "").strip()
                if not station:
                    unknown_seq += 1
                    key = f"{UNKNOWN_PREFIX} #{unknown_seq})"
                else:
                    key = station
                self._add_to_group(key, (seg, c))

    def _add_to_group(self, key: str, item: Item):
        if key not in self.group_lists:
            self.group_lists[key] = []
            self.group_order.append(key)
        self.group_lists[key].append(item)

    def _remove_from_groups(self, cand: ProgramCandidate) -> Optional[str]:
        for key in list(self.group_lists.keys()):
            lst = self.group_lists[key]
            for i, (_seg, c) in enumerate(lst):
                if c is cand:
                    del lst[i]
                    if not lst:
                        del self.group_lists[key]
                        self.group_order.remove(key)
                    return key
        return None

    def _known_group_names(self) -> list[str]:
        return [k for k in self.group_order if not k.startswith(UNKNOWN_PREFIX)]

    def _move_item_to_group(self, seg: CoarseSegment, c: ProgramCandidate, new_key: str):
        new_key = new_key.strip()
        if not new_key:
            return
        self._remove_from_groups(c)
        self._add_to_group(new_key, (seg, c))
        self._populate()

    def _move_within_group(self, key: str, index: int, delta: int):
        lst = self.group_lists.get(key)
        if not lst:
            return
        j = index + delta
        if 0 <= j < len(lst):
            lst[index], lst[j] = lst[j], lst[index]
            self._populate()

    # --------------------------------------------------------------- UI組立
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="表示:").pack(side="left")
        ttk.Radiobutton(top, text="放送局ごと", variable=self.group_mode, value="station",
                         command=self._populate).pack(side="left")
        ttk.Radiobutton(top, text="区間ごと", variable=self.group_mode, value="segment",
                         command=self._populate).pack(side="left")
        ttk.Button(top, text="全選択", command=lambda: self._set_all(True)).pack(side="right", padx=2)
        ttk.Button(top, text="全解除", command=lambda: self._set_all(False)).pack(side="right", padx=2)

        hint = ttk.Label(self, text="各項目のチェックを確認し、不要なものは外してください。"
                                     "「放送局ごと」表示では、右側のプルダウンで所属グループの変更"
                                     "(過渡的な断片を任意の局グループへ入れる、新しいグループ名を作る等)、"
                                     "▲▼で結合順の入れ替えができます。",
                          foreground="#555", wraplength=900)
        hint.pack(anchor="w", padx=8)

        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True, padx=8, pady=4)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=4)
        ttk.Checkbutton(bottom, text="同じグループの選択項目を(上から下の順に)1つのファイルへ結合する",
                         variable=self.merge_var).pack(anchor="w")
        ttk.Label(bottom, text="※ 結合の継ぎ目にタイムスタンプの不連続が残る場合、プレーヤーによっては"
                               "一瞬再生が乱れることがあります。重複データは自動検出して除去します。",
                  foreground="#777").pack(anchor="w")

        bottom2 = ttk.Frame(self)
        bottom2.pack(fill="x", padx=8, pady=8)
        self.count_label = ttk.Label(bottom2, text="")
        self.count_label.pack(side="left")
        ttk.Button(bottom2, text="キャンセル", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bottom2, text="選択項目を抽出", command=self._extract).pack(side="right", padx=4)

    def _placeholder_image(self) -> tk.PhotoImage:
        img = tk.PhotoImage(width=160, height=120)
        img.put("#333333", to=(0, 0, 160, 120))
        self._images.append(img)
        return img

    def _var_for(self, c: ProgramCandidate) -> tk.BooleanVar:
        v = self._vars.get(id(c))
        if v is None:
            v = tk.BooleanVar(value=c.selected)
            self._vars[id(c)] = v
        return v

    def _populate(self):
        self.scroll.clear()
        self._images.clear()
        parent = self.scroll.inner
        station_mode = self.group_mode.get() == "station"

        if station_mode:
            groups_iter = [(key, self.group_lists[key]) for key in self.group_order]
        else:
            groups_iter = [(os.path.basename(seg.path), [(seg, c) for c in seg.candidates])
                           for seg in self.segments]

        for header_text, items in groups_iter:
            total_size = sum(c.size for _, c in items)
            header = ttk.Label(parent, text=f"■ {header_text}  ({len(items)}件, {probe.human_size(total_size)})",
                                font=("", 10, "bold"))
            header.pack(anchor="w", pady=(10, 2), padx=4)

            for row_idx, (seg, c) in enumerate(items):
                row = ttk.Frame(parent, relief="groove", borderwidth=1)
                row.pack(fill="x", padx=4, pady=2)

                var = self._var_for(c)
                ttk.Checkbutton(row, variable=var, command=self._update_count).pack(side="left", padx=4)

                if c.thumbnail_path and os.path.exists(c.thumbnail_path):
                    try:
                        img = tk.PhotoImage(file=c.thumbnail_path)
                    except tk.TclError:
                        img = self._placeholder_image()
                else:
                    img = self._placeholder_image()
                self._images.append(img)
                ttk.Label(row, image=img).pack(side="left", padx=4, pady=4)

                info = c.info or {}
                if info:
                    title = info.get("title") or "(タイトル不明)"
                    line1 = f"{info.get('date','')} {info.get('start_time','')}  " \
                            f"{info.get('station','')} {info.get('channel','')}"
                    line2 = title
                    line3 = f"予定尺: {info.get('duration','?')}  " \
                            f"[出典: {os.path.basename(seg.path)} {c.start_pct}-{c.end_pct}%]"
                else:
                    line1 = "(番組情報を取得できませんでした)"
                    line2 = "過渡的な断片の可能性があります"
                    line3 = f"[出典: {os.path.basename(seg.path)} {c.start_pct}-{c.end_pct}%]"

                text_frame = ttk.Frame(row)
                text_frame.pack(side="left", fill="both", expand=True, padx=6)
                ttk.Label(text_frame, text=line1, foreground="#555").pack(anchor="w")
                ttk.Label(text_frame, text=line2, font=("", 10, "bold"), wraplength=420).pack(anchor="w")
                ttk.Label(text_frame, text=line3, foreground="#999").pack(anchor="w")

                ttk.Label(row, text=f"{probe.human_size(c.size)}",
                          foreground="#777", justify="right").pack(side="right", padx=8)

                if station_mode:
                    ctrl = ttk.Frame(row)
                    ctrl.pack(side="right", padx=6)
                    updown = ttk.Frame(ctrl)
                    updown.pack(side="top")
                    ttk.Button(updown, text="▲", width=2,
                               command=lambda k=header_text, i=row_idx: self._move_within_group(k, i, -1)
                               ).pack(side="left")
                    ttk.Button(updown, text="▼", width=2,
                               command=lambda k=header_text, i=row_idx: self._move_within_group(k, i, 1)
                               ).pack(side="left")
                    combo = ttk.Combobox(ctrl, width=16, values=self._known_group_names())
                    combo.set(header_text)
                    combo.pack(side="top", pady=(2, 0))
                    combo.bind("<<ComboboxSelected>>",
                               lambda e, s=seg, cc=c, cb=combo: self._move_item_to_group(s, cc, cb.get()))
                    combo.bind("<Return>",
                               lambda e, s=seg, cc=c, cb=combo: self._move_item_to_group(s, cc, cb.get()))

        self._update_count()

    def _set_all(self, value: bool):
        for v in self._vars.values():
            v.set(value)
        self._update_count()

    def _update_count(self):
        n = sum(1 for v in self._vars.values() if v.get())
        self.count_label.config(text=f"選択中: {n} / {len(self._vars)} 件")

    def _apply_overrides(self):
        """group_lists/群内の順番を、各候補のgroup_override/order_overrideへ反映する。"""
        for key, lst in self.group_lists.items():
            for idx, (_seg, c) in enumerate(lst):
                c.group_override = key
                c.order_override = idx

    def _extract(self):
        for seg in self.segments:
            for c in seg.candidates:
                var = self._vars.get(id(c))
                if var is not None:
                    c.selected = var.get()
        self._apply_overrides()
        merge = self.merge_var.get()
        self.destroy()
        self.on_extract(self.segments, merge)
