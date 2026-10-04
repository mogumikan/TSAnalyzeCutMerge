"""再生修復(PMT補完)機能。

背景(実データで確認した不具合): D-VHS等の複数チャンネル混在ファイルをTsSplitterで
粗分割する際、事前のPID保持対象の洗い出しが不十分だと、PATが指すPMT(Program Map
Table)のセクションそのものが出力から丸ごと欠落することがある(詳しくは
mpegts.scan_all_pmt_pids のコメントを参照)。映像/音声のバイト列自体は生きていても、
対応するPMTが無いとプレーヤー(特にTVTestのようにPAT/PMTを前提に動くもの)はPIDの
意味(どれが映像でどれが音声か)を特定できず再生できない。

本来はTsSplitterでの分割時にPMTを欠落させないことが正攻法(mpegts.pyの修正済み)だが、
既に出力してしまい元ファイルを削除済みなどで再分割できない場合のために、出来上がって
しまったファイルを直接診断・修復する機能をここに用意する。

注意: 元のPMTの内容を正確に復元できるわけではない(元のPMTは失われているため)。
ファイル自身に実際に含まれる映像/音声等のPIDを、PESヘッダ(stream_id)から推測して
最小限のPMTを合成する、あくまでベストエフォートの修復である。映像/音声のデータ自体は
一切変更・削除しない(合成したPMTパケットを追加で挿入するのみ)。
"""
from __future__ import annotations

import collections
import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import mpegts

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]   # (ラベル, 現在バイト位置, 全体バイト数)

# PAT/CAT/NIT/SDT/EIT/RST/TDT・TOT/SIT(パーシャルTS)/NULL: どれも「番組の
# 映像/音声そのもの」ではないシステム上のPIDなので、修復候補からは除外する。
SYSTEM_PIDS = {0x0000, 0x0001, 0x0010, 0x0011, 0x0012, 0x0013, 0x0014, 0x001E, 0x001F, 0x1FFF}

KIND_LABEL = {"video": "映像", "audio": "音声", "unknown": "不明(データ)"}
KIND_STREAM_TYPE = {"video": 0x02, "audio": 0x0F, "unknown": 0x06}   # 合成PMTに書く stream_type


def _classify_pes(payload: bytes) -> Optional[str]:
    """PESパケットの先頭(start_code + stream_id)から映像/音声を推測する
    (ISO/IEC 13818-1 Table 2-18)。判定できなければNone。"""
    if len(payload) < 4 or payload[0] != 0x00 or payload[1] != 0x00 or payload[2] != 0x01:
        return None
    stream_id = payload[3]
    if 0xE0 <= stream_id <= 0xEF:
        return "video"
    if 0xC0 <= stream_id <= 0xDF:
        return "audio"
    if stream_id == 0xBD:   # private_stream_1: 多くの場合AC-3等の音声に使われる
        return "audio"
    return None


def _pat_program_pmt_pairs(sec: bytes) -> list:
    """PATのセクション本体から (program_number, PMT PID) のペア一覧を返す
    (program_number=0のNIT行は除く)。"""
    if len(sec) < 8:
        return []
    sec_len = ((sec[1] & 0x0f) << 8) | sec[2]
    body = sec[8:3 + sec_len - 4]
    out = []
    i = 0
    while i + 4 <= len(body):
        prog_num = (body[i] << 8) | body[i + 1]
        pid = ((body[i + 2] & 0x1f) << 8) | body[i + 3]
        if prog_num != 0:
            out.append((prog_num, pid))
        i += 4
    return out


@dataclass
class ProgramStatus:
    program_number: int
    pmt_pid: int
    pmt_present: bool
    pmt_packet_count: int


@dataclass
class OrphanPid:
    """正常なPMTのどれにも属さない(=行き場を失った)、映像/音声らしきPID。
    PMTが欠落したプログラムの中身である可能性が高い、修復の材料。"""
    pid: int
    kind: str   # "video" / "audio" / "unknown"
    packet_count: int


@dataclass
class DiagnoseResult:
    pat_found: bool
    programs: list = field(default_factory=list)          # list[ProgramStatus]
    orphan_pids: list = field(default_factory=list)        # list[OrphanPid] (重要な順)
    likely_pcr_pid: Optional[int] = None
    total_packets: int = 0

    @property
    def has_missing_pmt(self) -> bool:
        return any(not p.pmt_present for p in self.programs)


def diagnose(path: str, packet_size: int, sync_offset: int,
             on_progress: Optional[Callable[[int, int], None]] = None,
             should_cancel: Optional[Callable[[], bool]] = None,
             progress_every: int = 500_000,
             min_orphan_packets: int = 30) -> DiagnoseResult:
    """ファイルを1回走査し、PATが指す各プログラムのPMTが実際に存在するかを診断する。
    PMTが欠落しているプログラムが1つでもあれば、正常などのPMTにも属さない
    映像/音声らしきPIDを抽出しておく(repair_missing_pmtでの修復材料になる)。"""
    size = os.path.getsize(path)
    program_pmt_map: dict = {}
    known_pmt_pids: set = set()
    pmt_pid_counts: collections.Counter = collections.Counter()
    working_elementary_pids: set = set()
    all_pid_counts: collections.Counter = collections.Counter()
    pcr_flag_counts: collections.Counter = collections.Counter()
    classified: dict = {}
    pat_buf: Optional[bytearray] = None
    pmt_bufs: dict = {}
    pat_found = False
    n = 0

    for pos, pkt in mpegts._iter_packets(path, packet_size, sync_offset):
        n += 1
        if n % progress_every == 0:
            if should_cancel is not None and should_cancel():
                raise mpegts.ScanCancelled()
            if on_progress is not None:
                on_progress(pos, size)

        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        all_pid_counts[pid] += 1

        afc = (pkt[3] >> 4) & 0x3
        if afc in (2, 3) and len(pkt) > 5:
            adapt_len = pkt[4]
            if adapt_len >= 1 and (pkt[5] & 0x10):   # PCR_flag
                pcr_flag_counts[pid] += 1

        _, pusi, payload = mpegts._payload(pkt)
        if not payload:
            continue

        if pid == 0x0000:
            pat_found = True
            pat_buf = mpegts._feed_section_buf(pat_buf, pusi, payload)
            if pat_buf is not None and len(pat_buf) >= 3:
                sec_len = ((pat_buf[1] & 0x0f) << 8) | pat_buf[2]
                total_len = 3 + sec_len
                if len(pat_buf) >= total_len:
                    sec = bytes(pat_buf[:total_len])
                    pat_buf = None
                    for prog_num, pmt_pid in _pat_program_pmt_pairs(sec):
                        program_pmt_map[prog_num] = pmt_pid
                        known_pmt_pids.add(pmt_pid)
            continue

        if pid in known_pmt_pids:
            pmt_pid_counts[pid] += 1
            buf = mpegts._feed_section_buf(pmt_bufs.get(pid), pusi, payload)
            if buf is not None and len(buf) >= 3:
                sec_len = ((buf[1] & 0x0f) << 8) | buf[2]
                total_len = 3 + sec_len
                if len(buf) >= total_len:
                    _pcr_pid, epids = mpegts._pmt_section_pids(bytes(buf[:total_len]))
                    working_elementary_pids.update(epids)
                    buf = None
            pmt_bufs[pid] = buf
            continue

        if pusi and pid not in classified:
            kind = _classify_pes(payload)
            if kind is not None:
                classified[pid] = kind

    if on_progress is not None:
        on_progress(size, size)

    programs = []
    for prog_num, pmt_pid in sorted(program_pmt_map.items()):
        cnt = pmt_pid_counts.get(pmt_pid, 0)
        programs.append(ProgramStatus(prog_num, pmt_pid, cnt > 0, cnt))

    orphan: list = []
    if any(not p.pmt_present for p in programs):
        for pid, cnt in all_pid_counts.items():
            if pid in SYSTEM_PIDS or pid in known_pmt_pids or pid in working_elementary_pids:
                continue
            if cnt < min_orphan_packets:
                continue
            kind = classified.get(pid, "unknown")
            orphan.append(OrphanPid(pid, kind, cnt))
        kind_order = {"video": 0, "audio": 1, "unknown": 2}
        orphan.sort(key=lambda o: (kind_order.get(o.kind, 9), -o.packet_count))

    likely_pcr_pid = pcr_flag_counts.most_common(1)[0][0] if pcr_flag_counts else None

    return DiagnoseResult(pat_found=pat_found, programs=programs, orphan_pids=orphan,
                           likely_pcr_pid=likely_pcr_pid, total_packets=n)


def _crc32_mpeg2(data: bytes) -> int:
    """MPEG-2セクション用CRC32 (ISO/IEC 13818-1 Annex A: 生成多項式0x04C11DB7,
    初期値0xFFFFFFFF, 反転なし, 出力XORなし)。zlib.crc32とはアルゴリズムが異なる
    (zlibはCRC-32/ISO-HDLC=反転あり)ため自前で計算する。"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= (byte << 24) & 0xFFFFFFFF
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def _build_pmt_section(program_number: int, pcr_pid: int, streams: list, version: int = 0) -> bytes:
    """ISO/IEC 13818-1 Table 2-33 (Program Map Table)に従い、記述子無しの
    最小限のPMTセクションをCRC32付きで組み立てる。streamsは(pid, stream_type)のリスト。"""
    body = bytearray()
    body += program_number.to_bytes(2, "big")
    body.append(0xC0 | ((version & 0x1F) << 1) | 0x01)   # reserved'11'+version(5)+current_next=1
    body.append(0x00)   # section_number
    body.append(0x00)   # last_section_number
    body.append(0xE0 | ((pcr_pid >> 8) & 0x1F))
    body.append(pcr_pid & 0xFF)
    body.append(0xF0)   # reserved'1111' + program_info_length上位4bit=0
    body.append(0x00)   # program_info_length下位8bit=0 (記述子無し)
    for pid, stream_type in streams:
        body.append(stream_type & 0xFF)
        body.append(0xE0 | ((pid >> 8) & 0x1F))
        body.append(pid & 0xFF)
        body.append(0xF0)   # ES_info_length = 0 (上位4bit)
        body.append(0x00)   # ES_info_length = 0 (下位8bit)

    section_length = len(body) + 4   # +4 = 末尾に付くCRC32の分
    head = bytearray()
    head.append(0x02)   # table_id = 0x02 (PMT)
    head.append(0xB0 | ((section_length >> 8) & 0x0F))   # section_syntax_indicator=1,'0',reserved'11'
    head.append(section_length & 0xFF)

    section = bytes(head) + bytes(body)
    crc = _crc32_mpeg2(section)
    return section + crc.to_bytes(4, "big")


def _pack_section_into_packets(section: bytes, pid: int, start_cc: int):
    """PSIセクションを188バイトTSパケット(pointer_field付き)に詰める。
    184バイトを超える場合は複数パケットに分割する(本ツールが合成する最小限の
    PMTは通常1パケットに収まる)。戻り値は (パケットのリスト, 次に使うcontinuity_counter)。"""
    packets = []
    cc = start_cc
    data = bytes([0x00]) + section   # 先頭にpointer_field=0
    first = True
    while data:
        chunk = data[:184]
        data = data[184:]
        pkt = bytearray(188)
        pkt[0] = 0x47
        pkt[1] = (0x40 if first else 0x00) | ((pid >> 8) & 0x1F)   # PUSIは先頭パケットのみ
        pkt[2] = pid & 0xFF
        pkt[3] = 0x10 | (cc & 0x0F)   # adaptation_field_control='01'(payloadのみ)
        pkt[4:4 + len(chunk)] = chunk
        for i in range(4 + len(chunk), 188):
            pkt[i] = 0xFF   # スタッフィング(table_id=0xFFは「以降は読むな」の意味を兼ねる)
        packets.append(bytes(pkt))
        cc = (cc + 1) & 0x0F
        first = False
    return packets, cc


@dataclass
class RepairedProgramInfo:
    program_number: int
    pmt_pid: int
    streams: list   # list[(pid, kind)]


@dataclass
class RepairPmtReport:
    programs_examined: int
    programs_repaired: int
    repaired_info: list   # list[RepairedProgramInfo]
    bytes_written: int
    skipped_reason: str = ""


def _copy_plain(src_path: str, out_path: str, on_progress: Optional[ProgressFn],
                 should_cancel: Optional[Callable[[], bool]]) -> None:
    size = os.path.getsize(src_path)
    if on_progress is None and should_cancel is None:
        shutil.copyfile(src_path, out_path)
        return
    written = 0
    with open(src_path, "rb") as fin, open(out_path, "wb") as fout:
        while True:
            if should_cancel is not None and should_cancel():
                raise mpegts.ScanCancelled()
            chunk = fin.read(8 * 1024 * 1024)
            if not chunk:
                break
            fout.write(chunk)
            written += len(chunk)
            if on_progress is not None:
                on_progress("コピー中(修復不要)", written, size)


def repair_missing_pmt(src_path: str, out_path: str, packet_size: int, sync_offset: int,
                        insert_interval_bytes: int = 8 * 1024 * 1024,
                        on_log: Optional[LogFn] = None,
                        on_progress: Optional[ProgressFn] = None,
                        should_cancel: Optional[Callable[[], bool]] = None) -> RepairPmtReport:
    """src_pathを診断し、PMTが欠落しているプログラムがあれば、ファイル自身に残る
    映像/音声らしきPIDから最小限のPMTを合成してout_pathへ書き出す(一定間隔で
    繰り返し挿入するので、プレーヤーがファイルのどこから再生を始めても検出できる)。
    元データ(映像/音声等の実バイト列)は一切変更しない。

    should_cancel()がTrueを返すと mpegts.ScanCancelled を送出して中断する。"""
    on_log = on_log or (lambda s: None)

    def diag_progress(cur, total):
        if on_progress:
            on_progress("診断中(PAT/PMTの構成を確認しています)", cur, total)

    on_log("  診断中(PAT/PMTの構成を確認しています)...")
    diag = diagnose(src_path, packet_size, sync_offset,
                     on_progress=diag_progress if on_progress else None,
                     should_cancel=should_cancel)

    if not diag.pat_found:
        on_log("  ! PAT自体が見つかりませんでした。この機能では修復できません(コピーのみ行います)。")
        _copy_plain(src_path, out_path, on_progress, should_cancel)
        return RepairPmtReport(len(diag.programs), 0, [], os.path.getsize(out_path), "PATが見つかりません")

    broken = [p for p in diag.programs if not p.pmt_present]
    if not broken:
        on_log("  欠落したPMTは見つかりませんでした(このファイルは修復不要のようです)。")
        _copy_plain(src_path, out_path, on_progress, should_cancel)
        return RepairPmtReport(len(diag.programs), 0, [], os.path.getsize(out_path), "PMTの欠落なし")

    streams = [(o.pid, KIND_STREAM_TYPE.get(o.kind, 0x06)) for o in diag.orphan_pids]
    if not streams:
        on_log("  ! PMTは欠落していますが、手がかりになる映像/音声らしきPIDが見つからず"
               "修復できませんでした(コピーのみ行います)。")
        _copy_plain(src_path, out_path, on_progress, should_cancel)
        return RepairPmtReport(len(diag.programs), 0, [], os.path.getsize(out_path),
                                "映像/音声PIDの手がかりが無い")

    pcr_pid = diag.likely_pcr_pid if diag.likely_pcr_pid is not None else streams[0][0]

    section_by_pmt_pid: dict = {}
    repaired_info: list = []
    for prog in broken:
        sec = _build_pmt_section(prog.program_number, pcr_pid, streams)
        section_by_pmt_pid[prog.pmt_pid] = sec
        repaired_info.append(RepairedProgramInfo(
            program_number=prog.program_number, pmt_pid=prog.pmt_pid,
            streams=[(o.pid, o.kind) for o in diag.orphan_pids],
        ))
        on_log(f"  program_number={prog.program_number} (PMT PID 0x{prog.pmt_pid:04x}) のPMTを"
               f"合成します (PCR PID: 0x{pcr_pid:04x}):")
        for o in diag.orphan_pids:
            on_log(f"    PID 0x{o.pid:04x}: {KIND_LABEL.get(o.kind, '不明')} "
                   f"({o.packet_count:,}パケット確認)")

    on_log("  ※ この修復は元のPMTを正確に復元するものではありません。ファイルに実際に"
           "残っている映像/音声等のPIDから最小限のPMTを合成する、あくまでベストエフォート"
           "の処置です(映像/音声のデータ自体は一切変更しません)。")

    cc_state = {pid: 0 for pid in section_by_pmt_pid}
    is_192 = (packet_size == 192)

    def _wrap(pkt188: bytes) -> bytes:
        return (b"\x00\x00\x00\x00" + pkt188) if is_192 else pkt188

    def write_pmt_set(fout) -> None:
        for pmt_pid, sec in section_by_pmt_pid.items():
            pkts, next_cc = _pack_section_into_packets(sec, pmt_pid, cc_state[pmt_pid])
            cc_state[pmt_pid] = next_cc
            for p in pkts:
                fout.write(_wrap(p))

    size = os.path.getsize(src_path)
    write_label = f"修復ファイルを書き出し中: {os.path.basename(out_path)}"
    written = 0
    with open(src_path, "rb") as fin, open(out_path, "wb") as fout:
        write_pmt_set(fout)   # 先頭にも1回挿入(冒頭から再生を始めるプレーヤーがすぐ検出できるように)
        pos = 0
        next_insert_at = insert_interval_bytes
        chunk_size = 8 * 1024 * 1024
        chunk_size = (chunk_size // packet_size) * packet_size
        while True:
            if should_cancel is not None and should_cancel():
                raise mpegts.ScanCancelled()
            if on_progress is not None:
                on_progress(write_label, pos, size)
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            fout.write(chunk)
            pos += len(chunk)
            written += len(chunk)
            if pos >= next_insert_at:
                write_pmt_set(fout)
                next_insert_at += insert_interval_bytes
    if on_progress is not None:
        on_progress(write_label, size, size)

    on_log(f"  完了: {os.path.basename(out_path)} (元データ{written:,}バイト + 合成PMT)")
    return RepairPmtReport(len(diag.programs), len(broken), repaired_info, os.path.getsize(out_path))
