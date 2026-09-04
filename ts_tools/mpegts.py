"""MPEG-2 TSの標準仕様(公開情報)に基づく軽量パーサー。

PAT/PMTの解析と、映像PIDのGOP開始点(キーフレーム境界)検出を行う。
TMPGEnc等の商用ソフトの内部実装を参照・解析したものではなく、
MPEG-2 Systems(ISO/IEC 13818-1)の公開仕様にのみ基づく実装。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Optional

GOP_START_CODE = b"\x00\x00\x01\xb8"        # GOPヘッダ開始コード(MPEG-2, ISO/IEC 13818-2)
PICTURE_START_CODE = b"\x00\x00\x01\x00"    # ピクチャ開始コード(同上)
SEQUENCE_HEADER_CODE = b"\x00\x00\x01\xb3"  # シーケンスヘッダ開始コード(同上)


def _iter_packets(path: str, packet_size: int, sync_offset: int, start_byte: int = 0,
                   max_bytes: Optional[int] = None):
    start_byte = (start_byte // packet_size) * packet_size   # パケット境界に整列
    with open(path, "rb") as f:
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
    while i + 5 <= end:
        stream_type = pmt[i]
        epid = ((pmt[i + 1] & 0x1f) << 8) | pmt[i + 2]
        eslen = ((pmt[i + 3] & 0x0f) << 8) | pmt[i + 4]
        if stream_type in (0x01, 0x02) and video_pid is None:   # MPEG-1/2 Video
            video_pid = epid
        all_pids.append(epid)
        i += 5 + eslen
    return StreamInfo(video_pid=video_pid, pcr_pid=pcr_pid, all_pids=tuple(all_pids))


def scan_all_pmt_pids(path: str, packet_size: int, sync_offset: int,
                       sample_points: int = 11) -> set:
    """ファイル全体を等間隔にサンプリングし、各地点で見つかったPMTのエレメンタリ
    ストリームPIDの和集合を返す。

    D-VHS等のパーシャルTSではチャンネル切り替えによりPAT/PMTの構成(PID割当)が
    ファイル内で変化することがある。TsSplitterは映像/音声/PCR等の既知の種別以外
    のPID(データ放送のデータカルーセル等, stream_type 0x0D)をデフォルトでは
    保持しないため、事前にこの関数で全構成のPIDを洗い出し、-PIDオプションで
    明示的に保持させる必要がある。
    """
    import os as _os
    size = _os.path.getsize(path)
    if size <= 0:
        return set()
    all_pids: set = set()
    n = max(2, sample_points)
    for i in range(n):
        start_byte = int(size * i / n)
        info = get_stream_info(path, packet_size, sync_offset, start_byte=start_byte)
        all_pids.update(info.all_pids)
        if info.pcr_pid is not None:
            all_pids.add(info.pcr_pid)
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
