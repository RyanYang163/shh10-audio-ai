"""字幕格式互转：LRC / SRT / VTT / TXT 双向转换。

这是本应用的**日常价值所在** —— 音频转写出来的是一行行文本，用户要的是能用的
歌词 / 字幕文件。所以这里的重点是**容错**与**不变式**：

* 容错：不标准的文件要尽量解析出来，而不是报错
  - LRC 的时间戳 `[mm:ss]` / `[mm:ss.xx]` / `[mm:ss.xxx]` / `[mm:ss:xx]` 都认
  - SRT 的毫秒分隔符 `,` 与 `.` 混用也认
  - VTT 的 `MM:SS.mmm` 与 `HH:MM:SS.mmm` 都认；`NOTE` / `STYLE` / `REGION` 块跳过
  - 编码：UTF-8（含 BOM）/ UTF-16（看 BOM）/ GB18030 自动识别
  - 块序号可有可无、多余的空行、CRLF 都能吃下
* 不变式：**LRC → SRT → LRC 往返后，逐条 cue 的时间戳与文本完全一致**
  （LRC 的时间精度是 10 ms、SRT 是 1 ms，所以这个方向无损；
  反向 SRT → LRC 会按 10 ms 取整，属于格式精度所限，不是 bug）。
  注意不变式管的是**时间戳与文本**，不是字节级排版：LRC 里
  ``[00:20.50][00:40.25]副歌`` 这种一行多时间戳的写法往返后会被展开成两行，
  元数据标签（``[ti:]`` 等）SRT 没有地方放，也会丢 —— 这两条都在文档与用例里写明。

内部统一用一条 cue 记录：``{"index", "start", "end", "text"}``，
其中 ``start`` / ``end`` 是秒（float），TXT 这类没有时间轴的来源为 ``None``。
"""

import math
import os
import re

FORMATS = ("lrc", "srt", "vtt", "txt")
EXT_BY_FORMAT = {"lrc": ".lrc", "srt": ".srt", "vtt": ".vtt", "txt": ".txt"}
FORMAT_BY_EXT = {".lrc": "lrc", ".srt": "srt", ".vtt": "vtt",
                 ".txt": "txt", ".text": "txt", ".smi": "srt"}

#: LRC 默认给最后一行 / 只有一行的时长（秒）
DEFAULT_TAIL_SECONDS = 2.0
#: 歌词模式下 merge_short 的默认值（秒）：短于它的行会被合并
LYRIC_MERGE_SECONDS = 1.5
#: 合并短句时允许的最大间隔（秒）：间隔太大就不合并，否则一句歌词会挂几十秒
DEFAULT_MERGE_GAP = 1.5
#: TXT 转成带时间轴的格式时，每行分配多长（秒）
DEFAULT_LINE_SECONDS = 3.0


class SubtitleError(Exception):
    """字幕解析 / 转换失败（内容里没有任何可识别的时间轴等）。"""


# ---------------------------------------------------------------- 时间


def _fraction_seconds(digits):
    """毫秒 / 厘秒 / 十分之一秒统一按「小数点后补足 3 位」解释。

    ``"5"`` → 0.5 s、``"34"`` → 0.34 s、``"340"`` → 0.34 s。
    LRC 的 ``[mm:ss.xx]`` 与 SRT 的 ``,mm`` 因此可以共用同一条规则。
    """
    return int(str(digits).ljust(3, "0")) / 1000.0


#: 通用时钟：``[HH:]MM:SS[.,]mmm``
_CLOCK_RE = re.compile(r"^(?:(\d{1,3}):)?(\d{1,3}):(\d{1,3})(?:[.,](\d{1,3}))?$")
#: LRC 时钟：``MM:SS[.:]x``，另容忍 ``[MM:SS:cc]``（第三段当厘秒）
_LRC_CLOCK_RE = re.compile(r"^(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?$")


def parse_clock(text):
    """解析 ``HH:MM:SS.mmm`` / ``MM:SS.mmm`` / ``MM:SS``，失败返回 None。"""
    match = _CLOCK_RE.match((text or "").strip())
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return (int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
            + (_fraction_seconds(fraction) if fraction else 0.0))


def parse_lrc_clock(text):
    """解析 LRC 的 ``MM:SS`` 系列，失败返回 None。

    特意与 :func:`parse_clock` 分开：LRC 里的 ``[01:23:45]`` 是
    「1 分 23 秒 45 厘秒」（老式写法），不是「1 小时 23 分 45 秒」——
    歌词文件里出现小时几乎不可能，而这两种读法差了 3600 倍，
    必须按格式各自解释。
    """
    match = _LRC_CLOCK_RE.match((text or "").strip())
    if not match:
        return None
    minutes, seconds, fraction = match.groups()
    return int(minutes) * 60 + int(seconds) + (_fraction_seconds(fraction) if fraction else 0.0)


def format_lrc_time(seconds):
    """``MM:SS.xx``（分钟可以超过 59，LRC 就是这么用的）。"""
    centis = int(_round_half_up(max(0.0, seconds) * 100))
    return "%02d:%02d.%02d" % (centis // 6000, (centis // 100) % 60, centis % 100)


def format_srt_time(seconds):
    """``HH:MM:SS,mmm``"""
    millis = int(_round_half_up(max(0.0, seconds) * 1000))
    return "%02d:%02d:%02d,%03d" % (millis // 3600000, (millis // 60000) % 60,
                                    (millis // 1000) % 60, millis % 1000)


def format_vtt_time(seconds):
    """``HH:MM:SS.mmm``"""
    return format_srt_time(seconds).replace(",", ".")


def _round_half_up(value):
    """四舍五入（Python 内建 round 是「银行家舍入」，时间戳上会让人困惑）。"""
    return math.floor(value + 0.5)


# ---------------------------------------------------------------- 读文本


def decode_bytes(raw):
    """把字节解成文本并返回 ``(文本, 编码名)``。

    顺序：BOM（UTF-8 / UTF-16）→ UTF-8 → GB18030 → latin-1 兜底。
    **永不失败** —— 用户给的歌词文件可能是任何编码，读不出来也比报错强。
    """
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", "replace"), "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace"), "utf-16"
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1"


def read_text(path):
    """读一个字幕文件，返回 ``(文本, 编码名)``。换行统一成 ``\\n``。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    text, encoding = decode_bytes(raw)
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding


# ---------------------------------------------------------------- 识别格式


def detect_format(text, filename=None):
    """猜格式。**内容优先**，文件名只做兜底 —— 用户把 .lrc 存成 .txt 很常见。"""
    head = (text or "").lstrip("﻿").lstrip()
    if head[:6].upper().startswith("WEBVTT"):
        return "vtt"
    if re.search(r"(?m)^\s*\d+\s*$", head[:400]) and "-->" in head:
        return "srt"
    if "-->" in head:
        return "srt"
    if re.search(r"\[\s*\d{1,3}:\d{1,2}([.:]\d{1,3})?\s*\]", head):
        return "lrc"
    if filename:
        guessed = FORMAT_BY_EXT.get(os.path.splitext(filename)[1].lower())
        if guessed:
            return guessed
    return "txt"


# ---------------------------------------------------------------- 解析


def parse(text, fmt=None):
    """解析字幕文本，返回 ``{"format", "cues", "meta", "encoding"}`` 结构。

    ``fmt`` 为 None 时按内容自动识别。解析不出任何 cue 时抛
    :class:`SubtitleError` —— 给出可读原因，而不是返回一个空列表。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    chosen = (fmt or detect_format(text)).lower()
    if chosen not in FORMATS:
        raise SubtitleError("不认识的字幕格式：%s（支持 %s）" % (fmt, " / ".join(FORMATS)))

    if chosen == "lrc":
        cues, meta = _parse_lrc(text)
    elif chosen == "vtt":
        cues, meta = _parse_vtt(text)
    elif chosen == "srt":
        cues, meta = _parse_srt(text)
    else:
        cues, meta = _parse_txt(text)

    if not cues:
        raise SubtitleError(
            "没有解析出任何字幕行（按 %s 解析）。请确认文件里是否有时间轴行，"
            "或改用其它格式再试。" % chosen.upper()
        )
    return {"format": chosen, "cues": cues, "meta": meta}


def parse_file(path, fmt=None):
    text, encoding = read_text(path)
    result = parse(text, fmt)
    result["encoding"] = encoding
    result["path"] = path
    result["text"] = text
    return result


_LRC_TAG_RE = re.compile(r"^\[([a-zA-Z]{1,8}):(.*?)\]$")
_LRC_STAMP_RE = re.compile(r"\[(\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?)\]")


def _parse_lrc(text):
    """LRC 歌词：一行可以有多个时间戳（副歌复用），文本取时间戳之后的部分。"""
    cues = []
    meta = {}
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        stamps = _LRC_STAMP_RE.findall(stripped)
        if not stamps:
            tag = _LRC_TAG_RE.match(stripped)
            if tag:
                meta[tag.group(1).lower()] = tag.group(2).strip()
                continue
            continue  # 既不是时间戳行也不是标签：跳过（可能是一句说明文字）
        lyric = _LRC_STAMP_RE.sub("", stripped).strip()
        for stamp in stamps:
            start = parse_lrc_clock(stamp)
            if start is None:
                continue
            cues.append({"start": start, "end": None, "text": lyric})

    if not cues:
        raise SubtitleError(
            "LRC 里没有找到形如 [mm:ss.xx] 的时间戳 —— 文件可能不是歌词，"
            "或者时间戳格式无法识别。"
        )

    # 按时间排序（同一时间戳的多行保持原有顺序），末尾时间取下一行起点
    cues.sort(key=lambda cue: cue["start"])
    for index, cue in enumerate(cues):
        if index + 1 < len(cues):
            cue["end"] = cues[index + 1]["start"]
        else:
            cue["end"] = cue["start"] + DEFAULT_TAIL_SECONDS
        cue["index"] = index + 1
    _round_cues(cues)
    offset_ms = meta.get("offset")
    if offset_ms is not None:
        try:
            # 只记录不自动应用：各播放器对 [offset:] 的正负号约定不一致，
            # 擅自应用会让「本来对的时间轴」变错。要平移请用 offset 参数。
            meta["offset_ms"] = int(offset_ms)
        except ValueError:
            pass
    return cues, meta


_SRT_ARROW_RE = re.compile(r"^\s*(?P<start>[0-9:.,]+)\s*-->\s*(?P<end>[0-9:.,]+)(?P<rest>.*)$")


def _parse_srt(text):
    """SRT：块之间用空行分隔，序号行可有可无，时间轴的毫秒分隔符 ``,`` ``.`` 都认。"""
    cues = []
    meta = {}
    for block in re.split(r"\n[ \t]*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        arrow_index = None
        for index, line in enumerate(lines[:3]):
            if "-->" in line:
                arrow_index = index
                break
        if arrow_index is None:
            # 没时间轴的块：整份文件都没时间轴的话，最后会以「没有字幕行」报错
            continue
        match = _SRT_ARROW_RE.match(lines[arrow_index])
        if not match:
            continue
        start = parse_clock(match.group("start"))
        end = parse_clock(match.group("end"))
        if start is None or end is None:
            continue
        body = "\n".join(lines[arrow_index + 1:]).strip("\n")
        cues.append({"start": start, "end": end, "text": body})

    if not cues:
        raise SubtitleError(
            "SRT 里没有找到形如 00:00:01,000 --> 00:00:04,000 的时间轴行。"
        )
    for index, cue in enumerate(cues):
        cue["index"] = index + 1
    return _round_cues(cues), meta


def _parse_vtt(text):
    """VTT：跳过 ``WEBVTT`` 头、``NOTE`` / ``STYLE`` / ``REGION`` 块与 cue 设置。"""
    body = text
    if body.lstrip("﻿").lstrip().upper().startswith("WEBVTT"):
        parts = body.split("\n")
        head = parts[0]
        body = "\n".join(parts[1:])
        if not head.upper().strip().startswith("WEBVTT"):
            body = text
    cues = []
    meta = {}
    for block in re.split(r"\n[ \t]*\n", body):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        first = lines[0].strip().upper()
        if first.startswith(("NOTE", "STYLE", "REGION")):
            continue
        arrow_index = None
        for index, line in enumerate(lines[:3]):
            if "-->" in line:
                arrow_index = index
                break
        if arrow_index is None:
            continue
        match = _SRT_ARROW_RE.match(lines[arrow_index])
        if not match:
            continue
        start = parse_clock(match.group("start"))
        end = parse_clock(match.group("end"))
        if start is None or end is None:
            continue
        # 时间轴行后面可能跟着 cue 设置（line:0 position:20%），不用管
        cue_text = "\n".join(lines[arrow_index + 1:]).strip("\n")
        cues.append({"start": start, "end": end, "text": cue_text})

    if not cues:
        raise SubtitleError("VTT 里没有找到任何 cue（时间轴行）。")
    for index, cue in enumerate(cues):
        cue["index"] = index + 1
    return _round_cues(cues), meta


def _parse_txt(text):
    """纯文本：一行一条，**没有时间轴**。转成 SRT/VTT 时会自动排时间。"""
    cues = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            cues.append({"start": None, "end": None, "text": stripped})
    return cues, {}


# ---------------------------------------------------------------- 变换


def _round_cues(cues, digits=3):
    """把时间戳收敛到毫秒 —— 纯浮点加减会留下 64.99000000000001 这种尾巴，
    直接进 JSON / 数据库很难看，也不便于断言。"""
    for cue in cues:
        for key in ("start", "end"):
            if cue.get(key) is not None:
                cue[key] = round(float(cue[key]), digits)
    return cues


def _duration(cue):
    if cue.get("start") is None or cue.get("end") is None:
        return 0.0
    return max(0.0, cue["end"] - cue["start"])


def _join_text(parts):
    """合并文本：两边都是中日韩字符时直接相接，否则补一个空格。

    中文歌词逐行合并成「今天 天气 很好」会比「今天天气很好」难看，
    而英文 `hello` + `world` 不空格又会连成一个词。
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if not out:
            out = part
            continue
        if _is_cjk(out[-1]) and _is_cjk(part[0]):
            out += part
        else:
            out += " " + part
    return out


def _is_cjk(char):
    code = ord(char)
    return (0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFF65)


def merge_short_cues(cues, min_duration, max_gap=DEFAULT_MERGE_GAP):
    """把「过短的行」合并成合理的长句（歌词模式的实现）。

    规则：连续的若干行，只要**每一行**的时长都短于 ``min_duration``，
    且与下一行的间隔不超过 ``max_gap``，就并成一条；
    合并后的时间轴取**首行起点到末行终点**，文本用空格 / 直接相接拼起来。

    间隔条件是必要的：没有它，一段间奏前后的两句歌词会被并成一条
    挂几十秒的字幕。没有时间轴的行（TXT）不参与合并。
    """
    if not cues:
        return cues, 0
    threshold = float(min_duration)
    if threshold <= 0:
        return cues, 0

    groups = []
    current = [cues[0]]
    for cue in cues[1:]:
        previous = current[-1]
        mergeable = (
            previous.get("start") is not None and cue.get("start") is not None
            and _duration(previous) < threshold
            and (cue["start"] - (previous.get("end") or previous["start"])) <= max_gap
        )
        if mergeable:
            current.append(cue)
        else:
            groups.append(current)
            current = [cue]
    groups.append(current)

    out = []
    for group in groups:
        if len(group) == 1:
            out.append(group[0])
            continue
        starts = [cue["start"] for cue in group if cue.get("start") is not None]
        ends = [cue["end"] for cue in group if cue.get("end") is not None]
        out.append({
            "start": min(starts) if starts else None,
            "end": max(ends) if ends else None,
            "text": _join_text([cue["text"] for cue in group]),
        })
    for index, cue in enumerate(out):
        cue["index"] = index + 1
    return out, len(cues) - len(out)


def shift_cues(cues, offset):
    """时间轴整体平移 ``offset`` 秒（可为负）。

    起点被夹到 0 时，这条 cue 的**可见时长会变短**：落在 0 之前的那一段
    在 LRC / SRT / VTT 里根本无法表示（不允许负时间戳）。所以第一行平移
    ``-2.5`` 秒后是 ``[0, 原终点-2.5]``，而不是被拉长成「保持原时长」——
    后者等于凭空多给出一段并不存在的时间。
    发生夹取时返回值会带上 ``clamped=True``，让响应能如实告知用户。
    """
    offset = float(offset or 0)
    if not offset:
        return cues, False
    clamped = False
    out = []
    for cue in cues:
        item = dict(cue)
        if item.get("start") is not None:
            start = item["start"] + offset
            if start < 0:
                clamped = True
                start = 0.0
            item["start"] = start
        if item.get("end") is not None:
            item["end"] = max(item.get("start") or 0.0, item["end"] + offset)
        out.append(item)
    return out, clamped


def lay_out_untimed(cues, line_seconds=DEFAULT_LINE_SECONDS, start_at=0.0):
    """给没有时间轴的行（TXT）铺一条顺序时间轴，让它们能输出成 SRT / VTT。"""
    out = []
    cursor = float(start_at)
    step = max(0.1, float(line_seconds or DEFAULT_LINE_SECONDS))
    for cue in cues:
        item = dict(cue)
        if item.get("start") is None:
            item["start"] = cursor
            item["end"] = cursor + step
        cursor = item["end"]
        out.append(item)
    return out


# ---------------------------------------------------------------- 渲染


def render(cues, fmt, meta=None, line_seconds=DEFAULT_LINE_SECONDS):
    """把 cue 列表渲染成目标格式的文本。"""
    fmt = (fmt or "srt").lower()
    if fmt not in FORMATS:
        raise SubtitleError("不支持的目标格式：%s（支持 %s）" % (fmt, " / ".join(FORMATS)))
    meta = meta or {}

    if fmt == "txt":
        return "\n".join(cue["text"].replace("\n", " ") for cue in cues) + "\n"
    if fmt == "lrc":
        return _render_lrc(cues, meta)
    if fmt == "vtt":
        return _render_vtt(cues)
    return _render_srt(cues)


def _render_lrc(cues, meta):
    lines = []
    for key in ("ti", "ar", "al", "by", "offset"):
        if meta.get(key):
            lines.append("[%s:%s]" % (key, meta[key]))
    if lines:
        lines.append("")
    for cue in cues:
        if cue.get("start") is None:
            continue
        text = cue["text"].replace("\n", " ").strip()
        lines.append("[%s]%s" % (format_lrc_time(cue["start"]), text))
    return "\n".join(lines) + "\n"


def _render_srt(cues):
    blocks = []
    for index, cue in enumerate(cues, 1):
        if cue.get("start") is None or cue.get("end") is None:
            continue
        blocks.append("%d\n%s --> %s\n%s" % (
            index, format_srt_time(cue["start"]), format_srt_time(cue["end"]),
            cue["text"].strip("\n"),
        ))
    return "\n\n".join(blocks) + "\n"


def _render_vtt(cues):
    blocks = ["WEBVTT"]
    for index, cue in enumerate(cues, 1):
        if cue.get("start") is None or cue.get("end") is None:
            continue
        blocks.append("%d\n%s --> %s\n%s" % (
            index, format_vtt_time(cue["start"]), format_vtt_time(cue["end"]),
            cue["text"].strip("\n"),
        ))
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------- 转换入口


def convert(cues, fmt, offset=0.0, merge_short=0.0,
            merge_max_gap=DEFAULT_MERGE_GAP, line_seconds=DEFAULT_LINE_SECONDS,
            meta=None):
    """一条完整的转换流水线，返回 ``(文本, 统计)``。

    顺序是有讲究的：

    1. **先合并短句** —— 要在**原始时间轴上**判断「这行是不是太短」，
       先平移再过阈值会把判断依据改掉；
    2. **再整体平移** —— 用户要的是「整个时间轴挪 2.5 秒」；
    3. **最后给没时间轴的行排时间** —— TXT 转 SRT 时才会走到这一步。
    """
    stats = {"cues_in": len(cues), "merged": 0, "offset": round(float(offset or 0), 3),
             "clamped": False}
    working = cues
    if merge_short and float(merge_short) > 0:
        working, merged = merge_short_cues(working, merge_short, merge_max_gap)
        stats["merged"] = merged
    if offset:
        working, clamped = shift_cues(working, offset)
        stats["clamped"] = clamped
    # 目标格式需要时间轴（LRC 也一样要），而来自 TXT 的行没有时间 —— 顺序铺一条。
    # LRC 也必须在这一步里排上时间，否则 _render_lrc 会把所有行都跳过，输出空文件。
    if fmt in ("srt", "vtt", "lrc") and any(cue.get("start") is None for cue in working):
        working = lay_out_untimed(working, line_seconds)
        stats["laid_out"] = True
    stats["cues_out"] = len(working)
    return render(working, fmt, meta=meta, line_seconds=line_seconds), stats


def convert_text(text, fmt, source_format=None, **kwargs):
    """文本进、文本出：解析 → 转换 → 渲染。"""
    parsed = parse(text, source_format)
    # 源文件里的元数据（LRC 的 [ti:]/[ar:]）默认带过去，调用方也可以显式覆盖
    kwargs.setdefault("meta", parsed.get("meta"))
    output, stats = convert(parsed["cues"], fmt, **kwargs)
    stats["source_format"] = parsed["format"]
    stats["target_format"] = (fmt or "").lower()
    return output, stats


def convert_file(source, output_dir, fmt, source_format=None, **kwargs):
    """转换一个文件并写到 ``output_dir``，返回结果记录。"""
    parsed = parse_file(source, source_format)
    kwargs.setdefault("meta", parsed.get("meta"))
    output, stats = convert(parsed["cues"], fmt, **kwargs)
    stem = os.path.splitext(os.path.basename(source))[0][:120]
    target = unique_path(output_dir, stem + EXT_BY_FORMAT.get(fmt.lower(), "." + fmt.lower()))
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(output)
    return {
        "ok": True, "source": source, "output": target,
        "source_format": parsed["format"], "target_format": fmt.lower(),
        "encoding": parsed.get("encoding"), "bytes": os.path.getsize(target),
        "stats": stats,
    }


def unique_path(directory, filename):
    """同名时加 ``-1`` / ``-2`` 后缀，**绝不覆盖已有文件**。

    用户的输出目录里可能有上一次的转换结果，静默覆盖是不可接受的
    （而且这是唯一会动用户目录的地方，必须保守）。
    """
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s-%d%s" % (stem, index, ext))
        index += 1
        if index > 999:
            raise SubtitleError("输出目录里同名文件太多（超过 999 个）")
    return candidate


def summarize(cues, limit=10):
    """给前端的一小段预览（不要把所有 cue 都塞进日志或列表里）。"""
    return [{"index": cue.get("index"), "start": cue.get("start"), "end": cue.get("end"),
             "text": cue["text"][:200]} for cue in cues[:limit]]
