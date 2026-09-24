"""可选转写引擎用例。

**这里没有装 faster-whisper，也没有任何外网**，所以「引擎本身转写得好不好」
不在验证范围内。验的是本应用自己写的那部分，而且都尽量**真跑**而不是打桩：

* 本机命令这条路：用一个真的子进程（``sys.executable`` + 一个脚本）走通
  ``run_local`` 的完整路径 —— 参数列表调用、退出码处理、超时、输出文件定位，
  全部是真实执行的结果，不是 mock 出来的。
* 远程接口这条路：在**本机起一个真的 HTTP 服务**当 OpenAI 兼容端点，
  真的发一次 multipart 请求、真的解析响应。断网也能跑，且验的是真链路。
* 关键安全性质：``--help`` 探测、参数白名单（拒绝 ``;`` / ``|`` / ``$``）、
  凭据永不回显 都有直接断言。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from app import engines  # noqa: E402

SRT_RESULT = ("1\n00:00:00,000 --> 00:00:02,000\n合成转写第一行\n\n"
              "2\n00:00:02,000 --> 00:00:04,000\n合成转写第二行\n")

#: 假的「转写命令」：把 argv 里的 --output_dir 解出来，写一份固定 SRT。
#: 用 ``sys.executable`` 当可执行文件跑它 —— 这样测的是真实子进程调用，
#: 而不是把 subprocess.run 换成 mock 自己骗自己。
#:
#: 两个踩过的坑，都写在这里免得下一个人再踩：
#:   1. 子进程里的 ``sys.argv[0]`` 是**脚本自己的路径**，run_local 传进来的
#:      「音频文件」正好就是它；写成 ``sys.argv[1]`` 拿到的是 ``--model``，
#:      最后写出的文件叫 ``--model.srt``，测试报「找不到结果文件」。
#:   2. 这个脚本的正文本身是被写进文件的字符串，所以里面**不能用 ``\n`` 转义**
#:      （外层字符串会先把它变成真正的换行，写出来的脚本就语法错误）。
#:      用 ``chr(10)`` 拼换行，绕开两层转义。
FAKE_CLI = '''import os
import sys

NL = chr(10)
source = sys.argv[0]
args = sys.argv[1:]
output_dir = "."
for index, item in enumerate(args):
    if item == "--output_dir" and index + 1 < len(args):
        output_dir = args[index + 1]
if "fail" in os.path.basename(output_dir):
    sys.stderr.write("fake engine: model not found on this host" + NL)
    sys.exit(3)
if "hang" in os.path.basename(output_dir):
    import time
    time.sleep(30)
stem = os.path.splitext(os.path.basename(source))[0]
os.makedirs(output_dir, exist_ok=True)
with open(os.path.join(output_dir, stem + ".srt"), "w", encoding="utf-8") as handle:
    handle.write(NL.join(["1", "00:00:00,000 --> 00:00:02,000", "假引擎结果", ""]))
'''

FAKE_FAIL = FAKE_CLI


class TempBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh10-eng-")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path


class LocalDetectionTest(TempBase):
    def setUp(self):
        # 探测结果在模块级缓存，每条用例前后都清掉，避免互相影响
        engines._state.update(checked=False, available=False, name=None, path=None,
                              version="", error=None)

    def test_status_shape_when_engine_absent(self):
        engines.detect_local(force=True)
        status = engines.local_status()
        for key in ("name", "available", "command", "version", "detail", "enables"):
            self.assertIn(key, status)
        self.assertEqual(status["name"], "faster-whisper")
        self.assertIn("transcribe", status["enables"])
        # 本机没有 faster-whisper 时必须是「不可用 + 可读原因」，而不是抛异常
        if not status["available"]:
            self.assertTrue(status["detail"])

    def test_detect_uses_which_and_really_runs_the_command(self):
        """把 which 指到当前解释器：探测会真的执行 ``--help`` 并拿到版本行。"""
        original = engines.shutil.which
        engines.shutil.which = lambda name: sys.executable if name == "faster-whisper" else None
        try:
            info = engines.detect_local(force=True)
        finally:
            engines.shutil.which = original
            engines._state.update(checked=False)
        self.assertTrue(info["available"])
        self.assertEqual(info["name"], "faster-whisper")
        self.assertTrue(info["version"], "应当从 --help 的输出里取到一行版本信息")

    def test_cache_avoids_repeat_probing(self):
        engines.detect_local(force=True)
        calls = []
        original = engines.shutil.which
        engines.shutil.which = lambda name: calls.append(name) or None
        try:
            engines.detect_local()
        finally:
            engines.shutil.which = original
        self.assertEqual(calls, [], "已探测过就不该再调 which")

    def test_broken_command_reports_error(self):
        original = engines.shutil.which
        engines.shutil.which = lambda name: (
            os.path.join(self.tmp, "no-such-binary") if name == "faster-whisper" else None)
        try:
            info = engines.detect_local(force=True)
        finally:
            engines.shutil.which = original
            engines._state.update(checked=False)
        self.assertFalse(info["available"])
        self.assertTrue(info["error"])


class LocalArgsTest(unittest.TestCase):
    def test_args_are_a_list_without_shell(self):
        args = engines.build_local_args("/usr/bin/faster-whisper", "/tmp/a.wav", "/tmp/out",
                                        model="small", language="zh")
        self.assertIsInstance(args, list)
        self.assertEqual(args[0], "/usr/bin/faster-whisper")
        self.assertEqual(args[1], "/tmp/a.wav")
        self.assertIn("--model", args)
        self.assertIn("small", args)
        self.assertIn("--language", args)
        self.assertIn("--output_dir", args)

    def test_injection_attempts_are_refused(self):
        for bad in ("small; rm -rf /", "small|cat /etc/passwd", "$(whoami)",
                    "small && curl evil", "small`id`", "small\nid"):
            with self.assertRaises(engines.EngineError):
                engines.build_local_args("cmd", "a.wav", "out", model=bad)
        with self.assertRaises(engines.EngineError):
            engines.build_local_args("cmd", "a.wav", "out", language="zh; id")
        with self.assertRaises(engines.EngineError):
            engines.build_local_args("cmd", "a.wav", "out", output_format="; id")

    def test_safe_token_is_ascii_only(self):
        """只放行 ASCII 字母数字与 ``._-``。

        注意 ``"中文".isalnum()`` 在 Python 里是 True —— 第一版直接用它做白名单，
        于是「只放行 ASCII」这句注释就成了假话。这里显式要求 ASCII。
        """
        for good in ("small", "large-v3", "distil_whisper.en", "zh"):
            self.assertTrue(engines.safe_token(good))
        for bad in ("", "a b", "a;b", "a/b", "a$b", "中文", "café"):
            self.assertFalse(engines.safe_token(bad))
        self.assertFalse(engines.safe_token("a\nb"), "换行也必须被拒")
        self.assertFalse(engines.safe_token("a\tb"))

    def test_no_shell_execution_anywhere(self):
        """把「绝不 shell 执行」钉在测试里 —— 用 AST 查，不靠字符串匹配。

        字符串匹配会把注释里那句「没有 shell=True」也算成违规（第一版就误报了）。
        """
        import ast

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "app", "engines.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        self.fail("engines.py 里出现了 shell= 参数（第 %d 行）" % node.lineno)
                name = getattr(node.func, "attr", "")
                if name in ("system", "popen", "run_in_shell"):
                    self.fail("engines.py 里出现了 %s（第 %d 行）" % (name, node.lineno))


class LocalRunTest(TempBase):
    """真的起一个子进程来跑 run_local。"""

    def test_run_local_produces_output(self):
        work = os.path.join(self.tmp, "work")
        result = engines.run_local(sys.executable, self.fake_engine, work, model="small",
                                   output_format="srt", timeout=30)
        self.assertTrue(os.path.isfile(result["output"]))
        self.assertEqual(result["format"], "srt")
        with open(result["output"], "r", encoding="utf-8") as fh:
            self.assertIn("假引擎结果", fh.read())

    def test_nonzero_exit_is_readable_error(self):
        """退出码非 0 → 可读错误，且带上 stderr 的最后几行。

        触发方式是把输出目录命名为含 "fail"（假引擎据此模拟失败）——
        用假的模型名不行，那会被参数白名单先拦掉。
        """
        work = os.path.join(self.tmp, "work-fail")
        with self.assertRaises(engines.EngineError) as ctx:
            engines.run_local(sys.executable, self.fake_engine, work, model="small",
                              timeout=30)
        message = str(ctx.exception)
        self.assertIn("退出码 3", message)
        self.assertIn("model not found", message)

    @property
    def fake_engine(self):
        """假的转写命令脚本（真子进程执行）。"""
        if not hasattr(self, "_fake_engine"):
            self._fake_engine = self.write("fake_engine.py", FAKE_CLI)
        return self._fake_engine

    def test_missing_output_file_is_readable_error(self):
        """命令成功退出但没写文件：要报「找不到结果文件」，而不是静默成功。"""
        script = self.write("silent.py", "import sys\nsys.exit(0)\n")
        work = os.path.join(self.tmp, "work-silent")
        with self.assertRaises(engines.EngineError) as ctx:
            engines.run_local(sys.executable, script, work, timeout=30)
        self.assertIn("没有找到结果文件", str(ctx.exception))

    def test_timeout_is_enforced(self):
        script = self.write("slow.py", "import time\ntime.sleep(30)\n")
        work = os.path.join(self.tmp, "work-slow")
        with self.assertRaises(engines.EngineError) as ctx:
            engines.run_local(sys.executable, script, work, timeout=1)
        self.assertIn("超时", str(ctx.exception))

    def test_unlaunchable_command(self):
        with self.assertRaises(engines.EngineError) as ctx:
            engines.run_local(os.path.join(self.tmp, "nope-binary"), "a.wav",
                              os.path.join(self.tmp, "w"), timeout=5)
        self.assertIn("无法启动", str(ctx.exception))


class RemoteUrlTest(unittest.TestCase):
    def test_accepts_http_and_https(self):
        self.assertEqual(engines.validate_base_url("https://api.example.com/"),
                         "https://api.example.com")
        self.assertEqual(engines.validate_base_url("http://10.0.0.5:8000"),
                         "http://10.0.0.5:8000")

    def test_refuses_other_schemes(self):
        for bad in ("file:///etc/passwd", "ftp://x/y", "javascript:alert(1)",
                    "//example.com", "", "   "):
            with self.assertRaises(engines.EngineError):
                engines.validate_base_url(bad)


class MultipartTest(TempBase):
    def test_body_contains_fields_and_file(self):
        source = self.write("clip.wav", "RIFF....WAVEfmt ")
        content_type, body = engines.build_multipart(
            {"model": "whisper-1", "response_format": "srt", "language": None},
            "file", source)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="model"', body)
        self.assertIn(b"whisper-1", body)
        self.assertIn(b"response_format", body)
        self.assertNotIn(b"language", body, "空字段应当被跳过")
        self.assertIn(b'filename="clip.wav"', body)
        self.assertIn(b"RIFF....WAVEfmt ", body)
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(body.rstrip().endswith(("--%s--" % boundary).encode()))

    def test_filename_is_sanitised(self):
        source = self.write("clip.wav", "data")
        _ctype, body = engines.build_multipart({}, "file", source, filename='a"b.wav')
        self.assertIn(b'filename="a_b.wav"', body)


class StubEndpoint:
    """本机起一个 OpenAI 兼容端点（真的 HTTP 服务，不是 mock）。"""

    def __init__(self, payload=None, status=200, content_type="application/json"):
        self.requests = []
        self.payload = payload if payload is not None else {"text": "合成转写"}
        self.status = status
        self.content_type = content_type
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                stub.requests.append({
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type", ""),
                    "authorization": self.headers.get("Authorization", ""),
                    "body": body,
                })
                if isinstance(stub.payload, (dict, list)):
                    raw = json.dumps(stub.payload).encode("utf-8")
                    ctype = "application/json"
                else:
                    raw = stub.payload.encode("utf-8")
                    ctype = stub.content_type
                self.send_response(stub.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RemoteCallTest(TempBase):
    def setUp(self):
        self.source = self.write("clip.wav", "RIFFfakeaudio")
        self.stub = None

    def tearDown(self):
        if self.stub:
            self.stub.close()

    def test_successful_call_and_payload_parsing(self):
        self.stub = StubEndpoint(payload=SRT_RESULT, content_type="text/plain")
        payload = engines.transcribe_remote(self.stub.base_url, "sk-secret-value",
                                            self.source, model="whisper-1",
                                            response_format="srt", timeout=20)
        self.assertEqual(payload["status"], 200)
        # 请求真的到了那个端点，路径与鉴权头都对
        request = self.stub.requests[0]
        self.assertEqual(request["path"], "/v1/audio/transcriptions")
        self.assertEqual(request["authorization"], "Bearer sk-secret-value")
        self.assertIn(b"RIFFfakeaudio", request["body"])
        # 响应能解析成 cue
        cues, fmt = engines.parse_remote_payload(payload)
        self.assertEqual(fmt, "srt")
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "合成转写第一行")
        self.assertAlmostEqual(cues[1]["end"], 4.0)

    def test_base_url_with_v1_prefix_is_not_doubled(self):
        self.stub = StubEndpoint(payload={"text": "x"})
        engines.transcribe_remote(self.stub.base_url + "/v1/audio/transcriptions",
                                  "k", self.source, timeout=20)
        self.assertEqual(self.stub.requests[0]["path"], "/v1/audio/transcriptions")

    def test_http_error_becomes_readable_and_hides_nothing(self):
        self.stub = StubEndpoint(payload={"error": "model not found"}, status=404)
        with self.assertRaises(engines.EngineError) as ctx:
            engines.transcribe_remote(self.stub.base_url, "sk-secret-value", self.source,
                                      timeout=20)
        message = str(ctx.exception)
        self.assertIn("404", message)
        self.assertIn("model not found", message)

    def test_connection_refused_is_readable(self):
        # 端口 1 上不会有服务
        with self.assertRaises(engines.EngineError) as ctx:
            engines.transcribe_remote("http://127.0.0.1:1", "k", self.source, timeout=3)
        self.assertIn("无法连接", str(ctx.exception))

    def test_no_api_key_means_no_authorization_header(self):
        self.stub = StubEndpoint(payload={"text": "x"})
        engines.transcribe_remote(self.stub.base_url, "", self.source, timeout=20)
        self.assertEqual(self.stub.requests[0]["authorization"], "")


class PayloadParsingTest(unittest.TestCase):
    def test_verbose_json_segments(self):
        payload = {"content_type": "application/json", "body": json.dumps({
            "text": "全部文本",
            "segments": [{"start": 0.0, "end": 1.5, "text": "第一段"},
                         {"start": 1.5, "end": 3.0, "text": "第二段"}],
        })}
        cues, fmt = engines.parse_remote_payload(payload)
        self.assertEqual(fmt, "segments")
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[1]["start"], 1.5)

    def test_plain_text_is_untimed(self):
        payload = {"content_type": "text/plain", "body": "第一行\n第二行\n"}
        cues, fmt = engines.parse_remote_payload(payload)
        self.assertEqual(fmt, "text")
        self.assertIsNone(cues[0]["start"])
        self.assertEqual(len(cues), 2)

    def test_json_text_field(self):
        payload = {"content_type": "application/json", "body": json.dumps({"text": "只有文本"})}
        cues, fmt = engines.parse_remote_payload(payload)
        self.assertEqual(fmt, "text")
        self.assertEqual(cues[0]["text"], "只有文本")

    def test_empty_response_is_refused(self):
        with self.assertRaises(engines.EngineError):
            engines.parse_remote_payload({"content_type": "text/plain", "body": "  "})

    def test_json_without_text_or_segments_is_refused(self):
        payload = {"content_type": "application/json", "body": json.dumps({"foo": 1})}
        with self.assertRaises(engines.EngineError):
            engines.parse_remote_payload(payload)


class SecretsTest(TempBase):
    def test_write_then_read(self):
        path = os.path.join(self.tmp, "config", "secrets.json")
        engines.write_secrets(path, {"remote_api_key": "sk-abc123"})
        self.assertEqual(engines.read_secrets(path)["remote_api_key"], "sk-abc123")

    def test_permissions_are_600_on_posix(self):
        path = os.path.join(self.tmp, "config2", "secrets.json")
        engines.write_secrets(path, {"remote_api_key": "sk-abc"})
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_missing_or_broken_file_returns_empty(self):
        self.assertEqual(engines.read_secrets(os.path.join(self.tmp, "nope.json")), {})
        broken = self.write("broken.json", "{ not json")
        self.assertEqual(engines.read_secrets(broken), {})

    def test_public_secrets_never_contains_the_key(self):
        public = engines.public_secrets({"remote_api_key": "sk-super-secret"})
        self.assertEqual(public, {"has_key": True, "key_length": len("sk-super-secret")})
        self.assertNotIn("sk-super-secret", json.dumps(public))

    def test_status_never_contains_the_key(self):
        settings = {"remote_enabled": True, "remote_base_url": "https://example.com",
                    "remote_model": "whisper-1", "remote_language": ""}
        status = engines.engine_status(settings, {"remote_api_key": "sk-super-secret"})
        blob = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("sk-super-secret", blob)
        self.assertTrue(status["remote"]["has_key"])
        self.assertTrue(status["remote"]["ready"])
        self.assertTrue(status["transcribe_available"])

    def test_remote_not_ready_without_key(self):
        settings = {"remote_enabled": True, "remote_base_url": "https://example.com"}
        config = engines.public_remote(settings, {})
        self.assertFalse(config["ready"])
        self.assertFalse(config["has_key"])

    def test_remote_disabled_by_default(self):
        status = engines.engine_status({}, {})
        self.assertFalse(status["remote"]["enabled"])
        self.assertFalse(status["remote"]["ready"])

    def test_unavailable_status_gives_guidance(self):
        """没有本地命令也没有远程接口时，必须给出「不影响其它功能」的指引。"""
        engines._state.update(checked=True, available=False, name=None, path=None,
                              version="", error="系统上没有找到 faster-whisper 命令")
        try:
            status = engines.engine_status({}, {})
        finally:
            engines._state.update(checked=False)
        if not status["local"]["available"]:
            self.assertFalse(status["transcribe_available"])
            self.assertIn("本地转写不可用", status["summary"])
            self.assertIn("不影响其它功能", status["hint"])
            self.assertIn("faster-whisper", status["hint"])


if __name__ == "__main__":
    unittest.main()
