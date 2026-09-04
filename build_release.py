#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""非エンジニア向けのRelease配布物(スタンドアロンexe)をビルドするスクリプト。

やること:
  1. PyInstallerで main.py をスタンドアロンexeにビルドする
     (tkinterdnd2のネイティブファイル(tkdnd)も同梱するよう明示指定)。
  2. release/ フォルダを作り、exe・README・LICENSE・THIRD_PARTY_NOTICES を
     まとめる。
  3. rplsinfo(再配布許諾済み)がローカルに見つかれば、rplsinfo.exeと
     その説明書(rplsinfo.txt)をそのまま release/rplsinfo/ に同梱する。
     TsSplitterとffmpegはライセンス上同梱しないため、README内の案内に
     従って利用者が各自用意する必要がある。

使い方:
    python build_release.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "TSAnalyzeCutMerge"

sys.path.insert(0, str(APP_DIR))
from ts_tools import config  # noqa: E402


def run_pyinstaller() -> Path:
    print("=== PyInstallerでビルド中... ===")
    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onefile",
        "--windowed",
        "--collect-data", "tkinterdnd2",
        "--distpath", str(APP_DIR / "dist"),
        "--workpath", str(APP_DIR / "build"),
        "--specpath", str(APP_DIR),
        "--noconfirm",
        str(APP_DIR / "main.py"),
    ]
    subprocess.run(args, check=True, cwd=APP_DIR)
    exe_path = APP_DIR / "dist" / f"{APP_NAME}.exe"
    if not exe_path.exists():
        raise SystemExit(f"ビルド失敗: {exe_path} が見つかりません")
    print(f"ビルド完了: {exe_path}")
    return exe_path


def build_release_folder(exe_path: Path) -> Path:
    release_dir = APP_DIR / "release" / APP_NAME
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)

    shutil.copy2(exe_path, release_dir / f"{APP_NAME}.exe")
    for fn in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        src = APP_DIR / fn
        if src.exists():
            shutil.copy2(src, release_dir / fn)

    # rplsinfo(再配布OK)を同梱
    rplsinfo_exe = Path(config.DEFAULT_RPLSINFO_EXE)
    if rplsinfo_exe.exists():
        dst_dir = release_dir / "rplsinfo"
        dst_dir.mkdir(exist_ok=True)
        shutil.copy2(rplsinfo_exe, dst_dir / rplsinfo_exe.name)
        rplsinfo_txt = rplsinfo_exe.parent / "rplsinfo.txt"
        if rplsinfo_txt.exists():
            shutil.copy2(rplsinfo_txt, dst_dir / "rplsinfo.txt")
        print(f"rplsinfoを同梱しました: {dst_dir}")
    else:
        print(f"警告: rplsinfoが見つかりません({rplsinfo_exe})。同梱をスキップします。"
              "配布先で別途用意してもらう必要があります。")

    readme_first = release_dir / "はじめにお読みください.txt"
    readme_first.write_text(
        "TSAnalyzeCutMerge をご利用いただきありがとうございます。\n\n"
        "1. このフォルダの TSAnalyzeCutMerge.exe を実行してください。\n"
        "2. TsSplitter.exe は同梱していません(ライセンス不明のため)。"
        "各自入手し、起動後の「詳細オプション」タブでパスを指定してください。\n"
        "3. rplsinfo は同梱済みです(rplsinfoフォルダ内)。パスは自動設定されて"
        "いない場合、rplsinfo\\rplsinfo.exe を指定してください。\n"
        "4. サムネイル機能を使うには ffmpeg が必要です(任意、無くても分割・"
        "結合は動作します)。\n\n"
        "詳しい使い方は README.md をご覧ください。\n",
        encoding="utf-8",
    )

    return release_dir


def make_zip(release_dir: Path) -> Path:
    zip_path = release_dir.parent / f"{APP_NAME}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in release_dir.rglob("*"):
            zf.write(p, p.relative_to(release_dir.parent))
    print(f"配布用zipを作成しました: {zip_path}")
    return zip_path


if __name__ == "__main__":
    exe = run_pyinstaller()
    rel_dir = build_release_folder(exe)
    make_zip(rel_dir)
    print("\n完了しました。release/ フォルダの中身をご確認ください。")
