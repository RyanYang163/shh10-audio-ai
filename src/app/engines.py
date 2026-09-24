"""可选的语音转写引擎（本地 faster-whisper 命令 / 远程 OpenAI 兼容接口）。

**核心功能不依赖这里任何一行。** 元数据读取、波形分析、静音检测、字幕互转
全部由本应用用标准库实现；转写只是「检测到才出现」的增强：

* 本机有 ``faster-whisper`` 命令行 → 可用
* 管理员自行配置了 OpenAI 兼容的 ``/v1/audio/transcriptions`` 接口并**显式确认启用** → 可用
* 两者都没有 → 界面明确显示「本地转写不可用」并给出指引，**应用照常可用**

------------------------------------------------------------------ 安全上刻意做窄

* 只用 ``subprocess`` 的**参数列表**形式调用，**绝不拼 shell 字符串** ——
  没有 ``shell=True``，就没有命令注入面；所有调用都带超时。
* 命令与参数映射是固定的：模型名、语言码这类参数来自白名单式的校验，
  不接受任意命令行。
* 远程接口的 API Key 单独存 ``data/config/secrets.json``（权限 600），
  任何接口响应、日志与状态里都**不回显**它 —— 只回 ``has_key: true``。
* 不下载、不安装任何引擎与模型：只调用系统上已有的，或管理员自己配的接口。
"""

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import subtitles

#: 按优先级探测的本机命令（不同安装方式给出的命令名不一样）
LOCAL_COMMANDS = ("faster-whisper", "faster_whisper", "whisper-ctranslate2", "whisper")

#: 允许请求的响应格式（OpenAI 兼容接口的标准取值）
REMOTE_FORMATS = ("srt", "vtt", "text", "verbose_json", "json")

DEFAULT_LOCAL_TIMEOUT = 3600
DEFAULT_REMOTE_TIMEOUT = 600

_state = {"checked": False, "available": False, "name": None, "path": None,
          "version": "", "error": None}
_lock = threading.Lock()


class EngineError(Exception):
    """引擎调用失败（命令不存在、退出码非 0、接口报错等）。"""


# ---------------------------------------------------------------- 本机引擎


def detect_local(force=False):
    """探测本机有没有可用的转写命令。结果会缓存（前端每次进页面都会问）。"""
    with _lock:
        if _state["checked"] and not force:
            return dict(_state)
        _state["checked"] = True
        for name in LOCAL_COMMANDS:
            path = shutil.which(name)
            if not path:
                continue
            version, error = _probe_command(path)
            _state.update(available=error is None, name=name, path=path,
                          version=version, error=error)
            return dict(_state)
        _state.update(available=False, name=None, path=None, version="",
                      error="系统上没有找到 faster-whisper 命令")
        return dict(_state)


def _probe_command(path):
    """``--help`` 探一次，确认这个命令真的能跑（有 which 结果但跑不起来的情况真实存在）。"""
    try:
        completed = subprocess.run([path, "--help"], capture_output=True,
                                   timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return "", "命令 %s 响应超时" % os.path.basename(path)
    except OSError as exc:
        return "", "无法执行 %s：%s" % (os.path.basename(path), exc)
    output = (completed.stdout or b"") + (completed.stderr or b"")
    first = output.decode("utf-8", "replace").strip().splitlines()
    if completed.returncode != 0 and not first:
        return "", "命令 %s 返回码 %d" % (os.path.basename(path), completed.returncode)
    return (first[0][:200] if first else ""), None


def local_status():
    """给前端的本机引擎状态。"""
    info = detect_local()
    return {
        "name": "faster-whisper",
        "available": info["available"],
        "command": info["name"],
        "version": info["version"],
        "detail": ("已检测到：%s" % (info["version"] or info["name"]))
                  if info["available"] else info["error"],
        "enables": ["transcribe"],
    }


def build_local_args(executable, source, output_dir, model="small",
                     language=None, output_format="srt"):
    """构造调用参数（**纯函数**，便于断言「没有 shell、参数是列表」）。

    参数取值来自固定的集合校验，不接受任意命令行 —— 调用方只能选
    模型名与语言码，不能塞进 ``;`` / ``|`` 之类的东西。
    """
    if not safe_token(model):
        raise EngineError("模型名含非法字符：%r" % model)
    if language and not safe_token(language):
        raise EngineError("语言码含非法字符：%r" % language)
    if output_format not in ("srt", "vtt", "txt", "json", "lrc"):
        raise EngineError("不支持的输出格式：%s" % output_format)
    args = [str(executable), str(source),
            "--model", str(model),
            "--output_dir", str(output_dir),
            "--output_format", "srt" if output_format == "lrc" else output_format]
    if language:
        args += ["--language", str(language)]
    return args


def safe_token(text):
    """只放行 **ASCII** 字母数字与 ``._-`` —— 参数一律不接受其它字符。

    为什么显式要求 ASCII：``"中文".isalnum()`` 在 Python 里是 ``True``，
    只用 ``isalnum()`` 做白名单会让「只放行字母数字」这句注释变成假话。
    这里虽然只用于 argv 列表（不经过 shell），但仍然按声明的口径收紧。
    """
    return bool(text) and all(
        ch.isascii() and (ch.isalnum() or ch in "._-") for ch in str(text))


def run_local(executable, source, work_dir, model="small", language=None,
              output_format="srt", timeout=DEFAULT_LOCAL_TIMEOUT):
    """调用本机命令转写，返回 ``{"output": 路径, "format": 格式}``。

    **绝不使用 ``shell=True``**：命令是列表，参数是固定映射，
    因此不存在拼接注入的可能。超时由 ``timeout`` 控制。
    """
    os.makedirs(work_dir, exist_ok=True)
    args = build_local_args(executable, source, work_dir, model, language, output_format)
    try:
        completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise EngineError("本机转写超时（超过 %d 秒）" % timeout)
    except OSError as exc:
        raise EngineError("无法启动转写命令：%s" % exc)

    if completed.returncode != 0:
        tail = _tail(completed.stderr) or _tail(completed.stdout) or "无输出"
        raise EngineError("转写命令退出码 %d：%s" % (completed.returncode, tail))

    suffix = ".srt" if output_format == "lrc" else "." + output_format
    stem = os.path.splitext(os.path.basename(source))[0]
    for candidate in (os.path.join(work_dir, stem + suffix),
                      os.path.join(work_dir, stem + ".srt"),
                      os.path.join(work_dir, stem + ".txt")):
        if os.path.isfile(candidate):
            return {"output": candidate, "format": os.path.splitext(candidate)[1].lstrip(".")}
    raise EngineError(
        "转写命令报告成功，但输出目录里没有找到结果文件（%s%s）—— "
        "可能是这个版本的命令行参数不同，请查看应用日志。" % (stem, suffix)
    )


def _tail(raw, lines=3, limit=400):
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace").strip().splitlines()
    return " / ".join(line.strip() for line in text[-lines:])[:limit]


# ---------------------------------------------------------------- 远程接口


def validate_base_url(url):
    """只接受 http(s)，并且必须是带主机的完整地址。"""
    text = str(url or "").strip().rstrip("/")
    if not text:
        raise EngineError("没有填写接口地址")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        raise EngineError("接口地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise EngineError("接口地址里没有主机名：%s" % text)
    return text


def remote_settings(settings):
    """从运行时配置里取出远程引擎的设置（**不含任何凭据**）。"""
    return {
        "enabled": bool(settings.get("remote_enabled", False)),
        "base_url": str(settings.get("remote_base_url", "") or ""),
        "model": str(settings.get("remote_model", "whisper-1") or "whisper-1"),
        "language": str(settings.get("remote_language", "") or ""),
    }


def read_secrets(path):
    """读凭据文件；不存在或损坏都返回空字典（**不能因为凭据读不出就崩**）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_secrets(path, payload):
    """原子写凭据文件并设权限 600。

    Windows 上 ``chmod`` 基本无效（NTFS ACL 不认 POSIX 位），这里不假装它生效 ——
    Debian/TOS 上才是它真正起作用的地方。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def public_secrets(secrets):
    """对外可暴露的凭据摘要 —— **只有 has_key，没有 key 本身**。"""
    key = secrets.get("remote_api_key") or ""
    return {"has_key": bool(key), "key_length": len(key) if key else 0}


def public_remote(settings, secrets):
    config = remote_settings(settings)
    config["has_key"] = bool(secrets.get("remote_api_key"))
    config["ready"] = bool(config["enabled"] and config["base_url"] and config["has_key"])
    return config


def build_multipart(fields, file_field, file_path, filename=None):
    """手搓 multipart/form-data（标准库没有现成的编码器）。

    返回 ``(content_type, body)``。刻意不用 ``requests`` / ``urllib3``：
    包内不能有第三方依赖。
    """
    boundary = "----shh10audioai%s" % os.urandom(12).hex()
    name = filename or os.path.basename(file_path)
    parts = []
    for key, value in (fields or {}).items():
        if value in (None, ""):
            continue
        parts.append(("--%s\r\n" % boundary).encode("utf-8"))
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode("utf-8"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(("--%s\r\n" % boundary).encode("utf-8"))
    parts.append(
        ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
         % (file_field, name.replace('"', "_"))).encode("utf-8")
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    with open(file_path, "rb") as fh:
        parts.append(fh.read())
    parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return "multipart/form-data; boundary=%s" % boundary, b"".join(parts)


def transcribe_remote(base_url, api_key, source, model="whisper-1", language=None,
                      response_format="srt", timeout=DEFAULT_REMOTE_TIMEOUT):
    """POST 到 OpenAI 兼容的 ``/v1/audio/transcriptions``。

    返回 ``{"content_type", "body", "status"}``；HTTP 错误会转成可读的
    :class:`EngineError`（**错误信息里绝不带上 api_key**）。
    """
    url = validate_base_url(base_url)
    if not url.endswith("/v1/audio/transcriptions"):
        url = url + "/v1/audio/transcriptions"
    if response_format not in REMOTE_FORMATS:
        raise EngineError("不支持的响应格式：%s" % response_format)

    fields = {"model": model, "response_format": response_format}
    if language:
        fields["language"] = language
    content_type, body = build_multipart(fields, "file", source)

    headers = {"Content-Type": content_type, "Accept": "*/*"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "body": response.read().decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", "replace")[:300]
        raise EngineError("转写接口返回 HTTP %s：%s" % (exc.code, detail or "无响应体"))
    except urllib.error.URLError as exc:
        raise EngineError("无法连接转写接口：%s" % exc.reason)
    except (TimeoutError, OSError) as exc:
        raise EngineError("转写接口请求失败：%s" % exc)


def parse_remote_payload(payload):
    """把接口返回的内容统一成 cue 列表。

    三种形态都支持：

    * ``verbose_json``：``{"segments": [{start, end, text}, ...]}`` → 带时间轴
    * ``json`` / ``text``：``{"text": "..."}`` 或裸文本 → 无时间轴，由调用方排时间
    * ``srt`` / ``vtt``：直接交给 subtitles 解析
    """
    body = (payload.get("body") or "").strip()
    content_type = (payload.get("content_type") or "").lower()
    if not body:
        raise EngineError("转写接口返回了空响应")

    if "json" in content_type or body[:1] in ("{", "["):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            segments = data.get("segments")
            if isinstance(segments, list) and segments:
                cues = []
                for index, segment in enumerate(segments, 1):
                    if not isinstance(segment, dict):
                        continue
                    text = str(segment.get("text", "")).strip()
                    if not text:
                        continue
                    cues.append({"index": index, "start": float(segment.get("start") or 0.0),
                                 "end": float(segment.get("end") or 0.0), "text": text})
                if cues:
                    return cues, "segments"
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                return _text_to_cues(text), "text"
        raise EngineError("转写接口返回的 JSON 里既没有 segments 也没有 text")

    for fmt in ("srt", "vtt", "lrc"):
        try:
            parsed = subtitles.parse(body, fmt)
            return parsed["cues"], fmt
        except subtitles.SubtitleError:
            continue
    return _text_to_cues(body), "text"


def _text_to_cues(text):
    return [{"index": index, "start": None, "end": None, "text": line.strip()}
            for index, line in enumerate(text.splitlines(), 1) if line.strip()]


def engine_status(settings, secrets):
    """``GET /api/audio/engines`` 的主体。

    **不含任何凭据**：只回 key 的存在性，不回内容。
    """
    local = local_status()
    remote = public_remote(settings, secrets)
    if local["available"]:
        summary = "本机已检测到 %s，可直接转写" % (local["version"] or local["name"])
    elif remote["ready"]:
        summary = "本机没有转写命令，但已配置远程接口"
    else:
        summary = "本地转写不可用"
    return {
        "ok": True,
        "local": local,
        "remote": remote,
        "transcribe_available": bool(local["available"] or remote["ready"]),
        "summary": summary,
        "hint": None if (local["available"] or remote["ready"]) else (
            "本机没有 faster-whisper 命令，也没有配置远程转写接口，"
            "因此语音转写功能不可用。这不影响其它功能："
            "音频元数据、波形与静音分析、字幕格式互转都完全离线可用。"
            "如需转写，可以（1）在系统上安装 faster-whisper，或"
            "（2）在「设置」里填写你自己可信任的 OpenAI 兼容接口地址并显式启用。"
        ),
    }
