# サードパーティ ソフトウェア・技術について

TSAnalyzeCutMerge 本体(このリポジトリのPythonソースコード)は MIT License で公開していますが、動作には以下の外部ツール・ライブラリを利用(または呼び出し)しています。それぞれ別のライセンス・利用条件が適用されるため、このファイルにまとめます。

## 実行に必要な外部ツール(同梱していません・各自入手してください)

### TsSplitter

- 本ツールから外部プロセスとして呼び出す実行ファイルです。
- 配布物(TsSplitter.txt等)を確認しましたが、著作権表記やライセンス・再配布条件を明記した記述は見つかりませんでした。作者名も公開されておらず、DTV関連ツール配布文化の中で「Ver1.26」として流通していたものです。
- ライセンス・著作者ともに不明なソフトウェアを無断で同梱・再配布することは避けるため、**本リポジトリには含めていません**。お手数ですが、各自の責任でDTV関係ファイル置き場から入手し、設定画面でパスを指定してください。

### rplsinfo

- 作者による `rplsinfo.txt` に「本ツールの再配布に制限はありませんので、自由に再配布してくださって構いません。」「ソースを改変したバージョン等を公開される場合は、作者への連絡・許可は必要ありません。」と明記されています。
- この記載に基づき、**Release配布物には rplsinfo.exe と同梱の rplsinfo.txt(原文のまま)を含めています**。改変は一切行っていません。
- 本ツールの著作権は原作者に帰属します。

### ffmpeg(任意・サムネイル生成機能でのみ使用)

- サムネイル生成はオプション機能で、システムにインストール済みの ffmpeg を検出して呼び出すだけです。**ffmpeg本体は同梱していません**。
- ffmpegはビルド構成によりGPL/LGPL等のライセンスが適用されます。各自 [ffmpeg.org](https://ffmpeg.org/) 等から入手してください。ffmpegが無くても分割・結合等の主要機能は動作します(サムネイルのみ無効になります)。

## Pythonライブラリ(release版exeに同梱)

### tkinterdnd2

- MIT License, Copyright (c) 2020 Philippe Gagné
- ドラッグ&ドロップ機能([TkinterDnD2](https://github.com/Eliav2/tkinterdnd2))に使用。内部で [tkdnd](https://github.com/petasis/tkdnd)(BSDライクライセンス)を利用しています。
- release版exeに同梱されるため、MITライセンスの著作権表示をここに記載します。

```
MIT License

Copyright (c) 2020 Philippe Gagné

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### PyInstaller

- release版exeのビルドに使用。PyInstaller自体はGPLですが、「Bootloader License Exception」により、PyInstallerでビルドしたアプリケーションを任意のライセンス(本プロジェクトの場合はMIT)で配布することが明示的に許可されています。詳細: https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt

## 参考にさせていただいたプロジェクト

コードはコピーしていませんが、アイデアや設計を参考にさせていただきました。

- **[TSCutter.GUI](https://github.com/nilaoda/TSCutter.GUI)**(nilaoda 作): キーフレーム精密カットの発想を参考にさせていただきました。実装は MPEG-2 (ISO/IEC 13818-2) の公開仕様から独自に書いています([mpegts.py](ts_tools/mpegts.py))。
- **[TSMerge](https://github.com/nilaoda/TSMerge)**(nilaoda 作): 重複データを検出して結合するアルゴリズムの発想を参考にさせていただき、Pythonで実装しました([overlap.py](ts_tools/overlap.py))。
