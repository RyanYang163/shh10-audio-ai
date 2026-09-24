"""WAV → PCM 解码 + 信号分析（**本应用的真本事**）。

只做一件事但做扎实：把 WAV 解成 PCM 样本，算出

* **RMS 能量**（逐帧，dBFS）
* **过零率**（逐帧，0~1）
* **静音 / 有声段落**（连续低于阈值的帧组成一段，可配阈值与最短时长）
* **波形峰值**（按时间分桶的 min/max，供前端画波形）

------------------------------------------------------------------ 三条设计约束

**一、只用标准库。** 包内不得含预编译二进制，也不许在安装时联网装依赖，
所以不能借 numpy / scipy / librosa / ffmpeg。全部用 ``array`` + ``struct`` 手写。

**二、流式，不整文件进内存。** 按固定大小的块（默认 1 MB）读、解、累计，
一小时的 WAV 也只会占用常数级内存。每个块之间会调一次 ``checkpoint()``，
让任务队列能取消/暂停。

**三、逐帧而不是逐块算指标。** 帧长默认 20 ms（音频分析的常规取值），
静音段的边界因此**必然量化到帧**——这是本模块最需要说清楚的精度限制，
它决定了测试里的容忍度，见 ``analyze()`` 的 docstring。

------------------------------------------------------------------ 为什么手写内层循环

内层的「求和平方 + 过零判断」是每个样本都要过的路径，也是唯一的性能热点。
试过用 ``sum(map(operator.mul, seg, seg))`` 这类 C 级组合把求和平方换成 C 循环，
收益约 1.5~2 倍，但代价是跨切片统计过零次数时要额外维护「上一片最后一个样本的符号」，
容易出错。当前实现选择**可读且正确**，实测吞吐 5.9~6.6 M 样本/秒（见 ``analyze()``）。
真要提速，热点只在下面那一个 for 循环里，替换它即可，接口不用动。
"""

import array
import math
import os
import sys

from . import audiometa

#: 每次读多少字节（1 MB）。太大：单块解码后的样本数组占内存；太小：系统调用多。
CHUNK_BYTES = 1 << 20

#: RMS 为 0（数字静音）时的 dB 下限，避免 -inf 进 JSON
SILENCE_DB_FLOOR = -200.0

#: 整数位深 → (array 类型码, 满量程)
_INT_TYPES = {
    8: ("B", 128.0),
    16: ("h", 32768.0),
    32: ("i", 2147483648.0),
}
_FLOAT_TYPES = {32: ("f", 1.0), 64: ("d", 1.0)}


class PcmError(Exception):
    """解码或分析失败（格式不支持、文件损坏等）。"""


# ---------------------------------------------------------------- 解码


def read_wav_info(path):
    """读 WAV 头（走 audiometa，保持两条路径对同一个头只有一份实现）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in audiometa.DECODABLE_EXTS:
        raise PcmError(
            "波形分析目前只支持 WAV（当前是 %s）。FLAC / MP3 / OGG / M4A 都是压缩格式，"
            "标准库解不了 —— 请先用音频工具转成 PCM WAV 再分析；"
            "元数据读取与字幕转换不受影响。" % (ext or "无扩展名"))
    try:
        info = audiometa.read_metadata(path)
    except audiometa.UnsupportedFormat:
        raise PcmError("波形分析目前只支持 WAV（无损、标准库可直接解码）")
    except audiometa.AudioMetaError as exc:
        raise PcmError(str(exc))
    if info.get("codec_id") not in (1, 3):
        raise PcmError(
            "这个 WAV 的内部编码是「%s」，不是 PCM/浮点 —— 标准库解不了。"
            "请先用音频工具转成 PCM WAV 再分析。" % info.get("codec")
        )
    if not info.get("data_size"):
        raise PcmError("WAV 里没有音频数据（data 块为空）")
    if not info.get("sample_rate") or not info.get("channels"):
        raise PcmError("WAV 头里的采样率或声道数缺失，无法解码")
    return info


def _decode_chunk(raw, bit_depth, codec_id):
    """把一块原始字节解成样本数组 + 满量程。

    8 位 WAV 是**无符号**的（PCM 规范如此），要减 128 才是带符号值；
    24 位没有对应的 array 类型码，走「补成 32 位再当有符号 int 读」的路子。
    """
    if codec_id == 3:
        entry = _FLOAT_TYPES.get(bit_depth)
        if entry is None:
            raise PcmError("不支持的浮点位深：%d 位" % bit_depth)
        type_code, full_scale = entry
    else:
        if bit_depth == 24:
            return _decode_int24(raw), 8388608.0
        entry = _INT_TYPES.get(bit_depth)
        if entry is None:
            raise PcmError("不支持的位深：%d 位（支持 8 / 16 / 24 / 32）" % bit_depth)
        type_code, full_scale = entry

    samples = array.array(type_code)
    samples.frombytes(raw)
    if sys.byteorder == "big":  # WAV 是小端；big-endian 机器上要翻一次
        samples.byteswap()
    if bit_depth == 8 and codec_id == 1:
        # 无符号 → 有符号：整体减 128。用 array 的逐元素运算做不到，转成有符号数组。
        shifted = array.array("h", bytes(len(samples) * 2))
        for index, value in enumerate(samples):
            shifted[index] = value - 128
        return shifted, full_scale
    return samples, full_scale


def _decode_int24(raw):
    """24 位 PCM：把每个 3 字节组补成 4 字节，最高字节填符号扩展。

    这样可以一次性用 ``array('i')`` 读出来，避免逐样本 ``int.from_bytes``
    （那会慢 10 倍以上）。填充用的是 C 级切片赋值，只有算符号扩展字节时
    过一遍 Python 循环。
    """
    usable = len(raw) - (len(raw) % 3)
    count = usable // 3
    if count == 0:
        return array.array("i")
    raw = raw[:usable]
    low = raw[0::3]
    mid = raw[1::3]
    high = raw[2::3]
    padded = bytearray(count * 4)
    padded[0::4] = low
    padded[1::4] = mid
    padded[2::4] = high
    padded[3::4] = bytes(0xFF if byte >= 0x80 else 0x00 for byte in high)
    samples = array.array("i")
    samples.frombytes(bytes(padded))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _downmix(samples, channels):
    """多声道 → 单声道求和。

    刻意**不做除法** —— 求和后的 RMS 只要在换算 dB 时除以
    ``channels * 满量程``，结果完全一样，却省掉了每个样本一次浮点除法。
    立体声用 ``map(operator.add)`` 走 C 级循环，是这里唯一值得的优化。
    """
    if channels == 1:
        return samples
    if channels == 2:
        from operator import add

        # 求和可能超出 int32，用 'q'（8 字节）保证不溢出；内存翻倍但仍是常数级
        return array.array("q", map(add, samples[0::2], samples[1::2]))
    out = array.array("q", bytes(len(samples) // channels * 8))
    for base in range(0, len(samples) - channels + 1, channels):
        total = 0
        for offset in range(channels):
            total += samples[base + offset]
        out[base // channels] = total
    return out


# ---------------------------------------------------------------- 分析


def analyze(path, frame_ms=20.0, threshold_db=-45.0, min_silence_ms=300.0,
            buckets=600, series_points=1200, max_frames=200000,
            progress=None, checkpoint=None):
    """分析一个 WAV，返回波形峰值 + 静音区间 + 逐帧 RMS/过零率。

    :param frame_ms: 逐帧指标的帧长（默认 20 ms）
    :param threshold_db: 低于此 RMS（dBFS）的帧判为静音，默认 -45 dBFS
    :param min_silence_ms: 短于此长度的静音段会被丢弃，默认 300 ms
        （避免把词与词之间的换气当成「静音段落」）
    :param buckets: 波形峰值分桶数
    :param series_points: 返回的 RMS / 过零率序列最多多少个点（超出就按组抽样）
    :param max_frames: 帧数上限；超长文件会自动加大帧长以**限制内存**，
        此时响应里的 ``frame_ms`` 会大于请求值，静音边界的量化误差也随之变大
    :param progress: ``progress(done_bytes, total_bytes)``，用于任务进度
    :param checkpoint: 无参可调用对象，每块调一次；抛异常即中止（取消/暂停）

    **精度与容忍度**（写测试时按这个来）：

    * 静音边界量化到**帧**，所以误差上界就是当时的 ``frame_ms``（默认 20 ms）。
    * 跨越边界的那个帧只要有信号能量（哪怕 10% 时长）就会被判为「有声」，
      因此实测出来的静音段通常比理论值**短一个帧长**。
    * 实测吞吐（本机、``tests/test_pcm.py`` 会打印出来）：**5.9 ~ 6.6 M 样本/秒**，
      即 44.1 kHz 立体声约 70 秒音频/秒 —— 一分钟的歌约 0.9 秒，一小时的录音约 50 秒。
      因此长文件走任务队列，同步接口只接受短音频（见 ``analyze_sync_seconds``，
      默认 120 秒 ≈ 2.8 秒 CPU）。
    """
    info = read_wav_info(path)
    rate = int(info["sample_rate"])
    channels = int(info["channels"])
    bit_depth = int(info["bit_depth"])
    codec_id = int(info["codec_id"])
    data_offset = int(info["data_offset"])
    data_size = int(info["data_size"])
    total_samples = data_size // max(1, channels * max(1, bit_depth // 8))
    if total_samples <= 0:
        raise PcmError("WAV 里没有可分析的音频数据")

    frame_len = max(1, int(round(rate * float(frame_ms) / 1000.0)))
    # 帧数上限：只影响内存（每帧 4 个数组元素），超长文件自动降分辨率
    if total_samples / frame_len > max_frames:
        frame_len = int(math.ceil(total_samples / float(max_frames)))
    actual_frame_ms = frame_len * 1000.0 / rate

    full_scale = channels * _full_scale(bit_depth, codec_id)

    rms_series = array.array("f")
    zcr_series = array.array("f")
    peak_min = array.array("f")
    peak_max = array.array("f")

    block_bytes = max(1, (channels * (bit_depth // 8)))
    # 块大小取整数个样本帧，且不超过 CHUNK_BYTES
    chunk_bytes = max(block_bytes, (CHUNK_BYTES // block_bytes) * block_bytes)

    total = 0.0
    crossings = 0
    prev_negative = False
    frame_pos = 0
    frame_lo = None
    frame_hi = None

    def _close_frame():
        """一帧结束：把 RMS（dBFS）、过零率与峰值记下来。"""
        nonlocal total, crossings, frame_pos, frame_lo, frame_hi
        rms_raw = math.sqrt(total / frame_pos) if frame_pos else 0.0
        if rms_raw <= 0:
            db = SILENCE_DB_FLOOR
        else:
            db = 20.0 * math.log10(rms_raw / full_scale)
        rms_series.append(db)
        zcr_series.append(crossings / float(frame_pos) if frame_pos else 0.0)
        value_lo = (frame_lo or 0) / full_scale
        value_hi = (frame_hi or 0) / full_scale
        peak_min.append(max(-1.0, min(1.0, value_lo)))
        peak_max.append(max(-1.0, min(1.0, value_hi)))
        total = 0.0
        crossings = 0
        frame_pos = 0
        frame_lo = None
        frame_hi = None

    processed = 0
    with open(path, "rb") as fh:
        fh.seek(data_offset)
        remaining = data_size
        while remaining > 0:
            if checkpoint is not None:
                checkpoint()
            raw = fh.read(min(chunk_bytes, remaining))
            if not raw:
                break
            remaining -= len(raw)
            processed += len(raw)
            # 丢掉不足一个样本帧的尾巴，保证解码对齐
            usable = len(raw) - (len(raw) % block_bytes)
            if usable <= 0:
                continue
            values, _scale = _decode_chunk(raw[:usable], bit_depth, codec_id)
            values = _downmix(values, channels)
            count = len(values)
            pos = 0
            while pos < count:
                take = min(frame_len - frame_pos, count - pos)
                segment = values[pos:pos + take]
                seg_lo = min(segment)
                seg_hi = max(segment)
                if frame_lo is None or seg_lo < frame_lo:
                    frame_lo = seg_lo
                if frame_hi is None or seg_hi > frame_hi:
                    frame_hi = seg_hi
                for value in segment:
                    total += value * value
                    negative = value < 0
                    if negative != prev_negative:
                        crossings += 1
                        prev_negative = negative
                pos += take
                frame_pos += take
                if frame_pos >= frame_len:
                    _close_frame()
            if progress is not None:
                progress(processed, data_size)
        if frame_pos > 0:
            _close_frame()

    frames = len(rms_series)
    if frames == 0:
        raise PcmError("WAV 里没有可分析的音频数据")

    duration = total_samples / float(rate)
    silences = detect_silence(rms_series, actual_frame_ms / 1000.0,
                              threshold_db, min_silence_ms, duration)
    silence_total = sum(item["duration"] for item in silences)

    return {
        "ok": True,
        "path": path,
        "size": info.get("size"),
        "format": info.get("format") or "WAV",
        "codec": info.get("codec"),
        "sample_rate": rate,
        "channels": channels,
        "bit_depth": bit_depth,
        "duration": round(duration, 3),
        "total_samples": total_samples,
        "frame_ms": round(actual_frame_ms, 3),
        "requested_frame_ms": round(float(frame_ms), 3),
        "frames": frames,
        "threshold_db": round(float(threshold_db), 2),
        "min_silence_ms": round(float(min_silence_ms), 2),
        "rms_db": _decimate(rms_series, series_points, "max"),
        "zcr": _decimate(zcr_series, series_points, "mean"),
        "peaks": _bucket_peaks(peak_min, peak_max, duration, buckets),
        "peak_buckets": buckets,
        "series_points": series_points,
        "silences": silences,
        "silence_count": len(silences),
        "silence_total": round(silence_total, 3),
        "speech_total": round(max(0.0, duration - silence_total), 3),
        "tags": info.get("tags") or {},
    }


def _full_scale(bit_depth, codec_id):
    if codec_id == 3:
        return float(_FLOAT_TYPES.get(bit_depth, ("f", 1.0))[1])
    if bit_depth == 24:
        return 8388608.0
    return float(_INT_TYPES.get(bit_depth, ("h", 32768.0))[1])


def detect_silence(rms_db, frame_seconds, threshold_db, min_silence_ms, duration):
    """从逐帧 RMS 序列里找静音段。

    判据只有一条：**连续的帧 RMS ≤ 阈值**即构成一段静音；片段短于
    ``min_silence_ms`` 的丢弃（那是词间换气，不是真静音）。
    """
    runs = []
    start = None
    for index, value in enumerate(rms_db):
        if value <= threshold_db:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(rms_db)))

    out = []
    for first, last in runs:
        begin = first * frame_seconds
        end = min(last * frame_seconds, duration)
        if (end - begin) * 1000.0 + 1e-9 < float(min_silence_ms):
            continue
        out.append({
            "start": round(begin, 3),
            "end": round(end, 3),
            "duration": round(end - begin, 3),
        })
    return out


def _decimate(series, points, mode):
    """把逐帧序列抽稀到 ``points`` 个点，便于前端画图。

    RMS 取组内**最大值**（保留能量包络的尖峰，取平均会把瞬态抹平）；
    过零率取**平均值**（它是比率，取最大没有意义）。
    """
    count = len(series)
    if count <= points or points <= 0:
        return [round(float(value), 2) for value in series]
    group = int(math.ceil(count / float(points)))
    out = []
    for start in range(0, count, group):
        chunk = series[start:start + group]
        if not chunk:
            continue
        if mode == "max":
            out.append(round(max(chunk), 2))
        else:
            out.append(round(sum(chunk) / len(chunk), 4))
    return out


def _bucket_peaks(peak_min, peak_max, duration, buckets):
    """把逐帧 min/max 归并成 ``buckets`` 个时间桶。"""
    frames = len(peak_min)
    if frames == 0 or buckets <= 0:
        return []
    group = max(1, int(math.ceil(frames / float(buckets))))
    out = []
    for start in range(0, frames, group):
        stop = min(frames, start + group)
        low = min(peak_min[start:stop])
        high = max(peak_max[start:stop])
        out.append({
            "t": round(start * (duration / frames), 4) if frames else 0.0,
            "min": round(low, 4),
            "max": round(high, 4),
        })
    return out


def supported(path):
    """这个文件能不能做波形分析（给前端用）。"""
    return os.path.splitext(path)[1].lower() in audiometa.DECODABLE_EXTS
