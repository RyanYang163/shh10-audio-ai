"""纯音频文件元数据读取（WAV / FLAC / MP3 / OGG / OPUS / M4A）。

**为什么自己写而不是调外部程序**：Deb 包内不得含预编译二进制（指引 16.4 一票否决），
生命周期脚本也不许联网装依赖。所以「不装任何东西也能读出时长、采样率、声道、
位深、比特率与标签」这件事必须由标准库**逐段解析文件头**完成。

解析的真的只是**文件头**：

* WAV   —— 遍历 RIFF 块，只读 ``fmt `` / ``LIST``，``data`` 只取长度不读内容
* FLAC  —— 遍历元数据块，只读 STREAMINFO(34 字节) 与 VORBIS_COMMENT
* MP3   —— 跳过 ID3v2 后在前 1 MB 内找第一个帧同步字，再读 Xing 头拿总帧数
* OGG   —— 读第一个页里的识别头 + 从文件**尾部**读最后一页的 granule
* M4A   —— 沿 ISO-BMFF 的 box 树走进 ``moov``，``mdat`` 按长度跳过（绝不读内容）

因此对几十 GB 的文件也是毫秒级返回。
"""

import os
import struct

#: 单次读取的上限，防止畸形文件头里的超大长度把内存吃光
MAX_TAG_BYTES = 1 << 20


class AudioMetaError(Exception):
    """音频元数据解析失败（文件损坏、结构异常等）。"""


class UnsupportedFormat(AudioMetaError):
    """文件类型不在读取范围内。"""


def _read_slice(fh, start, count):
    fh.seek(start)
    return fh.read(count)


# ---------------------------------------------------------------- WAV / RIFF


def read_wav(fh, size):
    """RIFF/WAVE：``fmt `` 给格式，``data`` 的长度给时长。

    支持的编码：PCM(1) 与 IEEE float(3)；WAVE_FORMAT_EXTENSIBLE(0xFFFE) 会把
    子格式 GUID 的前两字节当作真正的编码号。A-law / µ-law / ADPCM 等压缩编码
    这里只报告名字、不算时长（算出来也是错的），由调用方决定怎么提示用户。
    """
    header = _read_slice(fh, 0, 12)
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise AudioMetaError("不是合法的 WAV 文件（缺少 RIFF/WAVE 标志）")

    info = {
        "format": "WAV", "codec": "PCM", "codec_id": 1, "channels": None,
        "sample_rate": None, "bit_depth": None, "duration": 0.0, "bitrate": 0,
        "tags": {}, "lossless": True,
    }
    data_size = None
    data_offset = None
    pos = 12
    while pos + 8 <= size:
        chunk_header = _read_slice(fh, pos, 8)
        if len(chunk_header) < 8:
            break
        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack("<I", chunk_header[4:8])[0]
        body_start = pos + 8

        if chunk_id == b"fmt ":
            body = _read_slice(fh, body_start, min(max(chunk_size, 16), 64))
            if len(body) >= 16:
                fmt_code, channels, rate, byte_rate, _align, bits = struct.unpack(
                    "<HHIIHH", body[:16])
                if fmt_code == 0xFFFE and len(body) >= 40:
                    # WAVE_FORMAT_EXTENSIBLE：真正编码在 SubFormat GUID 的前 2 字节
                    fmt_code = struct.unpack("<H", body[24:26])[0]
                info["codec_id"] = fmt_code
                info["channels"] = channels
                info["sample_rate"] = rate
                info["bit_depth"] = bits
                info["bitrate"] = byte_rate * 8
                info["codec"] = {
                    1: "PCM", 3: "IEEE Float", 6: "A-law", 7: "µ-law",
                    0x55: "MP3", 0x2000: "AC-3",
                }.get(fmt_code, "格式 0x%X" % fmt_code)
                info["lossless"] = fmt_code in (1, 3)
        elif chunk_id == b"data":
            data_size = chunk_size
            data_offset = body_start
            if chunk_size == 0 or chunk_size == 0xFFFFFFFF:
                # 流式写入的 WAV 会把 data 长度写成 0 或 0xFFFFFFFF —— 用文件长度兜底
                data_size = max(0, size - body_start)
        elif chunk_id == b"LIST":
            body = _read_slice(fh, body_start, min(chunk_size, MAX_TAG_BYTES))
            info["tags"].update(_parse_riff_info(body))

        pos = body_start + chunk_size + (chunk_size % 2)

    if data_offset is None:
        raise AudioMetaError("WAV 文件里没有 data 块")
    info["data_offset"] = data_offset
    info["data_size"] = int(data_size or 0)

    if data_size and info["sample_rate"] and info["bit_depth"] and info["channels"]:
        bytes_per_second = (info["sample_rate"] * info["channels"] * info["bit_depth"]) / 8
        if bytes_per_second > 0 and info["lossless"]:
            info["duration"] = data_size / bytes_per_second
        info["total_samples"] = int(
            data_size // max(1, info["channels"] * (info["bit_depth"] // 8))
        )
    return info


def _parse_riff_info(body):
    """解析 ``LIST/INFO`` 里的 ``INAM`` / ``IART`` 之类的标签。"""
    tags = {}
    mapping = {b"INAM": "title", b"IART": "artist", b"IPRD": "album",
               b"ICMT": "comment", b"ICRD": "date", b"IGNR": "genre",
               b"IPRT": "track", b"ISFT": "encoder", b"ICOP": "copyright"}
    pos = 4 if body[:4] == b"INFO" else 0
    while pos + 8 <= len(body):
        tag_id = body[pos:pos + 4]
        tag_size = struct.unpack("<I", body[pos + 4:pos + 8])[0]
        if tag_size > len(body) - pos - 8:
            break
        raw = body[pos + 8:pos + 8 + tag_size].rstrip(b"\x00")
        name = mapping.get(tag_id)
        if name and raw:
            tags[name] = raw.decode("utf-8", "replace")
        pos += 8 + tag_size + (tag_size % 2)
    return tags


# ---------------------------------------------------------------- FLAC


def read_flac(fh, size):
    """FLAC：STREAMINFO 块里直接带采样率、声道、位深与总样本数，无需扫描全文件。"""
    if _read_slice(fh, 0, 4) != b"fLaC":
        raise AudioMetaError("不是合法的 FLAC 文件（缺少 fLaC 标志）")
    info = {
        "format": "FLAC", "codec": "FLAC", "channels": None, "sample_rate": None,
        "bit_depth": None, "duration": 0.0, "bitrate": 0, "tags": {}, "lossless": True,
    }
    pos = 4
    while pos + 4 <= size:
        block_header = _read_slice(fh, pos, 4)
        if len(block_header) < 4:
            break
        is_last = bool(block_header[0] & 0x80)
        block_type = block_header[0] & 0x7F
        block_size = int.from_bytes(block_header[1:4], "big")

        if block_type == 0 and block_size >= 34:
            body = _read_slice(fh, pos + 4, 34)
            packed = int.from_bytes(body[10:18], "big")
            sample_rate = (packed >> 44) & 0xFFFFF
            channels = ((packed >> 41) & 0x07) + 1
            bits = ((packed >> 36) & 0x1F) + 1
            total_samples = packed & 0xFFFFFFFFF
            info["sample_rate"] = sample_rate
            info["channels"] = channels
            info["bit_depth"] = bits
            info["total_samples"] = total_samples
            if sample_rate:
                info["duration"] = total_samples / sample_rate
                if info["duration"]:
                    info["bitrate"] = int(size * 8 / info["duration"])
        elif block_type == 4:
            body = _read_slice(fh, pos + 4, min(block_size, MAX_TAG_BYTES))
            info["tags"].update(_parse_vorbis_comment(body))

        pos += 4 + block_size
        if is_last:
            break
    return info


def _parse_vorbis_comment(body):
    """Vorbis comment 块：小端长度前缀 + ``KEY=value`` 文本。"""
    tags = {}
    if len(body) < 8:
        return tags
    try:
        vendor_len = struct.unpack("<I", body[:4])[0]
        cursor = 4 + vendor_len
        if cursor + 4 > len(body):
            return tags
        count = struct.unpack("<I", body[cursor:cursor + 4])[0]
        cursor += 4
        for _ in range(min(count, 256)):
            if cursor + 4 > len(body):
                break
            length = struct.unpack("<I", body[cursor:cursor + 4])[0]
            cursor += 4
            if length > len(body) - cursor:
                break
            entry = body[cursor:cursor + length].decode("utf-8", "replace")
            cursor += length
            if "=" in entry:
                key, _, value = entry.partition("=")
                tags[key.strip().lower()] = value.strip()
    except (struct.error, IndexError, ValueError):
        pass
    return tags


# ---------------------------------------------------------------- MP3


_MPEG_BITRATES = {
    # (版本组, 层) -> 15 个码率索引对应的 kbps
    ("1", 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    ("1", 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    ("1", 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    ("2", 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    ("2", 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    ("2", 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLE_RATES = {"1": [44100, 48000, 32000],
                 "2": [22050, 24000, 16000],
                 "25": [11025, 12000, 8000]}

#: ID3v2 文本帧 → 我们统一的标签名（只认这几种，够用且不会出错）
_ID3_TEXT_FRAMES = {
    b"TIT2": "title", b"TPE1": "artist", b"TALB": "album", b"TCON": "genre",
    b"TRCK": "track", b"TDRC": "date", b"TYER": "date", b"TSSE": "encoder",
    b"TCOM": "composer", b"TPE2": "album_artist", b"TCOP": "copyright",
    b"COMM": "comment",
}


def read_mp3(fh, size):
    """MP3：跳过 ID3v2 标签后定位第一个 MPEG 帧头，有 Xing/Info 头时用总帧数算精确时长。

    没有 Xing 头的老文件只能按「平均码率」估算时长 —— 这在 VBR 文件上会有偏差，
    因此响应里用 ``duration_estimated`` 明确标出来，不假装它是精确值。
    """
    info = {
        "format": "MP3", "codec": "MPEG Layer III", "channels": None,
        "sample_rate": None, "bit_depth": None, "duration": 0.0, "bitrate": 0,
        "tags": {}, "lossless": False, "duration_estimated": True,
    }
    start = 0
    id3 = _read_slice(fh, 0, 10)
    if id3[:3] == b"ID3" and len(id3) >= 10:
        tag_size = ((id3[6] & 0x7F) << 21 | (id3[7] & 0x7F) << 14
                    | (id3[8] & 0x7F) << 7 | (id3[9] & 0x7F))
        flags = id3[5]
        info["tags"]["id3_version"] = "2.%d" % id3[3]
        info["id3_size"] = tag_size
        # 标签体只读一次，用于取标题/艺术家；越界或畸形一律放弃（不影响时长解析）。
        # 注意读的是「含 10 字节头」的整段 —— 帧偏移是相对头之后的，_parse_id3v2_tags
        # 从 pos=10 开始遍历，第一版只读了帧部分导致永远解不出标签。
        if not (flags & 0x80):  # 0x80 = unsynchronisation，做了就不能按原偏移解帧
            body = _read_slice(fh, 0, min(10 + tag_size, MAX_TAG_BYTES))
            info["tags"].update(_parse_id3v2_tags(body, id3[3], flags))
        start = 10 + tag_size

    frame = _find_mpeg_frame(fh, start, min(size, start + (1 << 20)))
    if frame is None:
        raise AudioMetaError("没有找到 MPEG 帧头 —— 文件可能损坏，或者根本不是 MP3")
    offset, header = frame
    version_bits = (header[1] >> 3) & 0x03
    layer_bits = (header[1] >> 1) & 0x03
    bitrate_index = (header[2] >> 4) & 0x0F
    rate_index = (header[2] >> 2) & 0x03
    channel_mode = (header[3] >> 6) & 0x03

    version = {0: "25", 1: None, 2: "2", 3: "1"}.get(version_bits)
    layer = {1: 3, 2: 2, 3: 1}.get(layer_bits)
    if version is None or layer is None:
        raise AudioMetaError("MPEG 帧头非法（保留位被置位）")
    info["codec"] = "MPEG Layer %s" % {1: "I", 2: "II", 3: "III"}[layer]
    info["channels"] = 1 if channel_mode == 3 else 2
    rates = _SAMPLE_RATES[version]
    info["sample_rate"] = rates[rate_index] if rate_index < len(rates) else None
    table = _MPEG_BITRATES.get((version if version != "25" else "2", layer), [])
    bitrate = table[bitrate_index] if bitrate_index < len(table) else 0
    info["bitrate"] = bitrate * 1000

    frame_length = _mpeg_frame_length(header, version, layer, bitrate)
    body = _read_slice(fh, offset, max(frame_length, 64))
    frames = _xing_frame_count(body)
    if frames and info["sample_rate"]:
        samples_per_frame = _samples_per_mpeg_frame(version, layer)
        info["duration"] = frames * samples_per_frame / info["sample_rate"]
        info["duration_estimated"] = False
        if info["duration"]:
            info["bitrate"] = int((size - start) * 8 / info["duration"])
    elif info["bitrate"]:
        info["duration"] = (size - start) * 8 / info["bitrate"]
    info["total_samples"] = int((info["duration"] or 0) * (info["sample_rate"] or 0))
    return info


def _samples_per_mpeg_frame(version, layer):
    if layer == 1:
        return 384
    if layer == 2 or version == "1":
        return 1152
    return 576


def _parse_id3v2_tags(body, version_minor, flags):
    """解析 ID3v2.3/2.4 的文本帧（只读标签，不参与时长计算）。

    刻意做得极保守：任何越界、未知帧一律跳过，绝不因为标签畸形而抛异常 ——
    「读不出标题」是可接受的，「因为标题读不出而整个文件探不出时长」不是。
    """
    tags = {}
    if len(body) < 10:
        return tags
    extended = bool(flags & 0x40)  # 扩展头
    pos = 10
    if extended and pos + 4 <= len(body):
        ext_size = int.from_bytes(body[pos:pos + 4], "big")
        pos += 4 + max(0, ext_size)
    limit = min(len(body), MAX_TAG_BYTES)
    while pos + 10 <= limit:
        frame_id = body[pos:pos + 4]
        if not frame_id.strip(b"\x00"):
            break
        frame_size = int.from_bytes(body[pos + 4:pos + 8], "big")
        if version_minor >= 4:
            frame_size = _syncsafe(frame_size)
        if frame_size <= 0 or pos + 10 + frame_size > limit:
            break
        payload = body[pos + 10:pos + 10 + frame_size]
        pos += 10 + frame_size
        name = _ID3_TEXT_FRAMES.get(frame_id)
        if not name or len(payload) < 2:
            continue
        if frame_id == b"COMM":
            payload = _strip_id3_language(payload)
        text = _decode_id3_text(payload)
        if text:
            tags[name] = text
    return tags


def _strip_id3_language(payload):
    """COMM 帧在文本前有 3 字节语言码 + 一段以 0x00 结尾的短描述。"""
    if len(payload) < 5:
        return payload
    encoding = payload[0]
    rest = payload[4:]
    terminator = b"\x00\x00" if encoding in (1, 2) else b"\x00"
    index = rest.find(terminator)
    if index >= 0:
        return payload[:1] + rest[index + len(terminator):]
    return payload


def _syncsafe(value):
    """ID3v2.4 的帧长度是 synchsafe（每字节只用低 7 位）。"""
    return (((value >> 24) & 0x7F) << 21 | ((value >> 16) & 0x7F) << 14
            | ((value >> 8) & 0x7F) << 7 | (value & 0x7F))


def _decode_id3_text(payload):
    """第一字节是编码：0=latin-1, 1=utf-16(BOM), 2=utf-16be, 3=utf-8。"""
    encoding = payload[0]
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(encoding, "latin-1")
    return payload[1:].decode(codec, "replace").replace("\x00", " ").strip()


def _find_mpeg_frame(fh, start, limit):
    """在 ``[start, limit)`` 里找同步字 0xFFE，并校验头部字段合法。

    只扫前 1 MB：正常文件第一个帧头就在 ID3v2 之后，扫太远没有意义。
    """
    fh.seek(start)
    window = fh.read(max(0, limit - start))
    for index in range(max(0, len(window) - 4)):
        if window[index] == 0xFF and (window[index + 1] & 0xE0) == 0xE0:
            header = window[index:index + 4]
            layer_bits = (header[1] >> 1) & 0x03
            bitrate_index = (header[2] >> 4) & 0x0F
            rate_index = (header[2] >> 2) & 0x03
            if layer_bits in (1, 2, 3) and bitrate_index not in (0, 15) and rate_index != 3:
                return start + index, header
    return None


def _mpeg_frame_length(header, version, layer, bitrate_kbps):
    rate_index = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    rates = _SAMPLE_RATES.get(version, [44100, 48000, 32000])
    rate = rates[rate_index] if rate_index < len(rates) else 44100
    if not bitrate_kbps or not rate:
        return 0
    if layer == 1:
        return int(((12 * bitrate_kbps * 1000 / rate) + padding) * 4)
    samples = _samples_per_mpeg_frame(version, layer)
    return int(samples / 8 * bitrate_kbps * 1000 / rate) + padding


def _xing_frame_count(body):
    """Xing / Info 头里的总帧数字段（存在时才返回）。"""
    for marker in (b"Xing", b"Info"):
        index = body.find(marker)
        if index < 0 or index + 8 > len(body):
            continue
        flags = struct.unpack(">I", body[index + 4:index + 8])[0]
        if not (flags & 0x0001):
            return None
        if index + 12 > len(body):
            return None
        return struct.unpack(">I", body[index + 8:index + 12])[0]
    return None


# ---------------------------------------------------------------- OGG


def read_ogg(fh, size):
    """OGG：从第一个页里的识别头读采样率/声道；时长用**最后一页**的 granule 求。

    Opus 的 granule 一律按 48 kHz 计（规范如此），识别头里的 input rate 只是
    「原始采样率」的提示，不能拿它算时长。
    """
    if _read_slice(fh, 0, 4) != b"OggS":
        raise AudioMetaError("不是合法的 OGG 文件（缺少 OggS 标志）")
    info = {
        "format": "OGG", "codec": "OGG", "channels": None, "sample_rate": None,
        "bit_depth": None, "duration": 0.0, "bitrate": 0, "tags": {}, "lossless": False,
    }

    head = _read_slice(fh, 0, 65536)
    vorbis = head.find(b"\x01vorbis")
    opus = head.find(b"OpusHead")
    if vorbis >= 0 and vorbis + 16 <= len(head):
        info["codec"] = "Vorbis"
        info["channels"] = head[vorbis + 11]
        info["sample_rate"] = struct.unpack("<I", head[vorbis + 12:vorbis + 16])[0]
        # Vorbis comment 在第二个页里。注意 packet 的布局是
        # ``\x03vorbis`` + **Vorbis comment 块本体**（厂商串长度开头），
        # 所以在 +7 处直接交给通用解析器，而不是先自己读一个长度（第一版读错了偏移）。
        comment = head.find(b"\x03vorbis")
        if comment >= 0:
            info["tags"].update(_parse_vorbis_comment(head[comment + 7:]))
    elif opus >= 0 and opus + 19 <= len(head):
        info["codec"] = "Opus"
        info["channels"] = head[opus + 9]
        info["sample_rate"] = 48000
        info["input_sample_rate"] = struct.unpack("<I", head[opus + 12:opus + 16])[0]
        info["bit_depth"] = 16
        comment = head.find(b"OpusTags")
        if comment >= 0:
            info["tags"].update(_parse_vorbis_comment(head[comment + 8:]))
    elif head.find(b"\x7fFLAC") >= 0:
        info["codec"] = "FLAC (in OGG)"
        info["lossless"] = True
    else:
        info["codec"] = "未知 OGG 编码"

    if info["sample_rate"] is None:
        info["sample_rate"] = 48000

    granule = _last_granule(fh, size)
    if granule:
        info["duration"] = granule / info["sample_rate"]
        info["total_samples"] = granule
        if info["duration"]:
            info["bitrate"] = int(size * 8 / info["duration"])
    return info


def _last_granule(fh, size):
    """从文件尾部往前找最后一个 ``OggS`` 页，取它的 granule position。"""
    window = min(size, 65536)
    if window <= 0:
        return 0
    tail = _read_slice(fh, size - window, window)
    index = tail.rfind(b"OggS")
    if index < 0 or index + 14 > len(tail):
        return 0
    return struct.unpack("<q", tail[index + 6:index + 14])[0]


# ---------------------------------------------------------------- ISO-BMFF（M4A / MP4 / MOV）


def _iter_boxes(fh, start, end):
    """遍历 ``[start, end)`` 里的 box，产出 ``(type, header_start, body_start, body_end)``。

    ``mdat`` 这类大 box **只按长度跳过，绝不读内容** —— 这是「读元数据不读全文件」的关键。
    """
    pos = start
    while pos + 8 <= end:
        header = _read_slice(fh, pos, 16)
        if len(header) < 8:
            return
        box_size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8]
        header_size = 8
        if box_size == 1:
            if len(header) < 16:
                return
            box_size = struct.unpack(">Q", header[8:16])[0]
            header_size = 16
        elif box_size == 0:
            box_size = end - pos
        if box_size < header_size or pos + box_size > end:
            return
        yield box_type, pos, pos + header_size, pos + box_size
        pos += box_size


def _find_box(fh, start, end, wanted):
    for box_type, _hs, body_start, body_end in _iter_boxes(fh, start, end):
        if box_type == wanted:
            return body_start, body_end
    return None


def read_m4a(fh, size):
    """M4A / MP4 / MOV：走进 ``moov``，取 mvhd + 每条 trak 的 mdhd/stsd。

    ``ftyp`` 只用来判断容器家族；真正的信息在 ``moov`` 里，而 ``moov`` 可能在
    文件尾部（未 faststart 的 MP4 就是这样），所以按 box 树找而不是靠固定偏移。
    """
    info = {
        "format": "M4A", "codec": None, "channels": None, "sample_rate": None,
        "bit_depth": None, "duration": 0.0, "bitrate": 0, "tags": {}, "lossless": False,
        "brand": None, "audio_tracks": [],
    }
    head = _read_slice(fh, 0, 12)
    if len(head) < 8 or head[4:8] != b"ftyp":
        raise AudioMetaError("不是合法的 MP4/M4A 文件（缺少 ftyp box）")
    info["brand"] = head[8:12].decode("latin-1", "replace").strip()

    moov = _find_box(fh, 0, size, b"moov")
    if moov is None:
        raise AudioMetaError("MP4/M4A 里没有找到 moov box（文件可能不完整）")
    moov_start, moov_end = moov

    movie_duration = 0.0
    mvhd = _find_box(fh, moov_start, moov_end, b"mvhd")
    if mvhd:
        timescale, duration, _language = _read_mdhd_like(fh, mvhd[0], mvhd[1])
        if timescale:
            movie_duration = duration / timescale
    info["duration"] = movie_duration

    for box_type, _hs, trak_start, trak_end in _iter_boxes(fh, moov_start, moov_end):
        if box_type != b"trak":
            continue
        track = _parse_trak(fh, trak_start, trak_end)
        if track:
            info["audio_tracks"].append(track)

    audio = [t for t in info["audio_tracks"] if t.get("kind") == "audio"]
    chosen = audio[0] if audio else (info["audio_tracks"][0] if info["audio_tracks"] else None)
    if chosen:
        info["codec"] = chosen.get("codec")
        info["channels"] = chosen.get("channels")
        info["sample_rate"] = chosen.get("sample_rate")
        info["bit_depth"] = chosen.get("bit_depth")
        if chosen.get("duration"):
            info["duration"] = chosen["duration"]
    if info["duration"]:
        info["bitrate"] = int(size * 8 / info["duration"])
    info["track_count"] = len(info["audio_tracks"])
    return info


def _read_mdhd_like(fh, body_start, body_end):
    """读 mvhd / mdhd 的 ``(timescale, duration, language)``，兼容 version 0 与 1。

    两者布局一致：version/flags(4) + 创建/修改时间 + timescale + duration + 语言/音量。
    区别只在 version 1 的时间字段是 64 位。语言码在**最后**：3 个字母各占 5 bit，各 +0x60。
    """
    version = _read_slice(fh, body_start, 1)
    if not version:
        return 0, 0, "und"
    if version[0] == 1:
        raw = _read_slice(fh, body_start + 4 + 16, 14)
        if len(raw) < 14:
            return 0, 0, "und"
        timescale, duration = struct.unpack(">IQ", raw[:12])
        packed = struct.unpack(">H", raw[12:14])[0]
    else:
        raw = _read_slice(fh, body_start + 4 + 8, 10)
        if len(raw) < 10:
            return 0, 0, "und"
        timescale, duration = struct.unpack(">II", raw[:8])
        packed = struct.unpack(">H", raw[8:10])[0]
    if duration in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        duration = 0
    language = "und"
    if packed:
        letters = "".join(chr(((packed >> shift) & 0x1F) + 0x60) for shift in (10, 5, 0))
        if all("a" <= ch <= "z" for ch in letters):
            language = letters
    return timescale, duration, language


def _parse_trak(fh, start, end):
    """一条轨道：mdhd 给时长/语言，hdlr 给类型，stsd 给编码与声道。"""
    track = {"kind": "other", "codec": None, "channels": None, "sample_rate": None,
             "bit_depth": None, "duration": 0.0, "language": "und"}
    mdia = _find_box(fh, start, end, b"mdia")
    if mdia is None:
        return track
    mdia_start, mdia_end = mdia

    mdhd = _find_box(fh, mdia_start, mdia_end, b"mdhd")
    if mdhd:
        timescale, duration, language = _read_mdhd_like(fh, mdhd[0], mdhd[1])
        if timescale:
            track["duration"] = duration / timescale
        track["language"] = language

    hdlr = _find_box(fh, mdia_start, mdia_end, b"hdlr")
    if hdlr:
        handler = _read_slice(fh, hdlr[0] + 8, 4)
        if handler == b"soun":
            track["kind"] = "audio"
        elif handler == b"vide":
            track["kind"] = "video"

    minf = _find_box(fh, mdia_start, mdia_end, b"minf")
    if minf:
        stbl = _find_box(fh, minf[0], minf[1], b"stbl")
        if stbl:
            stsd = _find_box(fh, stbl[0], stbl[1], b"stsd")
            if stsd:
                track.update(_parse_stsd(fh, stsd[0], stsd[1]))
    return track


def _parse_stsd(fh, start, end):
    """stsd：样本描述表。音频条目里有声道数、位深与采样率（16.16 定点）。

    条目内的偏移（相对条目起点）：size(4) fourcc(4) reserved(6) data_ref_index(2)
    → version(2) revision(2) vendor(4) **channelcount(2) samplesize(2)**
    compression_id(2) packet_size(2) **samplerate(4, 16.16)**。
    条目起点在 ``stsd`` 体里是 +8（4 字节 version/flags + 4 字节条目数），
    于是三者落在 +32 / +34 / **+40**（第一版把采样率读成了 +36，
    读到的是 compression_id/packet_size，测试里直接表现为 sample_rate 为空）。
    """
    result = {"codec": None, "channels": None, "sample_rate": None, "bit_depth": None}
    raw = _read_slice(fh, start, min(end - start, 4096))
    if len(raw) < 16:
        return result
    count = struct.unpack(">I", raw[4:8])[0]
    if count < 1:
        return result
    fourcc = raw[12:16]
    result["codec"] = fourcc.decode("latin-1", "replace").strip() or None
    if len(raw) >= 44:
        channels, sample_size = struct.unpack(">HH", raw[32:36])
        rate = struct.unpack(">I", raw[40:44])[0] >> 16
        result["channels"] = channels or None
        result["bit_depth"] = sample_size or None
        result["sample_rate"] = rate or None
    return result


# ---------------------------------------------------------------- 统一入口


READERS = {
    ".wav": read_wav,
    ".wave": read_wav,
    ".flac": read_flac,
    ".mp3": read_mp3,
    ".ogg": read_ogg,
    ".oga": read_ogg,
    ".opus": read_ogg,
    ".m4a": read_m4a,
    ".mp4": read_m4a,
    ".mov": read_m4a,
    ".m4b": read_m4a,
}

#: 能读元数据的扩展名
AUDIO_EXTS = set(READERS)
#: 能解码成 PCM 做信号分析的扩展名（只有 WAV —— 无损且无压缩，标准库能解）
DECODABLE_EXTS = {".wav", ".wave"}
#: 字幕 / 文本类扩展名（由 subtitles.py 处理）
SUBTITLE_EXTS = {".lrc", ".srt", ".vtt", ".txt"}


def read_metadata(path):
    """读取一个音频文件的元数据；不支持的格式抛 :class:`UnsupportedFormat`。"""
    ext = os.path.splitext(path)[1].lower()
    reader = READERS.get(ext)
    if reader is None:
        raise UnsupportedFormat(
            "不支持的文件类型：%s（可读 WAV / FLAC / MP3 / OGG / OPUS / M4A）"
            % (ext or "无扩展名")
        )
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise AudioMetaError("无法读取文件：%s" % exc)
    with open(path, "rb") as fh:
        info = reader(fh, size)
    info["path"] = path
    info["size"] = size
    info["ext"] = ext
    info["duration"] = round(float(info.get("duration") or 0), 3)
    info.setdefault("lossless", False)
    info.setdefault("duration_estimated", False)
    return info
