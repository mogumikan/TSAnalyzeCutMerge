"""MPEG-2 TSの標準仕様(公開情報)に基づく軽量パーサー。

PAT/PMTの解析と、映像PIDのGOP開始点(キーフレーム境界)検出を行う。
TMPGEnc等の商用ソフトの内部実装を参照・解析したものではなく、
MPEG-2 Systems(ISO/IEC 13818-1)の公開仕様にのみ基づく実装。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Callable, Optional

GOP_START_CODE = b"\x00\x00\x01\xb8"        # GOPヘッダ開始コード(MPEG-2, ISO/IEC 13818-2)
PICTURE_START_CODE = b"\x00\x00\x01\x00"    # ピクチャ開始コード(同上)
SEQUENCE_HEADER_CODE = b"\x00\x00\x01\xb3"  # シーケンスヘッダ開始コード(同上)


def _iter_packets_fh(f, packet_size: int, sync_offset: int, start_byte: int = 0,
                      max_bytes: Optional[int] = None):
    """_iter_packetsと同じだが、既に開いているファイルオブジェクトfを使い回す版。
    呼び出し側でopen/closeを行う(同じファイルから極めて多数の小範囲を読む際、
    1回ずつopen()するオーバーヘッドを避けるため)。"""
    start_byte = (start_byte // packet_size) * packet_size   # パケット境界に整列
    f.seek(start_byte)
    read_total = 0
    pos = start_byte
    while max_bytes is None or read_total < max_bytes:
        b = f.read(packet_size)
        if len(b) < packet_size:
            return
        read_total += packet_size
        if b[sync_offset] != 0x47:
            pos += packet_size
            continue
        yield pos, b[sync_offset:sync_offset + 188]
        pos += packet_size


def _iter_packets(path: str, packet_size: int, sync_offset: int, start_byte: int = 0,
                   max_bytes: Optional[int] = None):
    with open(path, "rb") as f:
        yield from _iter_packets_fh(f, packet_size, sync_offset, start_byte, max_bytes)


def _payload(pkt188: bytes) -> tuple[int, bool, bytes]:
    """(pid, payload_unit_start, payload_bytes) を返す。"""
    pid = ((pkt188[1] & 0x1f) << 8) | pkt188[2]
    pusi = (pkt188[1] & 0x40) != 0
    afc = (pkt188[3] >> 4) & 0x3
    start = 4
    if afc in (2, 3):
        adapt_len = pkt188[4]
        start = 5 + adapt_len
    if afc in (0, 2) or start > len(pkt188):
        return pid, pusi, b""
    return pid, pusi, pkt188[start:]


def parse_packet_fields(pkt188: bytes) -> dict:
    """188バイトのTSパケット1つを解析し、各フィールドの値とバイト範囲を返す
    (パケットビューア表示用)。ISO/IEC 13818-1 2.4.3.2/2.4.3.3/2.4.3.5の
    公開仕様のみに基づく。バイト範囲は (開始, 終了) のタプル(終了は含まない)。
    """
    f: dict = {}
    f["sync_byte"] = {"value": pkt188[0], "bytes": (0, 1), "desc": "同期バイト(常に0x47)"}
    f["tei"] = {"value": bool(pkt188[1] & 0x80), "bytes": (1, 2), "desc": "転送エラー指標"}
    f["pusi"] = {"value": bool(pkt188[1] & 0x40), "bytes": (1, 2), "desc": "ペイロード開始指標"}
    f["priority"] = {"value": bool(pkt188[1] & 0x20), "bytes": (1, 2), "desc": "転送優先度"}
    pid = ((pkt188[1] & 0x1f) << 8) | pkt188[2]
    f["pid"] = {"value": pid, "bytes": (1, 3), "desc": "PID"}
    scramble = (pkt188[3] >> 6) & 0x3
    f["scrambling_control"] = {"value": scramble, "bytes": (3, 4),
                                "desc": "スクランブル制御(0=非スクランブル)"}
    afc = (pkt188[3] >> 4) & 0x3
    afc_desc = {0: "予約(無効)", 1: "ペイロードのみ", 2: "アダプテーションのみ", 3: "アダプテーション+ペイロード"}
    f["adaptation_field_control"] = {"value": afc, "bytes": (3, 4), "desc": afc_desc.get(afc, "")}
    f["continuity_counter"] = {"value": pkt188[3] & 0x0F, "bytes": (3, 4), "desc": "連続性カウンタ(0-15)"}

    pos = 4
    if afc in (2, 3):
        adapt_len = pkt188[4]
        adapt_end = min(5 + adapt_len, len(pkt188))
        af: dict = {"length": adapt_len, "bytes": (4, 5)}
        if adapt_len > 0 and adapt_end > 5:
            flags = pkt188[5]
            af["discontinuity_indicator"] = bool(flags & 0x80)
            af["random_access_indicator"] = bool(flags & 0x40)
            af["elementary_stream_priority"] = bool(flags & 0x20)
            af["pcr_flag"] = bool(flags & 0x10)
            af["opcr_flag"] = bool(flags & 0x08)
            af["splicing_point_flag"] = bool(flags & 0x04)
            af["transport_private_data_flag"] = bool(flags & 0x02)
            af["adaptation_field_extension_flag"] = bool(flags & 0x01)
            af["flags_bytes"] = (5, 6)
            if af["pcr_flag"]:
                pcr = read_pcr(pkt188)
                if pcr is not None:
                    af["pcr"] = pcr
                    af["pcr_bytes"] = (6, 12)
        f["adaptation_field"] = af
        pos = adapt_end
    if afc in (1, 3):
        f["payload"] = {"bytes": (pos, len(pkt188)), "desc": "ペイロード"}
        payload = pkt188[pos:]
        if f.get("pusi", {}).get("value") and payload[:3] == b"\x00\x00\x01":
            pts = read_pts(payload)
            if pts is not None:
                f["pes_pts"] = {"value": pts, "desc": "PES PTS(90kHz)"}
    return f


def read_pcr(pkt188: bytes) -> Optional[int]:
    """パケットにPCRが含まれていれば27MHzカウント値(整数)を返す。無ければNone。

    アダプテーションフィールドの構造は ISO/IEC 13818-1 2.4.3.5 の公開仕様。
    """
    afc = (pkt188[3] >> 4) & 0x3
    if afc not in (2, 3):
        return None
    adapt_len = pkt188[4]
    if adapt_len < 1:
        return None
    flags = pkt188[5]
    if not (flags & 0x10):   # PCR_flag
        return None
    if adapt_len < 7:
        return None
    b = pkt188[6:12]
    base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
    ext = ((b[4] & 0x01) << 8) | b[5]
    return base * 300 + ext


def find_pcr_positions(path: str, packet_size: int, sync_offset: int, pcr_pid: int,
                        max_packets: Optional[int] = None) -> list[tuple[int, int]]:
    """(ファイル先頭からのバイト位置, PCR値)のリストを返す(PCRが現れる度に記録)。"""
    out = []
    n = 0
    for pos, pkt in _iter_packets(path, packet_size, sync_offset):
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pid == pcr_pid:
            pcr = read_pcr(pkt)
            if pcr is not None:
                out.append((pos, pcr))
        n += 1
        if max_packets is not None and n >= max_packets:
            break
    return out


def pcr_to_byte_offset(pcr_positions: list[tuple[int, int]], target_pcr: int) -> Optional[int]:
    """pcr_positions(昇順)の中からtarget_pcrに最も近い記録地点のバイト位置を返す。"""
    if not pcr_positions:
        return None
    pcrs = [p[1] for p in pcr_positions]
    i = bisect.bisect_left(pcrs, target_pcr)
    if i <= 0:
        return pcr_positions[0][0]
    if i >= len(pcrs):
        return pcr_positions[-1][0]
    before = pcr_positions[i - 1]
    after = pcr_positions[i]
    return before[0] if (target_pcr - before[1]) <= (after[1] - target_pcr) else after[0]


def interpolate_value_at_position(pairs: list[tuple[int, int]], target_pos: int) -> Optional[float]:
    """pairs([(バイト位置, 値)], 位置昇順)から、target_posにおける値を線形補間する。

    PCRはほぼ一定間隔で増加するため、隣接する2つのPCRサンプルの間は
    バイト位置に対してほぼ線形とみなせる。1回分のサンプル間隔(約100ms)
    より遥かに小さい欠損の位置を精密に特定するために使う。
    """
    if not pairs:
        return None
    positions = [p[0] for p in pairs]
    i = bisect.bisect_left(positions, target_pos)
    if i <= 0:
        return float(pairs[0][1])
    if i >= len(pairs):
        return float(pairs[-1][1])
    p0, p1 = pairs[i - 1], pairs[i]
    if p1[0] == p0[0]:
        return float(p0[1])
    frac = (target_pos - p0[0]) / (p1[0] - p0[0])
    return p0[1] + frac * (p1[1] - p0[1])


def interpolate_position_at_value(pairs: list[tuple[int, int]], target_value: float) -> Optional[int]:
    """interpolate_value_at_positionの逆: 値からバイト位置を線形補間する
    (pairsは値についても単調増加である必要がある。PCR/PTSはラップアラウンド
    が無い短い区間では単調増加なので問題ない)。"""
    if not pairs:
        return None
    values = [p[1] for p in pairs]
    i = bisect.bisect_left(values, target_value)
    if i <= 0:
        return pairs[0][0]
    if i >= len(pairs):
        return pairs[-1][0]
    p0, p1 = pairs[i - 1], pairs[i]
    if p1[1] == p0[1]:
        return p0[0]
    frac = (target_value - p0[1]) / (p1[1] - p0[1])
    return int(round(p0[0] + frac * (p1[0] - p0[0])))


@dataclass
class CCGap:
    pid: int
    pos_before: int     # ギャップ直前の正常パケットの先頭バイト位置
    pos_after: int      # ギャップ直後の正常パケットの先頭バイト位置
    cc_before: int
    cc_after: int
    pcr_before: Optional[int] = None   # 直近のPCR(同時刻の対応付け用、無ければNone)
    pcr_after: Optional[int] = None
    pts_before: Optional[int] = None   # target_pid自身の直近PTS(PCRよりずっと精密)
    pts_after: Optional[int] = None


def find_cc_gaps(path: str, packet_size: int, sync_offset: int, target_pid: int,
                  pcr_pid: Optional[int] = None) -> list[CCGap]:
    """target_pidの連続性カウンタ(continuity_counter)の欠番(ドロップ)を検出する。

    ISO/IEC 13818-1 2.4.3.3: ペイロードを持つパケットのcontinuity_counterは
    そのPIDごとに0-15で1ずつ増加する。増分が1(mod16)でない箇所はパケット
    ロスト(ドロップ)を示す。

    あわせて、直近のPCR(pcr_pid指定時)と、target_pid自身のPES PTS(映像/音声
    フレームごとに現れるため、PCRよりずっと細かい間隔で得られる)も記録する。
    """
    gaps: list[CCGap] = []
    last_cc: Optional[int] = None
    last_pos: Optional[int] = None
    last_pcr: Optional[int] = None
    cur_pcr: Optional[int] = None
    last_pts: Optional[int] = None
    cur_pts: Optional[int] = None

    for pos, pkt in _iter_packets(path, packet_size, sync_offset):
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pcr_pid is not None and pid == pcr_pid:
            pcr = read_pcr(pkt)
            if pcr is not None:
                cur_pcr = pcr
        if pid != target_pid:
            continue
        _pid, pusi, payload = _payload(pkt)
        if pusi and payload:
            pts = read_pts(payload)
            if pts is not None:
                cur_pts = pts
        afc = (pkt[3] >> 4) & 0x3
        if afc not in (1, 3):   # ペイロード無しパケットはCCが増加しないので対象外
            continue
        cc = pkt[3] & 0x0F
        if last_cc is not None:
            expected = (last_cc + 1) & 0x0F
            if cc != expected:
                gaps.append(CCGap(pid=target_pid, pos_before=last_pos, pos_after=pos,
                                   cc_before=last_cc, cc_after=cc,
                                   pcr_before=last_pcr, pcr_after=cur_pcr,
                                   pts_before=last_pts, pts_after=cur_pts))
        last_cc = cc
        last_pos = pos
        last_pcr = cur_pcr
        last_pts = cur_pts
    return gaps


@dataclass
class StreamScanResult:
    pcr_positions: list = field(default_factory=list)   # [(バイト位置, PCR値), ...] pcr_pidの全記録
    pts_positions: list = field(default_factory=list)   # [(バイト位置, PTS値), ...] target_pidの全記録
    gaps: list = field(default_factory=list)             # target_pidのCCGapの一覧


class ScanCancelled(Exception):
    """scan_streamがキャンセルされたことを示す例外。"""


def scan_stream(path: str, packet_size: int, sync_offset: int, target_pid: int,
                 pcr_pid: Optional[int] = None,
                 on_progress: Optional[Callable[[int, int], None]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None,
                 progress_every: int = 200_000) -> StreamScanResult:
    """find_pcr_positions・find_pts_positions・find_cc_gapsが個別にファイルを
    3回読み直していたのを、1回のスキャンにまとめた高速版。

    ロジック自体は元の3関数と同一(このファイルを1回読みながら、pcr_pidの
    PCR記録・target_pidのPTS記録・target_pidのCCドロップ検出を同時に行う
    だけ)なので、結果は個別に呼んだ場合と完全に一致する。

    on_progress(現在のバイト位置, ファイル全体のバイト数) を一定間隔で呼ぶ。
    should_cancel() がTrueを返すとScanCancelledを送出して中断する。
    """
    result = StreamScanResult()
    last_cc: Optional[int] = None
    last_pos: Optional[int] = None
    last_pcr: Optional[int] = None
    cur_pcr: Optional[int] = None
    last_pts: Optional[int] = None
    cur_pts: Optional[int] = None

    total_size = None
    if on_progress is not None:
        import os as _os
        try:
            total_size = _os.path.getsize(path)
        except OSError:
            total_size = None

    n = 0
    for pos, pkt in _iter_packets(path, packet_size, sync_offset):
        n += 1
        if n % progress_every == 0:
            if should_cancel is not None and should_cancel():
                raise ScanCancelled()
            if on_progress is not None and total_size:
                on_progress(pos, total_size)

        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pcr_pid is not None and pid == pcr_pid:
            pcr = read_pcr(pkt)
            if pcr is not None:
                cur_pcr = pcr
                result.pcr_positions.append((pos, pcr))
        if pid != target_pid:
            continue
        _pid, pusi, payload = _payload(pkt)
        if pusi and payload:
            pts = read_pts(payload)
            if pts is not None:
                cur_pts = pts
                result.pts_positions.append((pos, pts))
        afc = (pkt[3] >> 4) & 0x3
        if afc not in (1, 3):
            continue
        cc = pkt[3] & 0x0F
        if last_cc is not None:
            expected = (last_cc + 1) & 0x0F
            if cc != expected:
                result.gaps.append(CCGap(pid=target_pid, pos_before=last_pos, pos_after=pos,
                                          cc_before=last_cc, cc_after=cc,
                                          pcr_before=last_pcr, pcr_after=cur_pcr,
                                          pts_before=last_pts, pts_after=cur_pts))
        last_cc = cc
        last_pos = pos
        last_pcr = cur_pcr
        last_pts = cur_pts

    if on_progress is not None and total_size:
        on_progress(total_size, total_size)
    return result


def read_pts(payload: bytes) -> Optional[int]:
    """PESヘッダ(payload先頭が 00 00 01 のPESパケット)からPTSを取り出す。

    PES/PTSのビット配置は ISO/IEC 13818-1 2.4.3.7 の公開仕様。
    """
    if len(payload) < 14 or payload[0:3] != b"\x00\x00\x01":
        return None
    stream_id = payload[3]
    # プログラムストリームマップ等、PTSを持たない特殊stream_idは除外
    if stream_id in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8):
        return None
    if len(payload) < 9:
        return None
    pts_dts_flags = (payload[7] >> 6) & 0x3
    if pts_dts_flags == 0:
        return None
    if len(payload) < 14:
        return None
    b = payload[9:14]
    pts = ((b[0] & 0x0E) << 29) | (b[1] << 22) | ((b[2] & 0xFE) << 14) | (b[3] << 7) | (b[4] >> 1)
    return pts


def read_dts(payload: bytes) -> Optional[int]:
    """PESヘッダからDTS(Decoding Time Stamp)を取り出す。PTS_DTS_flags='11'
    (PTSとDTSの両方がある)場合のみ存在する。DTSは符号化(=ファイル中の並び)
    順で単調増加するタイムスタンプで、Bフレームの並び替えの影響を受けない
    PTS(表示順のタイムスタンプで、Bフレームがあると前後することがある)
    とは区別して使う必要がある。ビット配置は ISO/IEC 13818-1 2.4.3.7 の公開仕様。
    """
    if len(payload) < 19 or payload[0:3] != b"\x00\x00\x01":
        return None
    if len(payload) < 9:
        return None
    pts_dts_flags = (payload[7] >> 6) & 0x3
    if pts_dts_flags != 0x3:   # PTSとDTSの両方がある場合のみ
        return None
    b = payload[14:19]
    dts = ((b[0] & 0x0E) << 29) | (b[1] << 22) | ((b[2] & 0xFE) << 14) | (b[3] << 7) | (b[4] >> 1)
    return dts


def find_dts_positions(path: str, packet_size: int, sync_offset: int, target_pid: int,
                        max_packets: Optional[int] = None) -> list[tuple[int, int]]:
    """(ファイル先頭からのバイト位置, DTS値[90kHz])のリストを返す。
    DTSが無い(PTSのみの)PESは含まれない。"""
    out = []
    n = 0
    for pos, pkt in _iter_packets(path, packet_size, sync_offset):
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pid != target_pid:
            continue
        _pid, pusi, payload = _payload(pkt)
        if pusi and payload:
            dts = read_dts(payload)
            if dts is not None:
                out.append((pos, dts))
        n += 1
        if max_packets is not None and n >= max_packets:
            break
    return out


def find_pts_positions(path: str, packet_size: int, sync_offset: int, target_pid: int,
                        max_packets: Optional[int] = None) -> list[tuple[int, int]]:
    """(ファイル先頭からのバイト位置, PTS値[90kHz])のリストを返す。

    PTSはPES(video/audioの符号化アクセスユニット)ごと、つまりPCRより
    ずっと細かい間隔(映像ならフレーム単位)で現れるため、PCRより精密な
    ファイル間の対応付けに使える。
    """
    out = []
    n = 0
    for pos, pkt in _iter_packets(path, packet_size, sync_offset):
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pid != target_pid:
            continue
        _pid, pusi, payload = _payload(pkt)
        if pusi and payload:
            pts = read_pts(payload)
            if pts is not None:
                out.append((pos, pts))
        n += 1
        if max_packets is not None and n >= max_packets:
            break
    return out


def _collect_section(path: str, packet_size: int, sync_offset: int, target_pid: int,
                      max_packets: int = 200000, start_byte: int = 0) -> Optional[bytes]:
    buf = None
    n = 0
    for _pos, pkt in _iter_packets(path, packet_size, sync_offset, start_byte=start_byte):
        n += 1
        if n > max_packets:
            break
        pid, pusi, payload = _payload(pkt)
        if pid != target_pid or not payload:
            continue
        if pusi:
            ptr = payload[0]
            payload = payload[1 + ptr:]
            buf = bytearray(payload)
        else:
            if buf is None:
                continue
            buf.extend(payload)
        if buf is not None and len(buf) >= 3:
            sec_len = ((buf[1] & 0x0f) << 8) | buf[2]
            total_len = 3 + sec_len
            if len(buf) >= total_len:
                return bytes(buf[:total_len])
    return None


@dataclass
class StreamInfo:
    video_pid: Optional[int] = None
    pcr_pid: Optional[int] = None
    all_pids: tuple = ()   # PMTに列挙された全エレメンタリストリームのPID(種別問わず)
    pid_types: dict = field(default_factory=dict)   # {pid: stream_type}


def get_stream_info(path: str, packet_size: int = 188, sync_offset: int = 0,
                     start_byte: int = 0) -> StreamInfo:
    """PAT/PMTを解析し、映像PIDとPCR PID、全エレメンタリストリームPIDを取得する
    (公開仕様ベースの最小実装)。start_byteを指定すると、そこから前方探索する
    (D-VHS等でチャンネルが切り替わり、途中でPAT/PMTの構成が変わる場合に、
    ファイル各所をサンプリングして構成の違いを拾うために使う)。"""
    pat = _collect_section(path, packet_size, sync_offset, 0x0000, start_byte=start_byte)
    if not pat:
        return StreamInfo()
    sec_len = ((pat[1] & 0x0f) << 8) | pat[2]
    body = pat[8:3 + sec_len - 4]
    pmt_pid = None
    i = 0
    while i + 4 <= len(body):
        prog_num = (body[i] << 8) | body[i + 1]
        pid = ((body[i + 2] & 0x1f) << 8) | body[i + 3]
        if prog_num != 0:
            pmt_pid = pid
            break
        i += 4
    if pmt_pid is None:
        return StreamInfo()

    pmt = _collect_section(path, packet_size, sync_offset, pmt_pid, start_byte=start_byte)
    if not pmt:
        return StreamInfo()
    sec_len = ((pmt[1] & 0x0f) << 8) | pmt[2]
    pcr_pid = ((pmt[8] & 0x1f) << 8) | pmt[9]
    proginfo_len = ((pmt[10] & 0x0f) << 8) | pmt[11]
    i = 12 + proginfo_len
    end = 3 + sec_len - 4
    video_pid = None
    all_pids = []
    pid_types: dict = {}
    while i + 5 <= end:
        stream_type = pmt[i]
        epid = ((pmt[i + 1] & 0x1f) << 8) | pmt[i + 2]
        eslen = ((pmt[i + 3] & 0x0f) << 8) | pmt[i + 4]
        if stream_type in (0x01, 0x02) and video_pid is None:   # MPEG-1/2 Video
            video_pid = epid
        all_pids.append(epid)
        pid_types[epid] = stream_type
        i += 5 + eslen
    return StreamInfo(video_pid=video_pid, pcr_pid=pcr_pid, all_pids=tuple(all_pids), pid_types=pid_types)


def _pat_section_pmt_pids(sec: bytes) -> list:
    """PATのセクション本体から、全プログラムのPMT PID一覧を返す
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
            out.append(pid)
        i += 4
    return out


def _pmt_section_pids(sec: bytes):
    """PMTのセクション本体から (PCR PID, エレメンタリストリームPID一覧) を返す。"""
    if len(sec) < 12:
        return None, []
    sec_len = ((sec[1] & 0x0f) << 8) | sec[2]
    pcr_pid = ((sec[8] & 0x1f) << 8) | sec[9]
    proginfo_len = ((sec[10] & 0x0f) << 8) | sec[11]
    i = 12 + proginfo_len
    end = 3 + sec_len - 4
    out = []
    while i + 5 <= end and i + 5 <= len(sec):
        epid = ((sec[i + 1] & 0x1f) << 8) | sec[i + 2]
        eslen = ((sec[i + 3] & 0x0f) << 8) | sec[i + 4]
        out.append(epid)
        i += 5 + eslen
    return pcr_pid, out


def _feed_section_buf(buf: Optional[bytearray], pusi: bool, payload: bytes) -> Optional[bytearray]:
    """_collect_sectionと同じ組み立てロジックを、複数PIDぶん呼び出し側で使い回せる
    よう関数として切り出したもの。pusi時は新しいセクションとして組み直す。"""
    if pusi:
        ptr = payload[0]
        return bytearray(payload[1 + ptr:])
    if buf is None:
        return None
    buf.extend(payload)
    return buf


def scan_all_pmt_pids(path: str, packet_size: int, sync_offset: int,
                       on_progress: Optional[Callable[[int, int], None]] = None,
                       should_cancel: Optional[Callable[[], bool]] = None,
                       progress_every: int = 500_000) -> set:
    """ファイル全体を1回だけ走査し、出現した全てのPAT/PMT構成(チャンネル切替を
    含む)から、映像/音声/PCR等のエレメンタリストリームPIDの和集合を返す。

    D-VHS等のパーシャルTSではチャンネル切り替えによりPAT/PMTの構成(PID割当)が
    ファイル内で変化することがある。TsSplitterは映像/音声/PCR等の既知の種別以外
    のPID(データ放送のデータカルーセル等, stream_type 0x0D)をデフォルトでは
    保持しないため、事前にこの関数で全構成のPIDを洗い出し、-PIDオプションで
    明示的に保持させる必要がある。

    以前は等間隔サンプリング(既定11点)だった。数GBのファイル内でサンプル点の
    間隔(数百MB)より短い区間にしか存在しない番組があると、その番組のPMTを
    サンプリングで検出できず、実際にTsSplitterがそのPMTのPIDを保持せずに落として
    しまい、結果としてPAT はプログラムを指しているのにそのPMTのパケットが1つも
    無い(=対応するPIDの構成をプレーヤーが特定できない)ファイルが生成される
    不具合が実データで確認された(TVTestでは再生できないが、PAT/PMTを前提に
    しない一部のツールでは再生できてしまうため気づきにくい)。そのため全パケットを
    1回だけ走査してPAT/PMTを漏れなく追跡する方式に変更した。"""
    import os as _os
    size = _os.path.getsize(path)
    if size <= 0:
        return set()

    all_pids: set = set()
    known_pmt_pids: set = set()
    pat_buf: Optional[bytearray] = None
    pmt_bufs: dict = {}
    n = 0

    for pos, pkt in _iter_packets(path, packet_size, sync_offset):
        n += 1
        if n % progress_every == 0:
            if should_cancel is not None and should_cancel():
                raise ScanCancelled()
            if on_progress is not None:
                on_progress(pos, size)

        pid, pusi, payload = _payload(pkt)
        if not payload:
            continue

        if pid == 0x0000:
            pat_buf = _feed_section_buf(pat_buf, pusi, payload)
            if pat_buf is not None and len(pat_buf) >= 3:
                sec_len = ((pat_buf[1] & 0x0f) << 8) | pat_buf[2]
                total_len = 3 + sec_len
                if len(pat_buf) >= total_len:
                    for pmt_pid in _pat_section_pmt_pids(bytes(pat_buf[:total_len])):
                        known_pmt_pids.add(pmt_pid)
                    pat_buf = None
        elif pid in known_pmt_pids:
            buf = _feed_section_buf(pmt_bufs.get(pid), pusi, payload)
            if buf is not None and len(buf) >= 3:
                sec_len = ((buf[1] & 0x0f) << 8) | buf[2]
                total_len = 3 + sec_len
                if len(buf) >= total_len:
                    pcr_pid, epids = _pmt_section_pids(bytes(buf[:total_len]))
                    all_pids.update(epids)
                    if pcr_pid is not None:
                        all_pids.add(pcr_pid)
                    buf = None
            pmt_bufs[pid] = buf

    # 注意: 以前はPMT自体のPID(0x3f0等)も-PIDの保持対象に含めていたが、実機検証で
    # 「TsSplitterに主番組の実PIDと他番組のPMT自体のPIDを同時に-PIDで渡すと、
    # ファイル全体の分割が丸ごと失敗する(0ファイル出力)」という重大な副作用が
    # 確認されたため撤回した。PMT欠落によるプレーヤー再生不可の問題は、
    # pmt_repair.py(再生修復・整理タブ)で事後的にPMTを合成する方式で対応する。
    if on_progress is not None:
        on_progress(size, size)
    return all_pids


def find_next_keyframe(path: str, packet_size: int, sync_offset: int, video_pid: int,
                        start_byte: int, search_window: int = 16 * 1024 * 1024) -> Optional[int]:
    """start_byte以降でvideo_pidのキーフレーム境界が現れる最初のTSパケットの
    ファイル先頭からのバイト位置を返す。見つからなければNone。

    放送用MPEG-2映像ストリームは、ランダムアクセス(チャンネル切り替え等)の
    ために各GOP(Iフレーム)の直前に必ずシーケンスヘッダ(0x000001B3, ISO/IEC
    13818-2 6.2.2.1)を再送する。このシーケンスヘッダの開始位置で切り出せば、
    デコーダは映像サイズ等のパラメータを再取得でき、コマ落ちや"Invalid frame
    dimensions"のようなデコードエラー無く先頭から復号できる。

    (最初はピクチャ開始コード0x000001 0x00のIフレーム判定を試みたが、実データで
     検証したところ、その数百バイト前にあるシーケンスヘッダを含めずに切り出すと
     デコーダがフレームサイズを取得できずエラーになることを確認したため、
     シーケンスヘッダの位置そのものを境界として採用する。)
    """
    buf = bytearray()
    breakpoints: list[tuple[int, int]] = []   # (bufでのオフセット, そのTSパケットの先頭ファイル位置)
    for pos, pkt in _iter_packets(path, packet_size, sync_offset, start_byte=start_byte,
                                   max_bytes=search_window):
        pid, _pusi, payload = _payload(pkt)
        if pid != video_pid or not payload:
            continue
        breakpoints.append((len(buf), pos))
        buf.extend(payload)

    idx = buf.find(SEQUENCE_HEADER_CODE)
    if idx == -1:
        return None
    offsets = [bp[0] for bp in breakpoints]
    j = bisect.bisect_right(offsets, idx) - 1
    return breakpoints[j][1] if j >= 0 else start_byte
