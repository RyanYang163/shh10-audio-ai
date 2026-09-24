"""合成测试素材 —— 每个字节都可预期，所以断言可以做到「精确」。

为什么不用真实样本：本仓库不能把版权音频入库；而自造文件能让
「静音边界到底落在哪一秒」「往返转换后时间戳是否逐字符一致」这类
最容易出错、也最值得断言的地方做到**精确可验**，而不是「看起来差不多」。

覆盖面：

* :func:`build_wav` —— 标准库 ``wave`` 写出真正的 WAV（可指定段落、声道、位深）
* :func:`build_flac` —— 最小合法 FLAC（STREAMINFO + VORBIS_COMMENT）
* :func:`build_mp3` —— ID3v2.3 标签 + 带 Xing 头的 MPEG 帧
* :func:`build_ogg` —— 识别头页 + 带 granule 的尾页
* :func:`build_m4a` —— ISO-BMFF 的 ftyp + moov（mvhd/trak/mdia/mdhd/hdlr/stsd）+ mdat

字幕素材不用二进制，直接在用例里写字符串（更直观），只在这里给出常用的
一个 LRC 与一个 SRT。
"""

import io
import math
import struct
import wave

# ---------------------------------------------------------------- WAV


def _sample_value(kind, index, rate, freq, amplitude):
    if kind == "silence":
        return 0.0
    if kind == "tone":
        return math.sin(2.0 * math.pi * freq * index / float(rate)) * amplitude
    if kind == "dc":
        return amplitude
    raise ValueError("未知的段落类型：%s" % kind)


def pcm_frames(segments, rate=44100, channels=1, bit_depth=16,
               freq=440.0, amplitude=0.5, gains=None):
    """按段落生成交错排列的 PCM 帧（bytes）与总帧数。

    ``segments`` 形如 ``[("silence", 0.5), ("tone", 1.0), ("silence", 0.5)]``。
    ``gains`` 给每个声道一个增益（默认全 1.0），用来验证多声道下混。
    """
    if gains is None:
        gains = [1.0] * channels
    if len(gains) != channels:
        raise ValueError("gains 的个数必须等于声道数")

    peak = {8: 127, 16: 32767, 24: 8388607, 32: 2147483647}[bit_depth]
    out = bytearray()
    total = 0
    for kind, seconds in segments:
        count = int(round(seconds * rate))
        for index in range(count):
            value = _sample_value(kind, total + index, rate, freq, amplitude)
            for channel in range(channels):
                scaled = value * gains[channel]
                if abs(scaled) > 1.0:
                    scaled = math.copysign(1.0, scaled)
                quantised = int(round(scaled * peak))
                if bit_depth == 8:
                    out.append(quantised + 128)          # 8 位 WAV 是无符号
                elif bit_depth == 16:
                    out += struct.pack("<h", quantised)
                elif bit_depth == 24:
                    out += struct.pack("<i", quantised)[:3]
                else:
                    out += struct.pack("<i", quantised)
        total += count
    return bytes(out), total


def build_wav(segments, rate=44100, channels=1, bit_depth=16,
              freq=440.0, amplitude=0.5, gains=None):
    """用标准库 ``wave`` 写一个真正的 WAV，返回字节串。"""
    frames, total = pcm_frames(segments, rate, channels, bit_depth, freq, amplitude, gains)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(bit_depth // 8)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


def write_wav(path, segments, **kwargs):
    data = build_wav(segments, **kwargs)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


#: 验收用的标准样本：0.5 秒静音 + 1 秒正弦 + 0.5 秒静音
STANDARD_SEGMENTS = [("silence", 0.5), ("tone", 1.0), ("silence", 0.5)]


def build_silent_wav(seconds, rate=8000, channels=1, bit_depth=16):
    """一段**纯静音**的长 WAV，按字节批量写 —— 秒级生成几百秒的素材。

    为什么需要它：测试里要造「一眼看不出分析要多花时间」的长文件
    （比如验证取消任务），逐样本循环生成几百秒的音频太慢，
    而静音正好可以直接铺字节：16 位是 0x0000、8 位是 0x80（无符号中心点）。
    """
    frames = int(round(seconds * rate))
    sample_bytes = bit_depth // 8
    if bit_depth == 8:
        payload = bytes([128]) * (frames * channels)
    else:
        payload = bytes(frames * channels * sample_bytes)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_bytes)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return buffer.getvalue()


# ---------------------------------------------------------------- FLAC


def build_flac(sample_rate=44100, channels=2, bit_depth=16, total_samples=88200,
               tags=None, padding=64):
    """最小合法 FLAC：STREAMINFO(34 字节) + 可选的 VORBIS_COMMENT + 一段填充。

    ``padding`` 让文件有非零长度 —— FLAC 的比特率是「文件大小 / 时长」，
    没有填充时算出来的比特率是 0，测不出东西。
    """
    packed = ((sample_rate & 0xFFFFF) << 44) | (((channels - 1) & 0x07) << 41) \
        | (((bit_depth - 1) & 0x1F) << 36) | (total_samples & 0xFFFFFFFFF)
    streaminfo = (
        struct.pack(">HH", 4096, 4096)          # min / max block size
        + b"\x00\x00\x00" + b"\x00\x00\x00"     # min / max frame size（未知填 0）
        + packed.to_bytes(8, "big")
        + b"\x00" * 16                          # MD5（这里不用校验）
    )
    blocks = bytearray()
    blocks.append(0x00)                          # 非最后一块，类型 0 = STREAMINFO
    blocks += len(streaminfo).to_bytes(3, "big")
    blocks += streaminfo

    if tags:
        block = _vorbis_comment_block(tags)
        blocks.append(0x84)                      # 最后一块 + 类型 4 = VORBIS_COMMENT
        blocks += len(block).to_bytes(3, "big")
        blocks += block
    else:
        blocks[0] = 0x80

    return b"fLaC" + bytes(blocks) + b"\x00" * padding


def _vorbis_comment_block(tags):
    vendor = b"shh10-synth"
    out = bytearray(struct.pack("<I", len(vendor)) + vendor)
    items = ["%s=%s" % (key, value) for key, value in tags.items()]
    out += struct.pack("<I", len(items))
    for item in items:
        raw = item.encode("utf-8")
        out += struct.pack("<I", len(raw)) + raw
    return bytes(out)


# ---------------------------------------------------------------- MP3


def _id3v2_frame(frame_id, text, version=3):
    frame_id = frame_id.encode("ascii") if isinstance(frame_id, str) else frame_id
    raw = b"\x03" + text.encode("utf-8")          # 编码 3 = UTF-8
    if version >= 4:
        size = _synchsafe(len(raw))
    else:
        size = struct.pack(">I", len(raw))
    return frame_id + size + b"\x00\x00" + raw


def _synchsafe(value):
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F,
                  (value >> 7) & 0x7F, value & 0x7F])


def build_id3v2(frames, version=3):
    body = b"".join(_id3v2_frame(fid, text, version) for fid, text in frames.items())
    return b"ID3" + bytes([version, 0, 0]) + _synchsafe(len(body)) + body


def build_mp3(title="合成测试曲", artist="合成歌手", frames=100, rate_index=0,
              bitrate_index=9, channel_mode=0, version=3, padding=4096):
    """ID3v2.3 标签 + 一个带 Xing 头的 MPEG1 Layer III 帧 + 填充。

    ``frames`` 是 Xing 头里声明的总帧数 —— 解析器应当据此算出
    ``frames * 1152 / 44100`` 的精确时长（而不是按比特率估算）。
    """
    header = bytes([
        0xFF,
        0xFB,                                     # MPEG1 + Layer III + 无 CRC
        (bitrate_index << 4) | (rate_index << 2),
        (channel_mode << 6),
    ])
    xing = b"Xing" + struct.pack(">I", 0x0001) + struct.pack(">I", frames)
    # 立体声 / 联合立体声的 Xing 头在帧头后 36 字节处
    body = header + b"\x00" * (36 - 4) + xing
    tag = build_id3v2({"TIT2": title, "TPE1": artist}, version)
    return tag + body + b"\x00" * padding


# ---------------------------------------------------------------- OGG


def _ogg_page(payload, granule=0, header_type=0, serial=1, sequence=0):
    segments = []
    remaining = len(payload)
    while remaining >= 255:
        segments.append(255)
        remaining -= 255
    segments.append(remaining)
    table = bytes(segments)
    page = bytearray(b"OggS\x00")
    page.append(header_type)
    page += struct.pack("<q", granule)
    page += struct.pack("<I", serial)
    page += struct.pack("<I", sequence)
    page += b"\x00\x00\x00\x00"                   # CRC（解析时不校验）
    page.append(len(table))
    page += table
    page += payload
    return bytes(page)


def build_ogg(sample_rate=44100, channels=2, total_samples=88200, tags=None):
    """一个 Vorbis 识别头页 + 一个评论页 + 一个带 granule 的尾页。

    时长由**尾页的 granule** 决定，这正是真实 OGG 的做法 ——
    识别头里只有采样率与声道，没有时长。
    """
    ident = (b"\x01vorbis" + struct.pack("<I", 0) + bytes([channels])
             + struct.pack("<I", sample_rate)
             + struct.pack("<iii", 0, 128000, 0) + bytes([0xB8, 1]))
    comment = b"\x03vorbis" + _vorbis_comment_block(tags or {})
    parts = [_ogg_page(ident, granule=0, header_type=2, sequence=0)]
    if tags:
        parts.append(_ogg_page(comment, granule=0, sequence=1))
    # 尾页：granule 就是总样本数
    parts.append(_ogg_page(b"\x00" * 16, granule=total_samples, sequence=2))
    return b"".join(parts)


def build_opus(input_sample_rate=44100, channels=2, granule=96000):
    """Opus 的识别头：``OpusHead`` + 版本 + 声道 + pre_skip + **原始**采样率。

    注意 granule 一律按 48 kHz 计 —— 这正是要验证的点：
    解析器不能拿识别头里的 ``input_sample_rate``（这里是 44100）去算时长。
    """
    ident = (b"OpusHead" + bytes([1, channels]) + struct.pack("<H", 312)
             + struct.pack("<I", input_sample_rate) + struct.pack("<h", 0) + bytes([0]))
    return _ogg_page(ident, granule=granule, header_type=2)


# ---------------------------------------------------------------- ISO-BMFF


def _box(box_type, payload):
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _full_box(box_type, version, flags, payload):
    return _box(box_type, struct.pack(">B3s", version, flags) + payload)


def build_m4a(sample_rate=44100, channels=2, bit_depth=16, duration=2.0,
              timescale=1000, codec=b"mp4a", language="eng", payload=2048):
    """最小合法 M4A：ftyp + moov(mvhd + 一条 soun 轨) + mdat。

    ``mdat`` 按长度跳过就能拿到元数据，正是「读元数据不读全文件」的验证点。
    """
    lang_bits = 0
    for char in language[:3]:
        lang_bits = (lang_bits << 5) | ((ord(char) - 0x60) & 0x1F)
    frames = int(round(duration * timescale))

    mvhd = _full_box(b"mvhd", 0, b"\x00\x00\x00",
                     struct.pack(">II", 0, 0) + struct.pack(">II", timescale, frames)
                     + struct.pack(">I", 0x00010000) + struct.pack(">H", 0x0100)
                     + b"\x00" * 10 + b"\x00" * 36 + struct.pack(">I", 2))
    mdhd = _full_box(b"mdhd", 0, b"\x00\x00\x00",
                     struct.pack(">II", 0, 0) + struct.pack(">II", timescale, frames)
                     + struct.pack(">HH", lang_bits, 0))
    hdlr = _full_box(b"hdlr", 0, b"\x00\x00\x00",
                     b"\x00" * 4 + b"soun" + b"\x00" * 12 + b"SoundHandler\x00")
    entry = struct.pack(">I", 8 + 28) + codec + b"\x00" * 6 + struct.pack(">H", 1)
    entry += struct.pack(">HHIHHHHI", 0, 0, 0, channels, bit_depth, 0, 0,
                         sample_rate << 16)
    stsd = _full_box(b"stsd", 0, b"\x00\x00\x00", struct.pack(">I", 1) + entry)
    stbl = _box(b"stbl", stsd)
    dinf = _box(b"dinf", _box(b"dref", struct.pack(">II", 0, 0)))
    minf = _box(b"minf", _full_box(b"smhd", 0, b"\x00\x00\x00", struct.pack(">HH", 0, 0))
                + dinf + stbl)
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    tkhd = _full_box(b"tkhd", 0, b"\x00\x00\x07",
                     struct.pack(">II", 0, 0) + struct.pack(">I", 1) + b"\x00" * 4
                     + struct.pack(">I", frames) + b"\x00" * 8
                     + struct.pack(">hhhh", 0, 0, 0x0100, 0) + b"\x00" * 36
                     + struct.pack(">II", 0, 0))
    trak = _box(b"trak", tkhd + mdia)
    ftyp = _box(b"ftyp", b"M4A " + struct.pack(">I", 0x200) + b"M4A mp42isom")
    moov = _box(b"moov", mvhd + trak)
    mdat = _box(b"mdat", b"\x00" * payload)
    return ftyp + moov + mdat


# ---------------------------------------------------------------- 字幕素材

LRC_SAMPLE = """[ti:合成测试歌词]
[ar:合成歌手]
[00:00.00]第一行歌词
[00:12.34]第二行歌词
[00:20.50][00:40.25]重复的副歌
[01:02.99]最后一行
"""

SRT_SAMPLE = """1
00:00:00,000 --> 00:00:02,500
First subtitle line

2
00:00:02.500 --> 00:00:05,000
第二行字幕
带两行文本
"""

VTT_SAMPLE = """WEBVTT

NOTE 这是一条注释，解析时要跳过

1
00:00:00.000 --> 00:00:02.500 line:0 position:20%
First line

2
00:02.500 --> 00:05.000
Second line
"""

#: 「每 1 秒一行」的碎句歌词 —— 歌词模式应当把它们合并成整句
LRC_FRAGMENTED = """[00:00.00]今天
[00:01.00]天气
[00:02.00]很好
[00:03.00]我
[00:04.00]想出去走走
"""
