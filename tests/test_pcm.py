"""WAV 解码与信号分析用例。

**静音检测的容忍度是怎么定的（这段是被断言直接引用的依据）**

* 逐帧指标的帧长默认 20 ms，静音段的边界**必然量化到帧**，所以误差上界就是一个帧长。
* 跨越边界的那一帧，只要含有信号能量（哪怕只占该帧时长的 10%）就会被判为「有声」，
  因此实测的静音段**通常比理论值短**。
* 合成样本里 0.5 秒 = 25 帧整，边界刚好落在帧边界上，所以
  「0.5 秒静音 + 1 秒正弦 + 0.5 秒静音」的实测值**恰好是** [0, 0.5] 与 [1.5, 2.0]。
  为了不把「刚好对齐」当成通用结论，另有一条**故意错开帧边界**的用例
  （起始 0.123 秒），在那一例里断言的就是 ±1 帧的容忍度。
"""

import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from app import audiometa, pcm  # noqa: E402

from tests import synth  # noqa: E402

#: 帧长（毫秒），与 analyze() 的默认值一致
FRAME_MS = 20.0
#: 容忍度：一个帧长 + 5 ms 的取整余量
TOLERANCE = FRAME_MS / 1000.0 + 0.005


class PcmBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh10-pcm-")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def assertClose(self, actual, expected, tolerance=TOLERANCE, label=""):
        self.assertLessEqual(
            abs(actual - expected), tolerance,
            "%s 实测 %.4f，期望 %.4f，相差 %.4f 秒（容忍度 %.4f）"
            % (label, actual, expected, abs(actual - expected), tolerance))


class SilenceDetectionTest(PcmBase):
    """合成样本上的静音检测 —— 本应用的验收核心。"""

    def setUp(self):
        self.path = self.write("standard.wav", synth.build_wav(synth.STANDARD_SEGMENTS))
        self.result = pcm.analyze(self.path)

    def test_two_silence_runs_found(self):
        silences = self.result["silences"]
        self.assertEqual(len(silences), 2, silences)
        self.assertClose(silences[0]["start"], 0.0, label="首段静音起点")
        self.assertClose(silences[0]["end"], 0.5, label="首段静音终点")
        self.assertClose(silences[1]["start"], 1.5, label="末段静音起点")
        self.assertClose(silences[1]["end"], 2.0, label="末段静音终点")

    def test_totals_add_up(self):
        self.assertAlmostEqual(self.result["duration"], 2.0, places=6)
        total = sum(item["duration"] for item in self.result["silences"])
        self.assertAlmostEqual(self.result["silence_total"], round(total, 3), places=3)
        self.assertAlmostEqual(
            self.result["silence_total"] + self.result["speech_total"],
            self.result["duration"], places=3)

    def test_frame_metrics_shape(self):
        self.assertEqual(self.result["frames"], 100)      # 2 秒 ÷ 20 ms
        self.assertEqual(self.result["frame_ms"], 20.0)
        self.assertEqual(len(self.result["rms_db"]), 100)
        self.assertEqual(len(self.result["zcr"]), 100)

    def test_silent_frames_are_floor_and_tone_frames_are_loud(self):
        """数字静音 → 下限值；正弦波振幅 0.5 → RMS ≈ 0.3536 → 约 -9.03 dBFS。"""
        series = self.result["rms_db"]
        self.assertEqual(series[0], pcm.SILENCE_DB_FLOOR)
        self.assertEqual(series[-1], pcm.SILENCE_DB_FLOOR)
        # 中段（第 50 帧 ≈ 1.0 秒处）应当是正弦波
        self.assertAlmostEqual(series[50], 20 * math.log10(0.5 / math.sqrt(2)), delta=0.5)

    def test_zero_crossing_rate_of_tone(self):
        """440 Hz 在 44.1 kHz 下每样本过零概率 = 2×440/44100 ≈ 0.01995。"""
        expected = 2 * 440 / 44100.0
        self.assertAlmostEqual(self.result["zcr"][50], expected, delta=0.004)
        self.assertEqual(self.result["zcr"][0], 0.0)

    def test_peaks_cover_the_tone(self):
        peaks = self.result["peaks"]
        self.assertTrue(peaks)
        loud = [item for item in peaks if abs(item["max"]) > 0.4]
        self.assertTrue(loud, "正弦波段落应当有接近 ±0.5 的峰值")
        self.assertLessEqual(max(item["max"] for item in peaks), 0.51)
        self.assertGreaterEqual(min(item["min"] for item in peaks), -0.51)

    def test_offset_boundaries_quantise_to_frames(self):
        """故意让边界落在帧中间：容忍度就是「一个帧长」这句话的实证。

        样本：0.523 秒静音 + 0.4 秒正弦 + 0.62 秒静音（0.523 与 0.923 都不是 20 ms 的整数倍）。

        逐帧算出来的期望值（帧 i 覆盖 [0.02i, 0.02(i+1))）：

        * 正弦从样本 23064（= 0.523 s）开始，落在第 26 帧内部，
          该帧 15% 有信号 → 判为有声 → 首段静音实测止于 **0.52**（比理论值**早**）。
        * 正弦到样本 40704（= 0.923 s）结束，第 46 帧同样含 15% 信号 → 有声
          → 末段静音实测始于 **0.94**（比理论值**晚**）。

        两个方向都出现了，正是「边界量化到帧」的直接证据；误差都在一个帧长以内。
        """
        path = self.write("offset.wav", synth.build_wav(
            [("silence", 0.523), ("tone", 0.4), ("silence", 0.62)]))
        result = pcm.analyze(path)
        self.assertEqual(len(result["silences"]), 2, result["silences"])

        first = result["silences"][0]
        self.assertClose(first["start"], 0.0, label="错开样本的首段起点")
        self.assertClose(first["end"], 0.523, label="错开样本的首段终点")
        self.assertAlmostEqual(first["end"], 0.52, places=3)
        self.assertLess(first["end"], 0.523,
                        "跨越边界的那一帧含信号，实测终点应当不晚于理论值")

        second = result["silences"][1]
        self.assertClose(second["start"], 0.923, label="错开样本的末段起点")
        self.assertAlmostEqual(second["start"], 0.94, places=3)
        self.assertGreater(second["start"], 0.923,
                           "尾帧含信号 → 实测起点应当不早于理论值")
        self.assertAlmostEqual(second["end"], result["duration"], places=3)

    def test_threshold_is_configurable(self):
        """把阈值提到 -3 dBFS：连正弦波（约 -9 dBFS）都被判成静音。"""
        result = pcm.analyze(self.path, threshold_db=-3.0)
        self.assertEqual(result["silence_count"], 1)
        self.assertAlmostEqual(result["silences"][0]["duration"], 2.0, places=3)

    def test_min_silence_filters_short_gaps(self):
        """最短静音时长设成 0.6 秒时，两段 0.5 秒的静音都被丢掉。"""
        result = pcm.analyze(self.path, min_silence_ms=600)
        self.assertEqual(result["silences"], [])
        self.assertEqual(result["silence_total"], 0.0)

    def test_short_silence_is_kept_when_threshold_allows(self):
        result = pcm.analyze(self.path, min_silence_ms=100)
        self.assertEqual(result["silence_count"], 2)

    def test_buckets_and_decimation(self):
        result = pcm.analyze(self.path, buckets=25, series_points=10)
        self.assertEqual(len(result["peaks"]), 25)
        self.assertLessEqual(len(result["rms_db"]), 10)
        self.assertLessEqual(len(result["zcr"]), 10)


class DecodeTest(PcmBase):
    """解码路径：不同位深 / 声道都应当解出同样的信号。"""

    def _analyze(self, **kwargs):
        path = self.write("v.wav", synth.build_wav(synth.STANDARD_SEGMENTS, **kwargs))
        return pcm.analyze(path)

    def test_8bit(self):
        result = self._analyze(bit_depth=8)
        self.assertEqual(len(result["silences"]), 2)
        self.assertClose(result["silences"][0]["end"], 0.5)

    def test_24bit(self):
        result = self._analyze(bit_depth=24)
        self.assertEqual(len(result["silences"]), 2)
        self.assertAlmostEqual(result["rms_db"][50], 20 * math.log10(0.5 / math.sqrt(2)),
                               delta=0.5)

    def test_32bit_int(self):
        result = self._analyze(bit_depth=32)
        self.assertEqual(len(result["silences"]), 2)
        self.assertClose(result["silences"][1]["start"], 1.5)

    def test_stereo_downmix_keeps_level(self):
        """两声道同相信号下混后，RMS 与单声道一致（除以声道数才是平均值）。"""
        result = self._analyze(channels=2)
        self.assertAlmostEqual(result["rms_db"][50], 20 * math.log10(0.5 / math.sqrt(2)),
                               delta=0.5)
        self.assertEqual(result["channels"], 2)

    def test_stereo_with_channel_gain(self):
        """左声道满幅、右声道一半：下混后 RMS 是两者的平均。"""
        path = self.write("g.wav", synth.build_wav([("tone", 0.2)], channels=2,
                                                   gains=[1.0, 0.5]))
        result = pcm.analyze(path)
        expected = 20 * math.log10((0.5 / math.sqrt(2) + 0.25 / math.sqrt(2)) / 2)
        self.assertAlmostEqual(result["rms_db"][5], expected, delta=0.6)

    def test_peaks_are_normalised(self):
        path = self.write("p.wav", synth.build_wav([("tone", 0.2)], amplitude=0.25))
        result = pcm.analyze(path)
        self.assertAlmostEqual(max(item["max"] for item in result["peaks"]), 0.25, delta=0.01)


class PcmErrorTest(PcmBase):
    def test_mp3_is_refused_with_readable_message(self):
        path = self.write("a.mp3", synth.build_mp3())
        with self.assertRaises(pcm.PcmError) as ctx:
            pcm.analyze(path)
        self.assertIn("只支持 WAV", str(ctx.exception))

    def test_empty_wav_is_refused(self):
        path = self.write("empty.wav", synth.build_wav([]))
        with self.assertRaises(pcm.PcmError) as ctx:
            pcm.analyze(path)
        self.assertIn("没有音频数据", str(ctx.exception))

    def test_compressed_wav_codec_is_refused(self):
        """把 fmt 块里的编码号改成 A-law(6)：应当报「解不了」而不是算出垃圾。"""
        data = bytearray(synth.build_wav([("tone", 0.1)]))
        offset = data.index(b"fmt ") + 8
        data[offset:offset + 2] = (6).to_bytes(2, "little")
        path = self.write("alaw.wav", bytes(data))
        with self.assertRaises(pcm.PcmError) as ctx:
            pcm.analyze(path)
        self.assertIn("A-law", str(ctx.exception))

    def test_supported_helper(self):
        self.assertTrue(pcm.supported("x.wav"))
        self.assertFalse(pcm.supported("x.mp3"))

    def test_detect_silence_pure_function(self):
        """直接对序列测：连续低于阈值的帧构成一段，短的丢弃。"""
        series = [-100.0, -100.0, -20.0, -100.0, -100.0, -100.0]
        runs = pcm.detect_silence(series, 0.1, -45.0, 300, 0.6)
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]["start"], 0.3)
        self.assertAlmostEqual(runs[0]["end"], 0.6)

    def test_frame_length_is_raised_for_very_long_input(self):
        """帧数上限生效时，响应里的 frame_ms 会大于请求值（并如实报出来）。"""
        path = self.write("long.wav", synth.build_wav([("tone", 4.0)]))
        result = pcm.analyze(path, frame_ms=20, max_frames=50)
        self.assertGreater(result["frame_ms"], 20)
        self.assertLessEqual(result["frames"], 50)
        self.assertEqual(result["requested_frame_ms"], 20.0)


class StreamingTest(PcmBase):
    def test_large_file_is_processed_in_chunks(self):
        """30 秒立体声（约 5 MB）能分析完，且结果与整段一致 —— 验证流式路径。"""
        path = self.write("big.wav", synth.build_wav(
            [("silence", 1.0), ("tone", 28.0), ("silence", 1.0)], channels=2))
        self.assertGreater(os.path.getsize(path), pcm.CHUNK_BYTES)
        seen = []
        result = pcm.analyze(path, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(len(result["silences"]), 2)
        self.assertClose(result["silences"][0]["end"], 1.0)
        self.assertGreater(len(seen), 1, "进度回调应当被多次调用（说明确实分块读了）")
        self.assertEqual(seen[-1][0], seen[-1][1])

    def test_throughput_is_measured(self):
        """把实测吞吐打出来（不是断言性能，只是让数字留在测试输出里可核对）。

        本机实测约 3~4 M 样本/秒（见 README 的实测数字）。
        """
        path = self.write("rate.wav", synth.build_wav([("tone", 20.0)], channels=2))
        started = time.time()
        result = pcm.analyze(path)
        elapsed = time.time() - started
        samples = result["total_samples"] * result["channels"]
        rate = samples / elapsed if elapsed else 0
        print("\n[实测] %d 万样本 / %.2f 秒 = %.2f M 样本/秒"
              % (samples // 10000, elapsed, rate / 1e6))
        self.assertGreater(rate, 500000, "吞吐低于 0.5 M 样本/秒基本不可用")

    def test_checkpoint_is_consulted(self):
        """checkpoint 抛异常时分析立刻中止（任务取消就是这样实现的）。"""
        path = self.write("cancel.wav", synth.build_wav([("tone", 30.0)], channels=2))
        calls = []

        def boom():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("canceled")

        with self.assertRaises(RuntimeError):
            pcm.analyze(path, checkpoint=boom)
        self.assertGreaterEqual(len(calls), 1)


class MetadataInteropTest(PcmBase):
    def test_read_wav_info_matches_audiometa(self):
        path = self.write("a.wav", synth.build_wav(synth.STANDARD_SEGMENTS, channels=2))
        info = pcm.read_wav_info(path)
        meta = audiometa.read_metadata(path)
        self.assertEqual(info["sample_rate"], meta["sample_rate"])
        self.assertEqual(info["channels"], meta["channels"])
        self.assertEqual(info["data_offset"], meta["data_offset"])
        self.assertEqual(info["data_size"], meta["data_size"])


if __name__ == "__main__":
    unittest.main()
