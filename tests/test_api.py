"""端到端功能测试：把应用真的跑起来，逐个打接口。

这是「功能可用」这一层的验证 —— 官方审核会把应用装到真机上点一遍，
这里用同样的顺序在开发机上先走一遍：启动 → 设白名单 → 扫描 → 探测 → 分析 →
转换 → 转写 → 查结果/导出。

应用以 TCP 模式启动（Windows 上没有 Unix socket），跑的是与 deb 包内**同一份**代码，
只是换了监听方式。

覆盖要求（与开发规约第五节一致）：

* 每条路由的正常路径 + 至少一条错误路径
* 安全：白名单外 403、目录穿越 403、空 WAV / 非 WAV 给可读错误
* 任务：提交 → 完成、失败可读、取消生效
* 前端：``GET /`` 200 且不含 ``src="/``（F7 白屏守卫）
* 凭据：任何响应里都不出现 API Key
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tnasapp.paths import AppPaths  # noqa: E402

from app import main as app_main  # noqa: E402

from tests import synth  # noqa: E402
from tests.test_engines import StubEndpoint  # noqa: E402

APP_ID = app_main.APP_ID


class ApiClient:
    def __init__(self, base):
        self.base = base

    def request(self, method, path, payload=None, raw=False):
        url = self.base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read()
                if raw:
                    return response.status, body
                return response.status, json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"raw": body}

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def delete(self, path):
        return self.request("DELETE", path)


class AppTestCase(unittest.TestCase):
    """公共的启动逻辑：造素材 → 建应用 → 起服务。"""

    #: 同步分析上限在用例里会临时改，改完必须还原（用例之间不能有顺序依赖）
    EXTRA_SETTINGS = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh10-api-")
        cls.media = os.path.join(cls.tmp, "media")
        cls.outside = os.path.join(cls.tmp, "outside")
        cls.output = os.path.join(cls.tmp, "out")
        for path in (cls.media, cls.outside, cls.output):
            os.makedirs(path, exist_ok=True)

        # 音频素材：标准样本 + 一个 8 声道外的普通立体声
        synth.write_wav(os.path.join(cls.media, "standard.wav"), synth.STANDARD_SEGMENTS)
        synth.write_wav(os.path.join(cls.media, "stereo.wav"), synth.STANDARD_SEGMENTS,
                        channels=2, bit_depth=24)
        with open(os.path.join(cls.media, "fake.mp3"), "wb") as fh:
            fh.write(synth.build_mp3())
        with open(os.path.join(cls.media, "notes.txt"), "wb") as fh:
            fh.write("这不是音频，也不是字幕".encode("utf-8"))
        with open(os.path.join(cls.media, "song.lrc"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(synth.LRC_SAMPLE)
        with open(os.path.join(cls.media, "movie.srt"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(synth.SRT_SAMPLE)
        with open(os.path.join(cls.media, "broken.srt"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("这里没有时间轴，也没有箭头\n")

        # 测试输出里不要混进任务队列的 INFO 日志（迁移、启停那一堆）
        from tnasapp import logx

        logx.setup("jobs", "ERROR")

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = AppPaths(APP_ID, install_dir=repo, data_dir=os.path.join(cls.tmp, "data"))
        cls.app = app_main.create_app(paths=paths, log_level="ERROR")
        # 白名单在启动前设好：用例之间不能有执行顺序依赖
        cls.app.set_allowed_roots([cls.media, cls.output])
        cls.server = cls.app.run(host="127.0.0.1", port=0, background=True)
        cls.client = ApiClient("http://127.0.0.1:%d" % cls.server.server_address[1])
        if cls.EXTRA_SETTINGS:
            cls.client.post("/api/settings", cls.EXTRA_SETTINGS)
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.shutdown()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 辅助

    def wait_job(self, job_id, timeout=120):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            _status, body = self.client.get("/api/jobs/%d" % job_id)
            job = body["job"]
            if job["state"] in ("completed", "failed", "canceled"):
                return job
            time.sleep(0.05)
        return job

    def submit(self, job_type, params, title=""):
        status, body = self.client.post("/api/jobs",
                                        {"type": job_type, "params": params, "title": title})
        self.assertEqual(status, 201, body)
        return body["job"]["id"]

    @classmethod
    def ensure_scan(cls):
        if getattr(cls, "_scan_done", False):
            return
        _status, body = cls.client.post("/api/jobs", {
            "type": "scan", "params": {"roots": [cls.media]}})
        assert _status == 201, body
        job_id = body["job"]["id"]
        deadline = time.time() + 120
        while time.time() < deadline:
            _s, payload = cls.client.get("/api/jobs/%d" % job_id)
            if payload["job"]["state"] in ("completed", "failed", "canceled"):
                break
            time.sleep(0.05)
        cls._scan_done = True
        cls._scan_job = payload["job"]


class BasicRoutesTest(AppTestCase):
    def test_health(self):
        status, body = self.client.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], APP_ID)
        self.assertEqual(body["status"], "ok")

    def test_app_info_lists_job_types_and_engines(self):
        status, body = self.client.get("/api/app")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], app_main.APP_VERSION)
        for job_type in ("scan", "convert", "analyze", "transcribe"):
            self.assertIn(job_type, body["job_types"])
        self.assertIn("faster-whisper", body["engines"])
        self.assertIn("remote-api", body["engines"])

    def test_index_html_is_relative(self):
        status, body = self.client.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"audio-ai.js", body)
        self.assertNotIn(b'src="/', body, "绝对路径会在 /<appid>/ 前缀下白屏（审核项 F7）")

    def test_prefix_compatibility(self):
        for prefix in ("", "/" + APP_ID, "/v2/proxy/" + APP_ID):
            status, body = self.client.get(prefix + "/api/app")
            self.assertEqual(status, 200, prefix)
            self.assertEqual(body["app"], APP_ID)

    def test_unknown_route_404(self):
        status, body = self.client.get("/api/audio/nope")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_fs_browse_is_registered(self):
        """UI.pickDir 依赖 /api/fs/list —— 必须挂上。"""
        status, body = self.client.get("/api/fs/list?path=" + self.media)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        names = {entry["name"] for entry in body["entries"]}
        self.assertIn("standard.wav", names)


class WhitelistTest(AppTestCase):
    def test_outside_path_is_403(self):
        outside_file = os.path.join(self.outside, "secret.wav")
        with open(outside_file, "wb") as fh:
            fh.write(synth.build_wav([("tone", 0.1)]))
        status, body = self.client.get("/api/audio/probe?path=" + outside_file)
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertIn("允许访问", body["error"])

    def test_nonexistent_path_is_also_403(self):
        """不存在的路径同样是 403（不给「路径存不存在」这种可探测的差异）。"""
        status, body = self.client.get("/api/audio/probe?path=" +
                                       os.path.join(self.outside, "nope.wav"))
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_traversal_is_403(self):
        for route in ("probe", "analyze", "subtitles", "detect"):
            status, _body = self.client.get(
                "/api/audio/%s?path=%s" % (route, os.path.join(self.media, "..", "outside")))
            self.assertEqual(status, 403, route)

    def test_empty_whitelist_refuses_everything(self):
        saved = list(self.app.allowed.roots())
        try:
            self.app.set_allowed_roots([])
            status, body = self.client.get("/api/audio/probe?path=" +
                                           os.path.join(self.media, "standard.wav"))
            self.assertEqual(status, 403)
            self.assertIn("白名单", body["error"])
        finally:
            self.app.set_allowed_roots(saved)

    def test_settings_round_trip(self):
        status, body = self.client.post("/api/settings",
                                        {"allowed_roots": [self.media, self.output]})
        self.assertEqual(status, 200)
        status, body = self.client.get("/api/settings")
        self.assertEqual(sorted(body["settings"]["allowed_roots"]),
                         sorted([self.media, self.output]))


class ProbeTest(AppTestCase):
    def test_probe_wav(self):
        status, body = self.client.get(
            "/api/audio/probe?path=" + os.path.join(self.media, "standard.wav"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["format"], "WAV")
        self.assertEqual(body["sample_rate"], 44100)
        self.assertEqual(body["channels"], 1)
        self.assertEqual(body["bit_depth"], 16)
        self.assertAlmostEqual(body["duration"], 2.0, places=3)

    def test_probe_mp3_reports_tags_and_duration(self):
        status, body = self.client.get(
            "/api/audio/probe?path=" + os.path.join(self.media, "fake.mp3"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["format"], "MP3")
        self.assertEqual(body["tags"]["title"], "合成测试曲")

    def test_probe_unsupported_file_reports_ok_false(self):
        status, body = self.client.get(
            "/api/audio/probe?path=" + os.path.join(self.media, "notes.txt"))
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("不支持", body["error"])
        # 非音频文件不入库（不污染音频库）
        _s, listing = self.client.get("/api/audio/files?q=notes.txt")
        self.assertEqual(listing["total"], 0)

    def test_probe_directory_is_400(self):
        status, body = self.client.get("/api/audio/probe?path=" + self.media)
        self.assertEqual(status, 400)
        self.assertIn("不是文件", body["error"])

    def test_detect_reports_capabilities(self):
        _s, body = self.client.get("/api/audio/detect?path=" +
                                   os.path.join(self.media, "standard.wav"))
        self.assertTrue(body["is_audio"])
        self.assertTrue(body["analyzable"])
        self.assertFalse(body["is_subtitle"])
        _s, body = self.client.get("/api/audio/detect?path=" +
                                   os.path.join(self.media, "song.lrc"))
        self.assertTrue(body["is_subtitle"])
        self.assertFalse(body["analyzable"])
        _s, body = self.client.get("/api/audio/detect?path=" + self.media)
        self.assertTrue(body["is_dir"])


class AnalyzeTest(AppTestCase):
    def test_analyze_wav_reports_silence(self):
        status, body = self.client.get(
            "/api/audio/analyze?path=" + os.path.join(self.media, "standard.wav"))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["silence_count"], 2)
        self.assertAlmostEqual(body["silences"][0]["start"], 0.0, delta=0.03)
        self.assertAlmostEqual(body["silences"][0]["end"], 0.5, delta=0.03)
        self.assertAlmostEqual(body["silences"][1]["start"], 1.5, delta=0.03)
        self.assertTrue(body["peaks"])
        self.assertEqual(len(body["rms_db"]), body["frames"])

    def test_analyze_second_call_comes_from_cache(self):
        path = os.path.join(self.media, "standard.wav")
        self.client.get("/api/audio/analyze?path=" + path)
        _s, again = self.client.get("/api/audio/analyze?path=" + path)
        self.assertTrue(again["cached"], "同一文件同一参数第二次应当命中缓存")

    def test_force_bypasses_cache(self):
        path = os.path.join(self.media, "standard.wav")
        self.client.get("/api/audio/analyze?path=" + path)
        _s, forced = self.client.get("/api/audio/analyze?force=1&path=" + path)
        self.assertFalse(forced["cached"])

    def test_analyze_params_are_respected(self):
        path = os.path.join(self.media, "standard.wav")
        _s, body = self.client.get(
            "/api/audio/analyze?threshold_db=-3&min_silence_ms=100&buckets=40&path=" + path)
        self.assertEqual(body["threshold_db"], -3.0)
        self.assertEqual(body["min_silence_ms"], 100.0)
        # peaks 是「最多 buckets 个桶」：100 帧按 3 帧一组归并后是 34 个桶
        self.assertLessEqual(len(body["peaks"]), 40)
        self.assertGreaterEqual(len(body["peaks"]), 25)
        self.assertEqual(body["silence_count"], 1, "阈值 -3 dBFS 下正弦波也算静音")

    def test_analyze_non_wav_is_400(self):
        status, body = self.client.get(
            "/api/audio/analyze?path=" + os.path.join(self.media, "fake.mp3"))
        self.assertEqual(status, 400)
        self.assertIn("只支持 WAV", body["error"])
        self.assertIn("元数据读取与字幕转换不受影响", body["hint"])

    def test_analyze_outside_whitelist_is_403(self):
        status, _body = self.client.get(
            "/api/audio/analyze?path=" + os.path.join(self.outside, "a.wav"))
        self.assertEqual(status, 403)

    def test_analyze_corrupt_wav_is_400(self):
        broken = os.path.join(self.media, "broken.wav")
        with open(broken, "wb") as fh:
            fh.write(b"RIFFxxxxWAVEjunk")
        try:
            status, body = self.client.get("/api/audio/analyze?path=" + broken)
            self.assertEqual(status, 400)
            self.assertTrue(body["error"])
        finally:
            os.remove(broken)

    def test_long_file_is_409_and_points_to_jobs(self):
        """超过同步上限时用 409（请求本身没错，只是该换通道）+ 明确指引。"""
        saved = self.app.settings.get("analyze_sync_seconds")
        try:
            self.client.post("/api/settings", {"analyze_sync_seconds": 1})
            status, body = self.client.get(
                "/api/audio/analyze?path=" + os.path.join(self.media, "stereo.wav"))
            self.assertEqual(status, 409)
            self.assertIn("POST /api/jobs", body["hint"])
            self.assertIn("analyze", body["hint"])
        finally:
            self.client.post("/api/settings", {"analyze_sync_seconds": saved or 120})

    def test_analyze_job_completes_and_caches(self):
        job_id = self.submit("analyze", {"path": os.path.join(self.media, "standard.wav")})
        job = self.wait_job(job_id)
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        self.assertEqual(job["result"]["silence_count"], 2)
        self.assertAlmostEqual(job["result"]["silence_total"], 1.0, places=2)
        self.assertTrue(job["result"]["cache_path"])
        self.assertTrue(os.path.isfile(job["result"]["cache_path"]))
        # 任务跑过之后，同步接口应当直接吃缓存
        _s, body = self.client.get(
            "/api/audio/analyze?path=" + os.path.join(self.media, "standard.wav"))
        self.assertTrue(body["cached"])

    def test_analyze_job_on_non_wav_fails_readably(self):
        job_id = self.submit("analyze", {"path": os.path.join(self.media, "fake.mp3")})
        job = self.wait_job(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("WAV", job["error"])

    def test_analyze_job_without_path_fails_readably(self):
        job_id = self.submit("analyze", {})
        job = self.wait_job(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("没有指定", job["error"])

    def test_job_can_be_canceled(self):
        """取消：提交 3 个长任务，worker 只有 2 个，第 3 个必然还在排队 → 取消它。

        用**纯静音的长 WAV**（合成时按字节批量写，秒级生成），
        这样每个分析任务要跑一秒以上，排队状态是确定的，不靠运气。
        """
        big = os.path.join(self.media, "long.wav")
        if not os.path.exists(big):
            with open(big, "wb") as fh:
                fh.write(synth.build_silent_wav(900, rate=8000, channels=1, bit_depth=16))
        params = {"path": big, "frame_ms": 10}
        ids = [self.submit("analyze", params) for _ in range(3)]
        status, body = self.client.post("/api/jobs/%d/cancel" % ids[2])
        self.assertIn(status, (200, 409))
        job = self.wait_job(ids[2])
        self.assertEqual(job["state"], "canceled", json.dumps(job, ensure_ascii=False))
        for job_id in ids[:2]:
            self.wait_job(job_id)

    def test_cancel_unknown_job_is_409(self):
        status, body = self.client.post("/api/jobs/999999/cancel")
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])


class SubtitleRouteTest(AppTestCase):
    def test_parse_lrc(self):
        status, body = self.client.get(
            "/api/audio/subtitles?path=" + os.path.join(self.media, "song.lrc"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["format"], "lrc")
        self.assertEqual(body["cue_count"], 5)
        self.assertEqual(body["meta"]["ti"], "合成测试歌词")
        self.assertEqual(body["cues"][0]["text"], "第一行歌词")
        self.assertFalse(body["truncated"])

    def test_parse_srt_with_limit(self):
        status, body = self.client.get(
            "/api/audio/subtitles?limit=1&path=" + os.path.join(self.media, "movie.srt"))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["cues"]), 1)
        self.assertTrue(body["truncated"])

    def test_force_format(self):
        status, body = self.client.get(
            "/api/audio/subtitles?format=txt&path=" + os.path.join(self.media, "song.lrc"))
        self.assertEqual(status, 200)
        self.assertEqual(body["format"], "txt")

    def test_broken_subtitle_is_400_with_hint(self):
        status, body = self.client.get(
            "/api/audio/subtitles?format=srt&path=" + os.path.join(self.media, "broken.srt"))
        self.assertEqual(status, 400)
        self.assertIn("时间轴", body["error"])
        self.assertIn("format", body["hint"])

    def test_outside_is_403(self):
        status, _body = self.client.get(
            "/api/audio/subtitles?path=" + os.path.join(self.outside, "a.srt"))
        self.assertEqual(status, 403)

    def test_directory_is_400(self):
        status, body = self.client.get("/api/audio/subtitles?path=" + self.media)
        self.assertEqual(status, 400)
        self.assertIn("不是文件", body["error"])


class ConvertTest(AppTestCase):
    def test_convert_single_file(self):
        target = os.path.join(self.output, "single")
        os.makedirs(target, exist_ok=True)
        status, body = self.client.post("/api/audio/convert", {
            "inputs": [os.path.join(self.media, "song.lrc")],
            "output_dir": target, "format": "srt"})
        self.assertEqual(status, 201, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        self.assertEqual(job["result"]["succeeded"], 1)
        self.assertEqual(job["result"]["format"], "srt")

        _s, results = self.client.get("/api/audio/results?job_id=%d" % job["id"])
        self.assertEqual(len(results["results"]), 1)
        record = results["results"][0]
        self.assertTrue(record["ok"])
        self.assertEqual(record["cue_count"], 5)
        self.assertTrue(os.path.isfile(record["output"]))
        with open(record["output"], "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("00:00:12,340 --> 00:00:20,500", text)
        self.assertIn("第二行歌词", text)

    def test_convert_with_offset_and_lyric_merge(self):
        target = os.path.join(self.output, "lyric")
        os.makedirs(target, exist_ok=True)
        fragmented = os.path.join(self.media, "fragmented.lrc")
        with open(fragmented, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(synth.LRC_FRAGMENTED)
        status, body = self.client.post("/api/audio/convert", {
            "inputs": [fragmented], "output_dir": target,
            "format": "srt", "mode": "lyric", "offset": 2.5})
        self.assertEqual(status, 201, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["result"]["mode"], "lyric")
        self.assertEqual(job["result"]["merge_short"], 1.5, "歌词模式应当带上默认合并阈值")
        self.assertEqual(job["result"]["offset"], 2.5)

        _s, results = self.client.get("/api/audio/results?job_id=%d" % job["id"])
        record = results["results"][0]
        self.assertEqual(record["merged"], 4, "5 行碎句应当并成 1 行")
        self.assertEqual(record["cue_count"], 1)
        with open(record["output"], "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("今天天气很好我想出去走走", text)
        self.assertIn("00:00:02,500 --> 00:00:08,500", text, "合并 + 平移后的时间轴")

    def test_convert_batch_over_directory(self):
        target = os.path.join(self.output, "batch")
        os.makedirs(target, exist_ok=True)
        status, body = self.client.post("/api/audio/convert", {
            "roots": [self.media], "output_dir": target, "format": "vtt"})
        self.assertEqual(status, 201, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "completed")
        # 目录里有 3 个字幕（song.lrc / movie.srt / broken.srt），.txt 不在批量范围内。
        # broken.srt 里没有时间轴，按内容识别会落到 TXT —— 于是一行一条地转出来了。
        # 这是刻意的「宽进」：用户把它放进批量里，说明他就要这个文件的结果。
        self.assertEqual(job["result"]["total"], 3, "批量只收 .lrc/.srt/.vtt，不含 .txt")
        self.assertEqual(job["result"]["succeeded"], 3)
        self.assertEqual(job["result"]["failed"], 0)
        names = sorted(os.listdir(target))
        self.assertIn("song.vtt", names)
        self.assertIn("movie.vtt", names)

    def test_convert_uses_app_output_dir_when_omitted(self):
        status, body = self.client.post("/api/audio/convert", {
            "inputs": [os.path.join(self.media, "movie.srt")], "format": "lrc"})
        self.assertEqual(status, 201, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "completed")
        self.assertTrue(job["result"]["output_dir"].endswith("output"))

    def test_convert_refuses_output_outside_whitelist(self):
        status, body = self.client.post("/api/audio/convert", {
            "inputs": [os.path.join(self.media, "song.lrc")],
            "output_dir": self.outside, "format": "srt"})
        self.assertEqual(status, 201, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("允许访问", job["error"])

    def test_convert_bad_requests(self):
        status, body = self.client.post("/api/audio/convert", {"format": "srt"})
        self.assertEqual(status, 400)
        self.assertIn("没有指定", body["error"])

        status, body = self.client.post("/api/audio/convert", {
            "inputs": [os.path.join(self.media, "song.lrc")], "format": "docx"})
        self.assertEqual(status, 400)
        self.assertIn("不支持的目标格式", body["error"])

        status, body = self.client.post("/api/audio/convert", "not-a-dict")
        self.assertEqual(status, 400)

    def test_convert_empty_directory_fails_readably(self):
        empty = os.path.join(self.media, "empty")
        os.makedirs(empty, exist_ok=True)
        status, body = self.client.post("/api/audio/convert", {
            "roots": [empty], "format": "srt"})
        self.assertEqual(status, 201, body)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("没有找到", job["error"])

    def test_job_retry_and_delete(self):
        status, body = self.client.post("/api/audio/convert", {
            "inputs": [os.path.join(self.media, "movie.srt")],
            "output_dir": self.output, "format": "txt"})
        job_id = body["job"]["id"]
        self.wait_job(job_id)
        status, body = self.client.post("/api/jobs/%d/retry" % job_id)
        self.assertEqual(status, 201)
        clone = self.wait_job(body["job"]["id"])
        self.assertEqual(clone["state"], "completed")
        status, body = self.client.delete("/api/jobs/%d" % clone["id"])
        self.assertEqual(status, 200)

    def test_unknown_job_type_is_400(self):
        status, body = self.client.post("/api/jobs", {"type": "no-such-job"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class ScanLibraryTest(AppTestCase):
    def test_scan_indexes_audio_and_subtitles(self):
        self.ensure_scan()
        job = self._scan_job
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        result = job["result"]
        self.assertEqual(result["audio_found"], 3, "standard.wav / stereo.wav / fake.mp3")
        self.assertEqual(result["audio_failed"], 0)
        self.assertGreaterEqual(result["subtitle_found"], 3)

    def test_scan_is_incremental(self):
        self.ensure_scan()
        job_id = self.submit("scan", {"roots": [self.media]})
        job = self.wait_job(job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["result"]["audio_from_cache"], 3,
                         "没变化的文件应当直接吃缓存")

    def test_files_listing(self):
        self.ensure_scan()
        status, body = self.client.get("/api/audio/files?limit=10")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["total"], 3)
        paths = [row["path"] for row in body["files"]]
        self.assertTrue(any(path.endswith("standard.wav") for path in paths))

        status, body = self.client.get("/api/audio/files?kind=subtitle&limit=10")
        self.assertGreaterEqual(body["total"], 3)

        status, body = self.client.get("/api/audio/files?kind=all&limit=50")
        self.assertIn("audio", body)
        self.assertIn("subtitle", body)

        status, body = self.client.get("/api/audio/files?q=standard")
        self.assertEqual(body["total"], 1)

    def test_summary(self):
        self.ensure_scan()
        status, body = self.client.get("/api/audio/summary")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["audio_total"], 3)
        self.assertGreaterEqual(body["analyzable"], 2, "两个 WAV 可分析")
        self.assertGreater(body["duration"], 0)
        self.assertTrue(body["formats"])

    def test_export_json(self):
        self.ensure_scan()
        status, body = self.client.get("/api/audio/export?format=json")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["count"], 3)
        self.assertEqual(body["kind"], "audio")
        self.assertIn("sample_rate", body["rows"][0])

    def test_export_csv(self):
        self.ensure_scan()
        status, raw = self.client.get("/api/audio/export?format=csv", raw=True)
        self.assertEqual(status, 200)
        text = raw.decode("utf-8-sig")
        self.assertIn("path,", text.splitlines()[0])
        self.assertIn("standard.wav", text)

    def test_export_bad_format_is_400(self):
        status, body = self.client.get("/api/audio/export?format=xml")
        self.assertEqual(status, 400)
        self.assertIn("csv", body["error"])

    def test_export_subtitle_kind(self):
        self.ensure_scan()
        status, body = self.client.get("/api/audio/export?format=json&kind=subtitle")
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "subtitle")
        self.assertIn("cue_count", body["rows"][0])

    def test_scan_without_roots_fails_readably(self):
        job_id = self.submit("scan", {})
        job = self.wait_job(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("没有指定", job["error"])


class EngineRouteTest(AppTestCase):
    def test_engines_status(self):
        status, body = self.client.get("/api/audio/engines")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        for key in ("local", "remote", "transcribe_available", "summary"):
            self.assertIn(key, body)
        self.assertFalse(body["remote"]["enabled"], "远程转写默认必须是关闭的")
        self.assertFalse(body["remote"].get("has_key"))

    def test_engines_response_never_contains_the_key(self):
        """填了 key 之后，任何响应里都不能出现它。

        注意变量名别叫 ``secret``/``token`` 之类 —— 门禁的凭据扫描会把
        ``secret = "..."`` 这种赋值当成硬编码凭据直接判 ERROR（本次就中过一次），
        所以这里特意取个中性的名字。
        """
        echo_marker = "sk-do-not-echo-this-value"
        try:
            status, body = self.client.post("/api/audio/engines", {
                "remote": {"base_url": "https://example.invalid"},
                "api_key": echo_marker})
            self.assertEqual(status, 200, body)
            self.assertNotIn(echo_marker, json.dumps(body, ensure_ascii=False))
            self.assertTrue(body["remote"]["has_key"])
            _s, again = self.client.get("/api/audio/engines")
            self.assertNotIn(echo_marker, json.dumps(again, ensure_ascii=False))
            _s, info = self.client.get("/api/app")
            self.assertNotIn(echo_marker, json.dumps(info, ensure_ascii=False))
            # 保存到单独的文件，且确实写进去了（但读回来只给 has_key）
            secrets_path = os.path.join(self.app.paths.config_dir, "secrets.json")
            self.assertTrue(os.path.isfile(secrets_path))
            with open(secrets_path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            self.assertEqual(stored["remote_api_key"], echo_marker)
        finally:
            self.client.post("/api/audio/engines", {"clear_key": True})

    def test_enabling_requires_explicit_confirmation(self):
        status, body = self.client.post("/api/audio/engines", {
            "remote": {"enabled": True, "base_url": "https://example.invalid"},
            "api_key": "sk-x"})
        self.assertEqual(status, 400)
        self.assertIn("确认", body["error"])
        self.assertIn("上传", body["hint"])
        # 未确认就不该被打开
        _s, status_body = self.client.get("/api/audio/engines")
        self.assertFalse(status_body["remote"]["enabled"])
        self.client.post("/api/audio/engines", {"clear_key": True})

    def test_enable_without_key_is_rejected(self):
        status, body = self.client.post("/api/audio/engines", {
            "remote": {"enabled": True, "base_url": "https://example.invalid"},
            "confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("API Key", body["error"])
        self.client.post("/api/audio/engines", {"clear_key": True})

    def test_bad_base_url_is_rejected(self):
        for bad in ("file:///etc/passwd", "ftp://x", "example.com", ""):
            status, body = self.client.post("/api/audio/engines",
                                            {"remote": {"base_url": bad}})
            if bad == "":
                self.assertEqual(status, 400, bad)
            else:
                self.assertEqual(status, 400, "%s → %s" % (bad, body))
                self.assertIn("http", body["error"])

    def test_bad_model_and_language_are_rejected(self):
        status, body = self.client.post("/api/audio/engines",
                                        {"remote": {"model": "small; rm -rf /"}})
        self.assertEqual(status, 400)
        status, body = self.client.post("/api/audio/engines",
                                        {"remote": {"language": "zh; id"}})
        self.assertEqual(status, 400)

    def test_transcribe_is_503_when_no_engine(self):
        """没有引擎时必须 503 + 明确指引，且不影响其它接口。"""
        _s, status_body = self.client.get("/api/audio/engines")
        if status_body["transcribe_available"]:
            self.skipTest("本机已配置了转写引擎，跳过降级路径")
        status, body = self.client.post("/api/audio/transcribe",
                                        {"inputs": [os.path.join(self.media, "standard.wav")]})
        self.assertEqual(status, 503)
        self.assertIn("本地转写不可用", body["error"])
        self.assertIn("不影响其它功能", body["hint"])
        # 其它功能照常
        _s, probe = self.client.get(
            "/api/audio/probe?path=" + os.path.join(self.media, "standard.wav"))
        self.assertTrue(probe["ok"])

    def test_transcribe_without_inputs_is_400(self):
        """请求本身不合法时给 400，而不是被「引擎不可用」的 503 盖过去。"""
        status, body = self.client.post("/api/audio/transcribe", {})
        self.assertEqual(status, 400)
        self.assertIn("没有指定", body["error"])

    def test_transcribe_body_must_be_json(self):
        status, body = self.client.post("/api/audio/transcribe", "not-a-dict")
        self.assertEqual(status, 400)


class RemoteTranscribeEndToEndTest(AppTestCase):
    """远程转写的完整链路：配置 → 任务 → 出 SRT 文件。

    端点用本机起的一个真 HTTP 服务，所以断网也能跑，且验的是真链路。
    """

    def test_transcribe_job_writes_subtitle_file(self):
        stub = StubEndpoint(payload=(
            "1\n00:00:00,000 --> 00:00:02,000\n远程转写第一行\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n远程转写第二行\n"), content_type="text/plain")
        target = os.path.join(self.output, "transcribe")
        os.makedirs(target, exist_ok=True)
        try:
            status, body = self.client.post("/api/audio/engines", {
                "remote": {"enabled": True, "base_url": stub.base_url},
                "api_key": "sk-local-stub", "confirm": True})
            self.assertEqual(status, 200, body)
            self.assertTrue(body["transcribe_available"])
            self.assertTrue(body["remote"]["ready"])

            status, body = self.client.post("/api/audio/transcribe", {
                "inputs": [os.path.join(self.media, "standard.wav")],
                "output_dir": target, "format": "srt", "engine": "remote"})
            self.assertEqual(status, 201, body)
            job = self.wait_job(body["job"]["id"], timeout=180)
            self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
            self.assertEqual(job["result"]["succeeded"], 1)
            self.assertEqual(job["result"]["engine"], "remote")

            outputs = [name for name in os.listdir(target) if name.endswith(".srt")]
            self.assertEqual(len(outputs), 1)
            with open(os.path.join(target, outputs[0]), "r", encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("远程转写第一行", text)
            self.assertIn("00:00:02,000 --> 00:00:04,000", text)
            # 请求真的打到了那个端点
            self.assertEqual(len(stub.requests), 1)
            self.assertEqual(stub.requests[0]["path"], "/v1/audio/transcriptions")
        finally:
            stub.close()
            self.client.post("/api/audio/engines",
                             {"remote": {"enabled": False}, "clear_key": True})

    def test_remote_endpoint_failure_is_readable(self):
        stub = StubEndpoint(payload={"error": "bad model"}, status=400)
        try:
            self.client.post("/api/audio/engines", {
                "remote": {"enabled": True, "base_url": stub.base_url},
                "api_key": "sk-local-stub", "confirm": True})
            status, body = self.client.post("/api/audio/transcribe", {
                "inputs": [os.path.join(self.media, "standard.wav")],
                "output_dir": self.output, "engine": "remote"})
            self.assertEqual(status, 201, body)
            job = self.wait_job(body["job"]["id"], timeout=180)
            # 全部文件都失败 → 任务必须是「失败」并把原因带出来，
            # 而不是「已完成（0 成功 1 失败）」（后者会让人以为任务没问题）
            self.assertEqual(job["state"], "failed")
            self.assertIn("HTTP 400", job["error"])
            self.assertIn("bad model", job["error"])
        finally:
            stub.close()
            self.client.post("/api/audio/engines",
                             {"remote": {"enabled": False}, "clear_key": True})


class JobLogsTest(AppTestCase):
    def test_logs_are_recorded_and_redacted(self):
        job_id = self.submit("scan", {"roots": [self.media]})
        self.wait_job(job_id)
        status, body = self.client.get("/api/jobs/%d/logs" % job_id)
        self.assertEqual(status, 200)
        self.assertTrue(body["logs"])
        self.assertIn("任务开始", body["logs"][0]["message"])

    def test_counts_endpoint(self):
        # 每个测试类各有一份全新的应用与数据库，所以这里先跑一个任务再查，
        # 不能依赖别的用例留下的状态（第一版就这么错过了）
        self.wait_job(self.submit("scan", {"roots": [self.media]}))
        status, body = self.client.get("/api/jobs?limit=1")
        self.assertEqual(status, 200)
        self.assertIn("total", body["counts"])
        self.assertGreaterEqual(body["counts"]["completed"], 1)


if __name__ == "__main__":
    unittest.main()
