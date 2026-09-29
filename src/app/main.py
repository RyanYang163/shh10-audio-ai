"""音频 AI 分析器 —— 应用装配、路由与任务处理器。

职责划分：

* ``audiometa.py`` —— WAV / FLAC / MP3 / OGG / OPUS / M4A 的元数据（纯文件头解析）
* ``pcm.py``       —— WAV → PCM 解码 + RMS / 过零率 / 静音检测 / 波形峰值
* ``subtitles.py`` —— LRC ⇄ SRT ⇄ VTT ⇄ TXT 互转、歌词模式、时间轴平移、批量转换
* ``engines.py``   —— 可选转写引擎（本机 faster-whisper / 自配远程接口）
* ``main.py``（本文件） —— 路由、任务处理器、扫描缓存

三条一贯的做法：

1. **默认只读。** 只有「转换字幕」会写文件，且只写用户显式指定的输出目录；
   不指定时写应用自己的 ``data/output/``，绝不动用户的原文件。
2. **长任务进队列。** 扫描目录、批量转换、整文件分析、转写都注册成任务，
   带进度、可取消；只有「读一个文件头」这类毫秒级操作才同步返回。
3. **可选能力真的可选。** 检测不到转写引擎时应用照常启动，界面上标「不可用」，
   绝不让它成为启动前提。
"""

import csv
import hashlib
import io
import json
import os
import time

from tnasapp import fsapi, server as srv

from . import audiometa, engines, pcm, subtitles

APP_ID = "shh10-audio-ai"
APP_VERSION = "1.0.012"
TITLE = "AI Audio Analyzer"

#: 扫描时递归的最大深度，防止误选根目录后无限下钻
MAX_DEPTH = 24
#: 单次扫描最多收录的文件数（保护内存与任务时长）
MAX_FILES = 200000
#: 批量转换时默认当作字幕的扩展名。``.txt`` 不在此列 ——
#: 一个目录里通常还有 README.txt / notes.txt，不能默认一起转了，
#: 需要时用 ``include_txt: true`` 显式打开。
BATCH_SUBTITLE_EXTS = {".lrc", ".srt", ".vtt"}

MIGRATIONS = [
    # 版本 5 起（前 4 个版本是任务队列的 SCHEMA，由框架提供）
    """
    CREATE TABLE IF NOT EXISTS audio_files (
        path        TEXT PRIMARY KEY,
        name        TEXT,
        size        INTEGER NOT NULL DEFAULT 0,
        mtime       REAL    NOT NULL DEFAULT 0,
        ext         TEXT,
        format      TEXT,
        codec       TEXT,
        duration    REAL    NOT NULL DEFAULT 0,
        sample_rate INTEGER,
        channels    INTEGER,
        bit_depth   INTEGER,
        bitrate     INTEGER NOT NULL DEFAULT 0,
        lossless    INTEGER NOT NULL DEFAULT 0,
        decodable   INTEGER NOT NULL DEFAULT 0,
        tags        TEXT,
        error       TEXT,
        probed_at   REAL    NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audio_ext ON audio_files(ext)",
    "CREATE INDEX IF NOT EXISTS idx_audio_error ON audio_files(error)",
    """
    CREATE TABLE IF NOT EXISTS subtitle_files (
        path       TEXT PRIMARY KEY,
        name       TEXT,
        size       INTEGER NOT NULL DEFAULT 0,
        mtime      REAL    NOT NULL DEFAULT 0,
        ext        TEXT,
        format     TEXT,
        cue_count  INTEGER NOT NULL DEFAULT 0,
        duration   REAL    NOT NULL DEFAULT 0,
        error      TEXT,
        probed_at  REAL    NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS convert_results (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id        INTEGER,
        source        TEXT NOT NULL,
        output        TEXT,
        source_format TEXT,
        target_format TEXT,
        cue_count     INTEGER NOT NULL DEFAULT 0,
        merged        INTEGER NOT NULL DEFAULT 0,
        offset        REAL    NOT NULL DEFAULT 0,
        ok            INTEGER NOT NULL DEFAULT 1,
        error         TEXT,
        created_at    REAL    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_convert_job ON convert_results(job_id)",
]


class AudioAiApp(srv.App):
    """在框架的默认配置上加本应用的几项。

    覆写 ``default_settings`` 而不是直接塞字典：``Settings`` 只在文件里没有该项时
    才用默认值，所以这样升级不会把用户改过的值冲掉。
    """

    def default_settings(self):
        base = super().default_settings()
        base.update({
            # 同步分析的最长音频时长（秒）。超过它就走任务队列 ——
            # 分析是逐样本的 Python 循环，长文件会占住一个请求很久。
            "analyze_sync_seconds": 120,
            "default_frame_ms": 20,
            "default_threshold_db": -45,
            "default_min_silence_ms": 300,
            # 远程转写接口默认**关闭**，必须管理员显式确认启用
            "remote_enabled": False,
            "remote_base_url": "",
            "remote_model": "whisper-1",
            "remote_language": "",
        })
        return base


def create_app(paths=None, log_level="INFO"):
    app = AudioAiApp(
        APP_ID, TITLE, version=APP_VERSION, workers=2, log_level=log_level,
        extra_migrations=MIGRATIONS, paths=paths,
        description="读取音频信息、检测静音段落，并在转写文本与字幕格式之间互转。",
    )
    # 引擎状态要在 App 构造之后再算：远程接口的可用性依赖已加载的运行时配置。
    app.engines = {
        "faster-whisper": engines.local_status(),
        "remote-api": _remote_engine_summary(app),
    }
    fsapi.register(app)
    _register_routes(app)
    _register_jobs(app)
    return app


def _remote_engine_summary(app):
    """给 ``/api/app`` 用的远程引擎摘要（只有可用性，没有地址也没有凭据）。"""
    config = engines.public_remote(app.settings.all(), _secrets(app))
    return {
        "name": "remote-api",
        "available": config["ready"],
        "detail": ("已配置远程转写接口：%s" % config["base_url"]) if config["ready"]
                  else "未启用远程转写接口（默认关闭）",
        "enables": ["transcribe"],
    }


# ---------------------------------------------------------------- 工具


def _secrets_path(app):
    return os.path.join(app.paths.config_dir, "secrets.json")


def _secrets(app):
    return engines.read_secrets(_secrets_path(app))


def _require_allowed(app, path, must_exist=True):
    """所有涉及用户文件的路径都必须先过白名单（realpath + 根比对）。"""
    return app.allowed.check(path, must_exist=must_exist)


def _resolve_output_dir(app, raw):
    """输出目录：给了就用给的（必须在白名单内），没给就用应用自己的 data/output。

    这是本应用**唯一**会写的地方，所以两道都收紧：白名单校验 + 可写探测。
    """
    if raw:
        target = app.allowed.check(raw, must_exist=False)
    else:
        target = app.paths.output_dir
    os.makedirs(target, exist_ok=True)
    if not os.access(target, os.W_OK):
        raise PermissionError("输出目录不可写：%s" % target)
    return target


def _walk_files(app, root, extensions, ctx=None, on_progress=None):
    """递归收集指定扩展名的文件。``os.scandir`` + 跳过明显的系统目录。"""
    real = _require_allowed(app, root)
    if os.path.isfile(real):
        return [real] if os.path.splitext(real)[1].lower() in extensions else []

    found = []
    stack = [(real, 0)]
    scanned = 0
    while stack:
        directory, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if ctx is not None:
            ctx.checkpoint()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.startswith((".", "@", "$")):
                                continue
                            stack.append((entry.path, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            if os.path.splitext(entry.name)[1].lower() in extensions:
                                found.append(entry.path)
                                if len(found) >= MAX_FILES:
                                    return sorted(found)
                    except OSError:
                        continue
                    scanned += 1
                    if scanned % 500 == 0 and on_progress:
                        on_progress(scanned, len(found))
        except OSError:
            continue
    return sorted(found)


def _cached_probe(app, path):
    """按 ``mtime + size`` 判断是否需要重新探测（未变化的文件不重算）。"""
    try:
        stat = os.stat(path)
    except OSError as exc:
        return {"path": path, "error": "无法读取：%s" % exc}
    cached = app.store.query_one("SELECT * FROM audio_files WHERE path=?", (path,))
    if cached and abs(cached["size"] - stat.st_size) < 1 and abs(cached["mtime"] - stat.st_mtime) < 1:
        record = dict(cached)
        record["cached"] = True
        record["tags"] = _loads(record.get("tags"), {})
        return record

    record = _probe_uncached(path)
    if record.get("unsupported"):
        # 不是音频文件：不入库（用户随手点一个 .txt 不该污染音频库）
        return record
    app.store.execute(
        "INSERT INTO audio_files (path,name,size,mtime,ext,format,codec,duration,"
        "sample_rate,channels,bit_depth,bitrate,lossless,decodable,tags,error,probed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET name=excluded.name, size=excluded.size,"
        " mtime=excluded.mtime, ext=excluded.ext, format=excluded.format,"
        " codec=excluded.codec, duration=excluded.duration,"
        " sample_rate=excluded.sample_rate, channels=excluded.channels,"
        " bit_depth=excluded.bit_depth, bitrate=excluded.bitrate,"
        " lossless=excluded.lossless, decodable=excluded.decodable, tags=excluded.tags,"
        " error=excluded.error, probed_at=excluded.probed_at",
        (
            path, os.path.basename(path), stat.st_size, stat.st_mtime,
            os.path.splitext(path)[1].lower(),
            record.get("format"), record.get("codec"),
            float(record.get("duration") or 0),
            record.get("sample_rate"), record.get("channels"), record.get("bit_depth"),
            int(record.get("bitrate") or 0), 1 if record.get("lossless") else 0,
            1 if record.get("decodable") else 0,
            json.dumps(record.get("tags") or {}, ensure_ascii=False),
            record.get("error"), time.time(),
        ),
    )
    return record


def _probe_uncached(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        info = audiometa.read_metadata(path)
    except audiometa.UnsupportedFormat:
        return {"path": path, "unsupported": True,
                "error": "不支持的文件类型：%s" % (ext or "无扩展名")}
    except audiometa.AudioMetaError as exc:
        return {"path": path, "error": "解析失败：%s" % exc, "ext": ext}
    except OSError as exc:
        return {"path": path, "error": "读取失败：%s" % exc, "ext": ext}

    info["decodable"] = ext in audiometa.DECODABLE_EXTS
    info["cached"] = False
    return info


def _audit_subtitle(app, path):
    """收录一个字幕文件（只统计 cue 数与总时长，不存正文）。"""
    try:
        stat = os.stat(path)
    except OSError as exc:
        return {"path": path, "error": "无法读取：%s" % exc}
    cached = app.store.query_one("SELECT * FROM subtitle_files WHERE path=?", (path,))
    if cached and abs(cached["size"] - stat.st_size) < 1 and abs(cached["mtime"] - stat.st_mtime) < 1:
        record = dict(cached)
        record["cached"] = True
        return record
    try:
        parsed = subtitles.parse_file(path)
        timed = [cue for cue in parsed["cues"] if cue.get("end") is not None]
        duration = max((cue["end"] for cue in timed), default=0.0)
        record = {"path": path, "name": os.path.basename(path), "size": stat.st_size,
                  "mtime": stat.st_mtime, "ext": os.path.splitext(path)[1].lower(),
                  "format": parsed["format"], "cue_count": len(parsed["cues"]),
                  "duration": round(duration, 3), "error": None}
    except (subtitles.SubtitleError, OSError) as exc:
        record = {"path": path, "name": os.path.basename(path), "size": stat.st_size,
                  "mtime": stat.st_mtime, "ext": os.path.splitext(path)[1].lower(),
                  "format": None, "cue_count": 0, "duration": 0.0, "error": str(exc)}
    app.store.execute(
        "INSERT INTO subtitle_files (path,name,size,mtime,ext,format,cue_count,duration,"
        "error,probed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET name=excluded.name, size=excluded.size,"
        " mtime=excluded.mtime, ext=excluded.ext, format=excluded.format,"
        " cue_count=excluded.cue_count, duration=excluded.duration, error=excluded.error,"
        " probed_at=excluded.probed_at",
        (path, record["name"], record["size"], record["mtime"], record["ext"],
         record["format"], record["cue_count"], record["duration"], record["error"],
         time.time()),
    )
    return record


def _loads(text, default):
    try:
        value = json.loads(text) if text else default
        return value if value is not None else default
    except (TypeError, ValueError):
        return default


def _analysis_params(app, req):
    """从请求里取分析参数，缺项落到应用默认值（设置页可改）。"""
    return {
        "frame_ms": _bounded(req.float_arg("frame_ms", settings_default(app, "default_frame_ms", 20)),
                             2.0, 200.0),
        "threshold_db": _bounded(
            req.float_arg("threshold_db", settings_default(app, "default_threshold_db", -45)),
            -120.0, 0.0),
        "min_silence_ms": _bounded(
            req.float_arg("min_silence_ms", settings_default(app, "default_min_silence_ms", 300)),
            0.0, 600000.0),
        "buckets": int(_bounded(req.int_arg("buckets", 600), 10, 4000)),
        "series_points": int(_bounded(req.int_arg("series_points", 1200), 10, 4000)),
    }


def _bounded(value, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def settings_default(app, key, fallback):
    """取应用设置里的默认值（设置页可改），缺失或非法就用兜底值。"""
    try:
        return float(app.settings.get(key, fallback))
    except (TypeError, ValueError):
        return float(fallback)


def _analysis_cache_path(app, path, stat, params):
    key = "|".join([path, str(int(stat.st_size)), "%.3f" % stat.st_mtime]
                   + ["%s=%s" % (name, params[name]) for name in sorted(params)])
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(app.paths.cache_dir, "analysis", digest + ".json")


def _load_cached_analysis(app, path, params):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    cache_path = _analysis_cache_path(app, path, stat, params)
    if not os.path.isfile(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    payload["cached"] = True
    return payload


def _store_analysis(app, path, payload):
    try:
        stat = os.stat(path)
        cache_path = _analysis_cache_path(app, path, stat, {
            "frame_ms": payload.get("requested_frame_ms"), "threshold_db": payload["threshold_db"],
            "min_silence_ms": payload["min_silence_ms"], "buckets": payload["peak_buckets"],
            "series_points": payload["series_points"],
        })
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return cache_path
    except OSError:
        return None


def _run_analysis(app, path, params, progress=None, checkpoint=None):
    payload = pcm.analyze(
        path,
        frame_ms=params["frame_ms"], threshold_db=params["threshold_db"],
        min_silence_ms=params["min_silence_ms"], buckets=params["buckets"],
        series_points=params["series_points"], progress=progress, checkpoint=checkpoint,
    )
    payload["cached"] = False
    payload["cache_path"] = _store_analysis(app, path, payload)
    return payload


# ---------------------------------------------------------------- 路由


def _register_routes(app):

    @app.get("/api/audio/detect")
    def _detect(req):
        """快速判断一个路径是文件还是目录、能做什么 —— 前端选完文件先问这个。"""
        path = req.arg("path", "")
        try:
            real = _require_allowed(app, path)
        except Exception as exc:
            return srv.Response.error(str(exc), 403,
                                      "请先在「设置」里把该目录加入可访问目录")
        ext = os.path.splitext(real)[1].lower()
        return srv.Response.json({
            "ok": True, "path": real, "is_dir": os.path.isdir(real), "extension": ext,
            "is_audio": ext in audiometa.AUDIO_EXTS,
            "analyzable": ext in audiometa.DECODABLE_EXTS,
            "is_subtitle": ext in audiometa.SUBTITLE_EXTS,
        })

    @app.get("/api/audio/probe")
    def _probe(req):
        """元数据：时长 / 采样率 / 声道 / 位深 / 比特率 / 标签。"""
        path = req.arg("path", "")
        try:
            real = _require_allowed(app, path)
        except Exception as exc:
            return srv.Response.error(str(exc), 403,
                                      "请先在「设置」里把该目录加入可访问目录")
        if not os.path.isfile(real):
            return srv.Response.error("不是文件：%s" % path, 400)
        result = _cached_probe(app, real)
        result["ok"] = result.get("error") is None
        return srv.Response.json(result)

    @app.get("/api/audio/analyze")
    def _analyze(req):
        """波形峰值 + 静音区间 + 逐帧 RMS / 过零率（仅 WAV）。

        长文件不在同步请求里做：分析是逐样本的 Python 循环，几分钟的音频
        会占住一个请求很久。超过 ``analyze_sync_seconds`` 时返回 409 并指向任务队列
        —— 用 409 而不是 400，是因为请求本身没错，只是「该换个通道做」。
        """
        path = req.arg("path", "")
        try:
            real = _require_allowed(app, path)
        except Exception as exc:
            return srv.Response.error(str(exc), 403,
                                      "请先在「设置」里把该目录加入可访问目录")
        if not os.path.isfile(real):
            return srv.Response.error("不是文件：%s" % path, 400)
        params = _analysis_params(app, req)

        # force=1 跳过缓存重算 —— 前端的「重新分析」按钮用它，
        # 否则同一个文件同一个参数永远拿到缓存，按钮看起来没反应
        if not req.bool_arg("force"):
            cached = _load_cached_analysis(app, real, params)
            if cached is not None:
                return srv.Response.json(cached)

        if not pcm.supported(real):
            return srv.Response.error(
                "波形分析目前只支持 WAV", 400,
                "其它格式（FLAC / MP3 / OGG / M4A）需要先解码成 PCM WAV 才能做"
                "逐样本分析。元数据读取与字幕转换不受影响。")
        try:
            info = pcm.read_wav_info(real)
        except pcm.PcmError as exc:
            return srv.Response.error(str(exc), 400)

        duration = float(info.get("duration") or 0)
        limit = float(app.settings.get("analyze_sync_seconds", 120) or 120)
        if duration > limit:
            return srv.Response.error(
                "这段音频长 %.0f 秒，超过同步分析的 %d 秒上限" % (duration, int(limit)), 409,
                "请改用任务方式（不会阻塞页面，且可看进度、可取消）："
                "POST /api/jobs {\"type\": \"analyze\", \"params\": {\"path\": \"%s\"}}"
                % real)
        try:
            return srv.Response.json(_run_analysis(app, real, params))
        except pcm.PcmError as exc:
            return srv.Response.error(str(exc), 400)
        except OSError as exc:
            return srv.Response.error("读取失败：%s" % exc, 400)

    @app.get("/api/audio/subtitles")
    def _subtitles(req):
        """解析一个字幕文件为 cue 列表（自动识别格式，可强制指定）。"""
        path = req.arg("path", "")
        try:
            real = _require_allowed(app, path)
        except Exception as exc:
            return srv.Response.error(str(exc), 403,
                                      "请先在「设置」里把该目录加入可访问目录")
        if not os.path.isfile(real):
            return srv.Response.error("不是文件：%s" % path, 400)
        limit = int(_bounded(req.int_arg("limit", 500), 1, 20000))
        fmt = (req.arg("format") or "").strip().lower() or None
        try:
            parsed = subtitles.parse_file(real, fmt)
        except subtitles.SubtitleError as exc:
            return srv.Response.error(str(exc), 400,
                                      "可以先确认文件内容，或用 format 参数强制指定格式"
                                      "（lrc / srt / vtt / txt）")
        except OSError as exc:
            return srv.Response.error("读取失败：%s" % exc, 400)
        cues = parsed["cues"]
        timed = [cue for cue in cues if cue.get("end") is not None]
        return srv.Response.json({
            "ok": True, "path": real, "format": parsed["format"],
            "encoding": parsed.get("encoding"), "meta": parsed.get("meta") or {},
            "cue_count": len(cues),
            "duration": round(max((cue["end"] for cue in timed), default=0.0), 3),
            "cues": subtitles.summarize(cues, limit),
            "truncated": len(cues) > limit,
        })

    @app.get("/api/audio/files")
    def _files(req):
        """已收录的文件（``kind=audio`` 默认 / ``subtitle`` / ``all``）。"""
        limit = int(_bounded(req.int_arg("limit", 100), 1, 500))
        offset = max(0, req.int_arg("offset", 0))
        keyword = (req.arg("q") or "").strip()
        kind = (req.arg("kind") or "audio").strip().lower()
        payload = {"ok": True, "kind": kind}
        if kind in ("audio", "all"):
            payload["audio"] = _query_library(
                app, "audio_files", keyword, limit, offset,
                order="ORDER BY path", extra_columns="*")
        if kind in ("subtitle", "all"):
            payload["subtitle"] = _query_library(
                app, "subtitle_files", keyword, limit, offset,
                order="ORDER BY path", extra_columns="*")
        if kind == "audio":
            payload["total"] = payload["audio"]["total"]
            payload["files"] = payload["audio"]["rows"]
        elif kind == "subtitle":
            payload["total"] = payload["subtitle"]["total"]
            payload["files"] = payload["subtitle"]["rows"]
        else:
            payload["total"] = payload["audio"]["total"] + payload["subtitle"]["total"]
        return srv.Response.json(payload)

    @app.get("/api/audio/summary")
    def _summary(req):
        audio_total = app.store.scalar("SELECT COUNT(*) FROM audio_files", default=0)
        audio_failed = app.store.scalar(
            "SELECT COUNT(*) FROM audio_files WHERE error IS NOT NULL", default=0)
        duration = app.store.scalar(
            "SELECT COALESCE(SUM(duration),0) FROM audio_files", default=0)
        size = app.store.scalar(
            "SELECT COALESCE(SUM(size),0) FROM audio_files", default=0)
        analyzable = app.store.scalar(
            "SELECT COUNT(*) FROM audio_files WHERE decodable=1", default=0)
        subtitle_total = app.store.scalar("SELECT COUNT(*) FROM subtitle_files", default=0)
        subtitle_failed = app.store.scalar(
            "SELECT COUNT(*) FROM subtitle_files WHERE error IS NOT NULL", default=0)
        converted = app.store.scalar(
            "SELECT COUNT(*) FROM convert_results WHERE ok=1", default=0)
        formats = app.store.query(
            "SELECT COALESCE(format,'未知') AS format, COUNT(*) AS n,"
            " COALESCE(SUM(duration),0) AS seconds FROM audio_files"
            " GROUP BY format ORDER BY n DESC")
        return srv.Response.json({
            "ok": True, "audio_total": audio_total, "audio_failed": audio_failed,
            "duration": round(float(duration or 0), 3), "total_bytes": size,
            "analyzable": analyzable, "subtitle_total": subtitle_total,
            "subtitle_failed": subtitle_failed, "converted": converted,
            "formats": formats,
        })

    @app.get("/api/audio/results")
    def _results(req):
        job_id = req.arg("job_id")
        if job_id:
            rows = app.store.query(
                "SELECT * FROM convert_results WHERE job_id=? ORDER BY id", (int(job_id),))
        else:
            rows = app.store.query(
                "SELECT * FROM convert_results ORDER BY id DESC LIMIT ?",
                (int(_bounded(req.int_arg("limit", 100), 1, 500)),))
        return srv.Response.json({"ok": True, "results": rows})

    @app.get("/api/audio/export")
    def _export(req):
        """导出已收录清单：CSV 或 JSON。"""
        kind = (req.arg("kind") or "audio").strip().lower()
        fmt = (req.arg("format") or "json").strip().lower()
        if kind == "subtitle":
            rows = app.store.query("SELECT * FROM subtitle_files ORDER BY path")
            columns = ["path", "name", "size", "format", "cue_count", "duration", "error"]
        else:
            rows = app.store.query("SELECT * FROM audio_files ORDER BY path")
            columns = ["path", "name", "size", "format", "codec", "duration",
                       "sample_rate", "channels", "bit_depth", "bitrate", "lossless",
                       "error"]
        table = [{column: row.get(column) for column in columns} for row in rows]
        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in table:
                writer.writerow(row)
            return srv.Response(200, buffer.getvalue().encode("utf-8-sig"),
                                "text/csv; charset=utf-8")
        if fmt != "json":
            return srv.Response.error("不支持的导出格式：%s（只支持 csv / json）" % fmt, 400)
        return srv.Response.json({"ok": True, "kind": kind, "count": len(table), "rows": table})

    @app.get("/api/audio/engines")
    def _engines(req):
        """转写引擎状态。**响应里不含任何凭据。**"""
        return srv.Response.json(engines.engine_status(app.settings.all(), _secrets(app)))

    @app.post("/api/audio/engines")
    def _engines_set(req):
        """配置远程转写接口。

        **启用必须显式确认**（``confirm: true``）：一旦启用，用户选的音频会被
        发送到他自己填的那个地址去。这是不可逆的信息流向，必须让他明确说一次「知道了」。
        关闭不需要确认。
        """
        body = req.json_body()
        if not isinstance(body, dict):
            return srv.Response.error("请求体必须是 JSON 对象", 400)

        remote = body.get("remote") if isinstance(body.get("remote"), dict) else {}
        changes = {}
        if "enabled" in remote:
            changes["remote_enabled"] = bool(remote["enabled"])
        if "base_url" in remote:
            try:
                changes["remote_base_url"] = engines.validate_base_url(remote["base_url"])
            except engines.EngineError as exc:
                return srv.Response.error(str(exc), 400)
        if "model" in remote:
            model = str(remote["model"] or "").strip()
            if model and not engines.safe_token(model):
                return srv.Response.error("模型名含非法字符", 400)
            changes["remote_model"] = model or "whisper-1"
        if "language" in remote:
            language = str(remote["language"] or "").strip()
            if language and not engines.safe_token(language):
                return srv.Response.error("语言码含非法字符（如 zh / en / ja）", 400)
            changes["remote_language"] = language

        secrets = _secrets(app)
        key_changed = False
        if "api_key" in body:
            key = str(body.get("api_key") or "")
            secrets["remote_api_key"] = key
            key_changed = True
        if body.get("clear_key"):
            secrets.pop("remote_api_key", None)
            key_changed = True
        if key_changed:
            engines.write_secrets(_secrets_path(app), secrets)

        enabling = changes.get("remote_enabled") is True
        if enabling and body.get("confirm") is not True:
            return srv.Response.error(
                "启用远程转写需要显式确认", 400,
                "启用后，你选择的音频会被上传到你填写的接口地址做转写。"
                "确认无误请重发一次并带上 \"confirm\": true。")
        if enabling:
            merged = dict(app.settings.all())
            merged.update(changes)
            config = engines.public_remote(merged, secrets)
            if not config["base_url"] or not config["has_key"]:
                return srv.Response.error(
                    "启用前需要先填好接口地址与 API Key", 400,
                    "缺少：%s" % "、".join(
                        [name for name, ok in (("base_url", config["base_url"]),
                                               ("api_key", config["has_key"])) if not ok]))

        if changes:
            app.settings.update(changes)
        app.engines = {
            "faster-whisper": engines.local_status(),
            "remote-api": _remote_engine_summary(app),
        }
        app.log.info("远程转写配置已更新：%s", ", ".join(sorted(changes)) or "无字段变化")
        return srv.Response.json(engines.engine_status(app.settings.all(), _secrets(app)))

    @app.post("/api/audio/convert")
    def _convert(req):
        """字幕转换。**一律进队列** —— 批量转换一个目录可能有上百个文件。"""
        body = req.json_body()
        if not isinstance(body, dict):
            return srv.Response.error("请求体必须是 JSON 对象", 400)
        inputs = body.get("inputs") or []
        roots = body.get("roots") or []
        if not inputs and not roots:
            return srv.Response.error("没有指定要转换的文件", 400,
                                      "请给 inputs（文件列表）或 roots（目录列表）")
        target = str(body.get("format") or "").strip().lower()
        if target not in subtitles.FORMATS:
            return srv.Response.error(
                "不支持的目标格式：%s" % (target or "未指定"), 400,
                "可用格式：%s" % " / ".join(subtitles.FORMATS))
        try:
            job = app.jobs.submit("convert", {
                "inputs": inputs, "roots": roots,
                "output_dir": body.get("output_dir") or "",
                "format": target,
                "mode": str(body.get("mode") or "subtitle").lower(),
                "offset": float(body.get("offset") or 0),
                "merge_short": body.get("merge_short"),
                "merge_max_gap": body.get("merge_max_gap"),
                "line_duration": body.get("line_duration"),
                "source_format": body.get("source_format") or None,
                "include_txt": bool(body.get("include_txt")),
            }, title="转换字幕为 %s" % target.upper())
        except ValueError as exc:
            return srv.Response.error(str(exc), 400)
        except RuntimeError as exc:
            return srv.Response.error(str(exc), 503)
        return srv.Response.json({"ok": True, "job": job}, status=201)

    @app.post("/api/audio/transcribe")
    def _transcribe(req):
        """语音转写。引擎不可用时明确 503 + 指引，而不是静默失败。"""
        body = req.json_body()
        if not isinstance(body, dict):
            return srv.Response.error("请求体必须是 JSON 对象", 400)
        inputs = body.get("inputs") or []
        roots = body.get("roots") or []
        if not inputs and not roots:
            return srv.Response.error("没有指定要转写的音频文件", 400)
        # 先校验请求本身，再看引擎可用性 —— 否则一个明显写错的请求会被 503 掩盖
        status = engines.engine_status(app.settings.all(), _secrets(app))
        if not status["transcribe_available"]:
            return srv.Response.error("本地转写不可用", 503, status["hint"])
        try:
            job = app.jobs.submit("transcribe", {
                "inputs": inputs, "roots": roots,
                "output_dir": body.get("output_dir") or "",
                "format": str(body.get("format") or "srt").lower(),
                "engine": str(body.get("engine") or "auto").lower(),
                "model": str(body.get("model") or "").strip(),
                "language": str(body.get("language") or "").strip(),
            }, title="语音转写")
        except ValueError as exc:
            return srv.Response.error(str(exc), 400)
        except RuntimeError as exc:
            return srv.Response.error(str(exc), 503)
        return srv.Response.json({"ok": True, "job": job}, status=201)


def _query_library(app, table, keyword, limit, offset, order, extra_columns):
    clauses, params = [], []
    if keyword:
        clauses.append("path LIKE ?")
        params.append("%" + keyword + "%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = app.store.query(
        "SELECT %s FROM %s%s %s LIMIT ? OFFSET ?" % (extra_columns, table, where, order),
        tuple(params + [limit, offset]))
    total = app.store.scalar("SELECT COUNT(*) FROM %s%s" % (table, where),
                             tuple(params), default=0)
    for row in rows:
        if "tags" in row:
            row["tags"] = _loads(row["tags"], {})
    return {"total": total, "rows": rows}


# ---------------------------------------------------------------- 任务


def _register_jobs(app):

    @app.jobs.register("scan")
    def _job_scan(ctx):
        """扫描目录，收录音频与字幕文件（按 mtime + size 增量）。"""
        roots = ctx.params.get("roots") or []
        if not roots:
            raise ValueError("没有指定要扫描的目录")

        audio, subtitle = [], []
        for index, root in enumerate(roots, 1):
            ctx.message("正在收集：%s" % root)
            audio.extend(_walk_files(app, root, audiometa.AUDIO_EXTS, ctx))
            subtitle.extend(_walk_files(app, root, audiometa.SUBTITLE_EXTS, ctx))
            ctx.progress(index, len(roots), "已找到 %d 个音频 / %d 个字幕文件"
                         % (len(audio), len(subtitle)))

        audio = sorted(set(audio))
        subtitle = sorted(set(subtitle))
        ctx.log("共找到 %d 个音频文件、%d 个字幕文件" % (len(audio), len(subtitle)))

        ok = failed = cached = 0
        total = len(audio) + len(subtitle)
        done = 0
        for path in audio:
            ctx.checkpoint()
            done += 1
            record = _cached_probe(app, path)
            if record.get("cached"):
                cached += 1
            elif record.get("error"):
                failed += 1
                ctx.log("解析失败：%s —— %s" % (path, record["error"]), "WARN")
            else:
                ok += 1
            if done % 10 == 0 or done == total:
                ctx.progress(done, total, "已解析 %d/%d" % (done, total))

        subtitle_ok = 0
        for path in subtitle:
            ctx.checkpoint()
            done += 1
            record = _audit_subtitle(app, path)
            if not record.get("error"):
                subtitle_ok += 1
            if done % 20 == 0 or done == total:
                ctx.progress(done, total, "已解析 %d/%d" % (done, total))

        ctx.set_result({
            "audio_found": len(audio), "audio_ok": ok, "audio_failed": failed,
            "audio_from_cache": cached, "subtitle_found": len(subtitle),
            "subtitle_ok": subtitle_ok,
        })

    @app.jobs.register("convert")
    def _job_convert(ctx):
        """字幕批量转换。"""
        params = ctx.params
        target = str(params.get("format") or "").lower()
        if target not in subtitles.FORMATS:
            raise ValueError("不支持的目标格式：%s" % (target or "未指定"))
        out_dir = _resolve_output_dir(app, params.get("output_dir"))

        include = set(BATCH_SUBTITLE_EXTS)
        if params.get("include_txt"):
            include.add(".txt")
        sources = []
        for root in params.get("roots") or []:
            sources.extend(_walk_files(app, root, include, ctx))
        for path in params.get("inputs") or []:
            real = _require_allowed(app, path)
            if os.path.isfile(real):
                sources.append(real)
        sources = sorted(set(sources))
        if not sources:
            raise ValueError("没有找到可转换的字幕文件（支持 %s）"
                             % " / ".join(sorted(include)))

        mode = str(params.get("mode") or "subtitle").lower()
        merge_short = params.get("merge_short")
        if merge_short in (None, ""):
            # 歌词模式：默认就把 1 秒一行的碎句并成整句
            merge_short = subtitles.LYRIC_MERGE_SECONDS if mode == "lyric" else 0.0
        else:
            merge_short = _bounded(merge_short, 0.0, 60.0)
        merge_gap = _bounded(params.get("merge_max_gap")
                             if params.get("merge_max_gap") not in (None, "")
                             else subtitles.DEFAULT_MERGE_GAP, 0.0, 600.0)
        line_seconds = _bounded(params.get("line_duration")
                                if params.get("line_duration") not in (None, "")
                                else subtitles.DEFAULT_LINE_SECONDS, 0.1, 600.0)
        offset = float(params.get("offset") or 0)

        ctx.log("共 %d 个字幕文件 → %s（模式 %s、平移 %+.2fs、合并阈值 %.2fs）"
                % (len(sources), target.upper(), mode, offset, merge_short))
        succeeded = 0
        first_error = None
        results = []
        for index, source in enumerate(sources, 1):
            ctx.checkpoint()
            ctx.message("转换 %s" % os.path.basename(source))
            try:
                record = subtitles.convert_file(
                    source, out_dir, target,
                    source_format=params.get("source_format"),
                    offset=offset, merge_short=merge_short, merge_max_gap=merge_gap,
                    line_seconds=line_seconds)
            except (subtitles.SubtitleError, OSError, UnicodeDecodeError) as exc:
                record = {"ok": False, "source": source, "error": str(exc)}
                first_error = first_error or "%s：%s" % (os.path.basename(source), exc)
                ctx.log("失败：%s —— %s" % (source, exc), "WARN")
            results.append(record)
            if record["ok"]:
                succeeded += 1
            stats = record.get("stats") or {}
            app.store.execute(
                "INSERT INTO convert_results (job_id,source,output,source_format,"
                "target_format,cue_count,merged,offset,ok,error,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ctx.job_id, source, record.get("output"), record.get("source_format"),
                 target, int(stats.get("cues_out") or 0), int(stats.get("merged") or 0),
                 offset, 1 if record["ok"] else 0, record.get("error"), time.time()),
            )
            ctx.progress(index, len(sources), "%d/%d 成功 %d" % (index, len(sources), succeeded))

        # 一个都没成功：把任务标成「失败」并把第一条原因带出来。
        # 只报「已完成（0 成功 3 失败）」会让人以为任务没问题，必须显式失败。
        if sources and not succeeded:
            raise RuntimeError("全部 %d 个文件都转换失败，第一个原因：%s"
                               % (len(sources), first_error or "未知"))

        ctx.set_result({
            "total": len(sources), "succeeded": succeeded,
            "failed": len(sources) - succeeded, "output_dir": out_dir,
            "format": target, "mode": mode, "offset": offset,
            "merge_short": merge_short,
        })

    @app.jobs.register("analyze")
    def _job_analyze(ctx):
        """整文件波形 + 静音分析（长文件走这里，结果落缓存）。"""
        path = ctx.params.get("path")
        if not path:
            raise ValueError("没有指定要分析的音频文件")
        real = _require_allowed(app, path)
        if not os.path.isfile(real):
            raise ValueError("不是文件：%s" % path)
        if not pcm.supported(real):
            raise ValueError("波形分析目前只支持 WAV（其它格式需要先解码成 PCM）")

        params = {
            # 缺项落到应用设置里的默认值 —— 与同步接口（_analysis_params）保持同一口径，
            # 否则同一个文件走同步和走队列会得到不同的静音区间
            "frame_ms": _bounded(ctx.params.get("frame_ms")
                                 if ctx.params.get("frame_ms") not in (None, "")
                                 else settings_default(app, "default_frame_ms", 20), 2.0, 200.0),
            "threshold_db": _bounded(ctx.params.get("threshold_db")
                                     if ctx.params.get("threshold_db") not in (None, "")
                                     else settings_default(app, "default_threshold_db", -45),
                                     -120.0, 0.0),
            "min_silence_ms": _bounded(ctx.params.get("min_silence_ms")
                                       if ctx.params.get("min_silence_ms") not in (None, "")
                                       else settings_default(app, "default_min_silence_ms", 300),
                                       0.0, 600000.0),
            "buckets": int(_bounded(ctx.params.get("buckets", 600), 10, 4000)),
            "series_points": int(_bounded(ctx.params.get("series_points", 1200), 10, 4000)),
        }
        ctx.log("开始分析 %s（帧长 %.0f ms、阈值 %.1f dBFS）"
                % (os.path.basename(real), params["frame_ms"], params["threshold_db"]))
        try:
            payload = _run_analysis(
                app, real, params,
                progress=lambda done, total: ctx.progress(done, total, "已读取 %.0f%%"
                                                          % (100.0 * done / max(1, total))),
                checkpoint=ctx.checkpoint)
        except pcm.PcmError as exc:
            raise ValueError(str(exc))
        ctx.set_result({
            "path": payload["path"], "duration": payload["duration"],
            "frames": payload["frames"], "frame_ms": payload["frame_ms"],
            "silence_count": payload["silence_count"],
            "silence_total": payload["silence_total"],
            "speech_total": payload["speech_total"],
            "peak_buckets": len(payload["peaks"]),
            "cache_path": payload.get("cache_path"),
        })

    @app.jobs.register("transcribe")
    def _job_transcribe(ctx):
        """可选的语音转写（本机命令或远程接口）。"""
        params = ctx.params
        settings = app.settings.all()
        secrets = _secrets(app)
        status = engines.engine_status(settings, secrets)
        if not status["transcribe_available"]:
            raise RuntimeError(status["hint"] or "本地转写不可用")

        engine = str(params.get("engine") or "auto").lower()
        local_ok = status["local"]["available"]
        remote = status["remote"]
        if engine == "auto":
            engine = "local" if local_ok else "remote"
        if engine == "local" and not local_ok:
            raise RuntimeError("本机没有可用的转写命令（%s）" % status["local"]["detail"])
        if engine == "remote" and not remote["ready"]:
            raise RuntimeError("远程转写接口未启用或配置不完整")

        out_dir = _resolve_output_dir(app, params.get("output_dir"))
        target = str(params.get("format") or "srt").lower()
        if target not in subtitles.FORMATS:
            raise ValueError("不支持的目标格式：%s" % target)

        sources = []
        for root in params.get("roots") or []:
            sources.extend(_walk_files(app, root, audiometa.AUDIO_EXTS, ctx))
        for path in params.get("inputs") or []:
            real = _require_allowed(app, path)
            if os.path.isfile(real):
                sources.append(real)
        sources = sorted(set(sources))
        if not sources:
            raise ValueError("没有找到可转写的音频文件")

        model = str(params.get("model") or "").strip()
        language = str(params.get("language") or "").strip()
        succeeded = 0
        first_error = None
        for index, source in enumerate(sources, 1):
            ctx.checkpoint()
            ctx.message("转写 %s" % os.path.basename(source))
            try:
                cues = _transcribe_one(app, ctx, engine, source, model, language,
                                       settings, secrets)
                text, stats = subtitles.convert(
                    cues, target,
                    offset=0.0, merge_short=0.0,
                    line_seconds=subtitles.DEFAULT_LINE_SECONDS)
                stem = os.path.splitext(os.path.basename(source))[0][:120]
                out_path = subtitles.unique_path(
                    out_dir, stem + subtitles.EXT_BY_FORMAT.get(target, "." + target))
                with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                ctx.log("已输出 %s（%d 条字幕）" % (out_path, stats["cues_out"]))
                succeeded += 1
            except (engines.EngineError, subtitles.SubtitleError, OSError) as exc:
                first_error = first_error or "%s：%s" % (os.path.basename(source), exc)
                ctx.log("失败：%s —— %s" % (source, exc), "WARN")
            ctx.progress(index, len(sources), "%d/%d 成功 %d" % (index, len(sources), succeeded))

        # 全部失败 → 任务标成失败并带上原因（与 convert 的处理一致）
        if sources and not succeeded:
            raise RuntimeError("全部 %d 个文件都转写失败，第一个原因：%s"
                               % (len(sources), first_error or "未知"))

        ctx.set_result({"total": len(sources), "succeeded": succeeded,
                        "failed": len(sources) - succeeded, "engine": engine,
                        "output_dir": out_dir, "format": target})


def _transcribe_one(app, ctx, engine, source, model, language, settings, secrets):
    """单个文件的转写 → 返回 cue 列表。"""
    if engine == "local":
        work_dir = os.path.join(app.paths.tmp_dir, "transcribe-%s" % ctx.job_id)
        os.makedirs(work_dir, exist_ok=True)
        info = engines.detect_local()
        ctx.log("调用本机命令 %s 转写（模型 %s）"
                % (info.get("command"), model or settings.get("remote_model") or "small"))
        result = engines.run_local(
            info["path"], source, work_dir,
            model=model or "small", language=language or None,
            output_format="srt")
        text, _encoding = subtitles.read_text(result["output"])
        parsed = subtitles.parse(text, None)
        return parsed["cues"]

    config = engines.remote_settings(settings)
    ctx.log("调用远程接口 %s 转写（模型 %s）"
            % (config["base_url"], model or config["model"]))
    payload = engines.transcribe_remote(
        config["base_url"], secrets.get("remote_api_key", ""), source,
        model=model or config["model"],
        language=language or config["language"] or None,
        response_format="srt")
    cues, source_format = engines.parse_remote_payload(payload)
    ctx.log("接口返回 %d 条（%s）" % (len(cues), source_format))
    return cues


def main(argv=None):
    from tnasapp import cli

    return cli.main(APP_ID, APP_VERSION, create_app, argv=argv,
                    description="音频 AI 分析器 —— 元数据、静音检测与字幕互转")
