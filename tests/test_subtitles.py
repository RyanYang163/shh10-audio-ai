"""字幕互转用例。

重点是三条：

1. **往返不变式**：LRC → SRT → LRC 之后，逐条 cue 的时间戳与文本必须一致
   （LRC 精度 10 ms、SRT 精度 1 ms，这个方向无损）。
   同时明确写出**不变式管不到的地方**：一行多时间戳会被展开成多行，
   ``[ti:]`` 这类标签 SRT 没地方放会丢。
2. **容错**：时间戳的各种写法、SRT 的逗号与点号混用、缺序号、CRLF、GB18030
   编码、VTT 的 NOTE 块与 cue 设置，都要能解析而不是报错。
3. **坏输入给可读错误**：不是抛异常就完事，错误信息要能指导用户下一步。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from app import subtitles  # noqa: E402

from tests import synth  # noqa: E402


class TimeParsingTest(unittest.TestCase):
    def test_lrc_variants(self):
        self.assertAlmostEqual(subtitles.parse_lrc_clock("00:12"), 12.0)
        self.assertAlmostEqual(subtitles.parse_lrc_clock("00:12.3"), 12.3)      # 十分之一秒
        self.assertAlmostEqual(subtitles.parse_lrc_clock("00:12.34"), 12.34)    # 厘秒
        self.assertAlmostEqual(subtitles.parse_lrc_clock("00:12.345"), 12.345)  # 毫秒
        self.assertAlmostEqual(subtitles.parse_lrc_clock("00:12:34"), 12.34)    # 老式 mm:ss:cc
        self.assertAlmostEqual(subtitles.parse_lrc_clock("75:30.00"), 4530.0)   # 分钟可超 59
        self.assertIsNone(subtitles.parse_lrc_clock("abc"))

    def test_srt_and_vtt_variants(self):
        self.assertAlmostEqual(subtitles.parse_clock("00:00:01,234"), 1.234)
        self.assertAlmostEqual(subtitles.parse_clock("00:00:01.234"), 1.234)
        self.assertAlmostEqual(subtitles.parse_clock("00:12:34,500"), 754.5)
        self.assertAlmostEqual(subtitles.parse_clock("02:03"), 123.0)           # MM:SS
        self.assertAlmostEqual(subtitles.parse_clock("02:03.5"), 123.5)
        self.assertIsNone(subtitles.parse_clock("99:99:99:99"))

    def test_lrc_three_groups_is_not_hours(self):
        """LRC 的 [01:23:45] 是 1 分 23 秒 45 厘秒，不是 1 小时 23 分 45 秒。"""
        self.assertAlmostEqual(subtitles.parse_lrc_clock("01:23:45"), 83.45)
        self.assertNotAlmostEqual(subtitles.parse_lrc_clock("01:23:45"), 5025.0)

    def test_formatters(self):
        self.assertEqual(subtitles.format_srt_time(1.234), "00:00:01,234")
        self.assertEqual(subtitles.format_vtt_time(1.234), "00:00:01.234")
        self.assertEqual(subtitles.format_lrc_time(12.34), "00:12.34")
        self.assertEqual(subtitles.format_lrc_time(3723.99), "62:03.99")
        # 四舍五入方向：2.345 秒 → 2.35 秒（不是银行家舍入）
        self.assertEqual(subtitles.format_lrc_time(2.345), "00:02.35")
        self.assertEqual(subtitles.format_srt_time(-5), "00:00:00,000")   # 负数夹到 0


class LrcParseTest(unittest.TestCase):
    def test_parse_metadata_and_cues(self):
        parsed = subtitles.parse(synth.LRC_SAMPLE)
        self.assertEqual(parsed["format"], "lrc")
        self.assertEqual(parsed["meta"]["ti"], "合成测试歌词")
        self.assertEqual(parsed["meta"]["ar"], "合成歌手")
        self.assertEqual(len(parsed["cues"]), 5)      # 副歌一行两个时间戳 → 两条
        starts = [cue["start"] for cue in parsed["cues"]]
        self.assertEqual(starts, [0.0, 12.34, 20.5, 40.25, 62.99])
        self.assertEqual(parsed["cues"][0]["text"], "第一行歌词")
        self.assertEqual(parsed["cues"][2]["text"], "重复的副歌")

    def test_end_time_is_next_start(self):
        parsed = subtitles.parse(synth.LRC_SAMPLE)
        self.assertAlmostEqual(parsed["cues"][0]["end"], 12.34)
        # 最后一行没有下一行，给一个默认尾巴（2 秒）
        self.assertAlmostEqual(parsed["cues"][-1]["end"], 62.99 + 2.0)

    def test_offset_tag_is_reported_not_applied(self):
        """``[offset:-500]`` 只报告、不自动应用 —— 各播放器对符号约定不一致。"""
        parsed = subtitles.parse("[offset:-500]\n[00:01.00]abc\n")
        self.assertEqual(parsed["meta"]["offset_ms"], -500)
        self.assertAlmostEqual(parsed["cues"][0]["start"], 1.0)

    def test_untimed_lines_are_skipped_not_fatal(self):
        parsed = subtitles.parse("这是一句说明\n[00:01.00]真正的歌词\n")
        self.assertEqual(len(parsed["cues"]), 1)

    def test_no_timestamp_raises_readable_error(self):
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            subtitles.parse("这不是歌词文件\n随便写点东西\n", "lrc")
        self.assertIn("时间戳", str(ctx.exception))


class SrtVttParseTest(unittest.TestCase):
    def test_srt_basic(self):
        parsed = subtitles.parse(synth.SRT_SAMPLE)
        self.assertEqual(parsed["format"], "srt")
        self.assertEqual(len(parsed["cues"]), 2)
        self.assertAlmostEqual(parsed["cues"][0]["start"], 0.0)
        self.assertAlmostEqual(parsed["cues"][0]["end"], 2.5)
        self.assertEqual(parsed["cues"][1]["text"], "第二行字幕\n带两行文本")

    def test_srt_tolerates_mixed_separators_and_missing_index(self):
        text = ("00:00:01.500 --> 00:00:03,000\n甲\n\n"
                "7\n00:00:03,000 --> 00:00:04.250\n乙\n")
        parsed = subtitles.parse(text)
        self.assertEqual(parsed["format"], "srt")
        self.assertEqual(len(parsed["cues"]), 2)
        self.assertAlmostEqual(parsed["cues"][0]["start"], 1.5)
        self.assertAlmostEqual(parsed["cues"][1]["end"], 4.25)

    def test_srt_tolerates_crlf_and_blank_lines(self):
        text = synth.SRT_SAMPLE.replace("\n", "\r\n") + "\r\n\r\n"
        parsed = subtitles.parse(text)
        self.assertEqual(len(parsed["cues"]), 2)

    def test_srt_tolerates_cue_settings(self):
        text = "1\n00:00:01,000 --> 00:00:02,000 X1:100 X2:200 Y1:1 Y2:2\n丙\n"
        parsed = subtitles.parse(text)
        self.assertEqual(parsed["cues"][0]["text"], "丙")

    def test_vtt_skips_note_and_settings(self):
        parsed = subtitles.parse(synth.VTT_SAMPLE)
        self.assertEqual(parsed["format"], "vtt")
        self.assertEqual(len(parsed["cues"]), 2)
        self.assertEqual(parsed["cues"][0]["text"], "First line")
        self.assertAlmostEqual(parsed["cues"][1]["start"], 2.5)   # MM:SS.mmm 形式

    def test_vtt_style_block_skipped(self):
        text = ("WEBVTT\n\nSTYLE\n::cue { color: red }\n\n"
                "00:00:01.000 --> 00:00:02.000\n丁\n")
        parsed = subtitles.parse(text)
        self.assertEqual(len(parsed["cues"]), 1)

    def test_garbage_with_arrow_raises(self):
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            subtitles.parse("abc --> def\n内容\n", "srt")
        self.assertIn("时间轴", str(ctx.exception))

    def test_format_detection_prefers_content_over_extension(self):
        self.assertEqual(subtitles.detect_format(synth.SRT_SAMPLE, "x.lrc"), "srt")
        self.assertEqual(subtitles.detect_format(synth.VTT_SAMPLE, "x.srt"), "vtt")
        self.assertEqual(subtitles.detect_format(synth.LRC_SAMPLE, "x.txt"), "lrc")
        self.assertEqual(subtitles.detect_format("纯文本\n", "x.txt"), "txt")


class EncodingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh10-sub-")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _write(self, name, raw):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    def test_utf8_bom(self):
        path = self._write("a.lrc", b"\xef\xbb\xbf" + synth.LRC_SAMPLE.encode("utf-8"))
        parsed = subtitles.parse_file(path)
        self.assertEqual(parsed["encoding"], "utf-8-sig")
        self.assertEqual(parsed["cues"][0]["text"], "第一行歌词")

    def test_gb18030(self):
        path = self._write("b.srt", synth.SRT_SAMPLE.encode("gb18030"))
        parsed = subtitles.parse_file(path)
        self.assertEqual(parsed["encoding"], "gb18030")
        self.assertEqual(parsed["cues"][1]["text"], "第二行字幕\n带两行文本")

    def test_utf16(self):
        path = self._write("c.lrc", synth.LRC_SAMPLE.encode("utf-16"))
        parsed = subtitles.parse_file(path)
        self.assertEqual(parsed["encoding"], "utf-16")
        self.assertEqual(parsed["cues"][0]["text"], "第一行歌词")

    def test_crlf_file(self):
        path = self._write("d.vtt", synth.VTT_SAMPLE.replace("\n", "\r\n").encode("utf-8"))
        parsed = subtitles.parse_file(path)
        self.assertEqual(len(parsed["cues"]), 2)


class RoundTripTest(unittest.TestCase):
    """往返不变式 —— 这是本模块最该被守住的东西。"""

    def test_lrc_srt_lrc_preserves_timestamps_and_text(self):
        srt, stats = subtitles.convert_text(synth.LRC_SAMPLE, "srt")
        self.assertEqual(stats["cues_in"], 5)
        self.assertEqual(stats["cues_out"], 5)
        back, _ = subtitles.convert_text(srt, "lrc")
        original = subtitles.parse(synth.LRC_SAMPLE)
        again = subtitles.parse(back)
        self.assertEqual(len(again["cues"]), len(original["cues"]))
        for before, after in zip(original["cues"], again["cues"]):
            self.assertAlmostEqual(before["start"], after["start"], places=3,
                                   msg="时间戳必须逐一还原")
            self.assertEqual(before["text"], after["text"], "文本必须逐一还原")

    def test_lrc_srt_lrc_is_byte_stable_on_second_pass(self):
        """第二次往返必须逐字节稳定 —— 说明转换是幂等方式收敛的，不会来回漂。"""
        first, _ = subtitles.convert_text(synth.LRC_SAMPLE, "srt")
        back, _ = subtitles.convert_text(first, "lrc")
        again, _ = subtitles.convert_text(back, "srt")
        self.assertEqual(first, again)

    def test_multi_timestamp_line_expands(self):
        """不变式管不到的地方，明写出来：一行多时间戳会被展开成多行。"""
        srt, _ = subtitles.convert_text("[00:01.00][00:09.00]副歌\n", "srt")
        self.assertEqual(srt.count("副歌"), 2)
        self.assertIn("00:00:01,000 --> ", srt)
        self.assertIn("00:00:09,000 --> ", srt)

    def test_metadata_is_lost_through_srt(self):
        """SRT 没有放标签的地方：``[ti:]`` 过一趟 SRT 就没了（这是格式所限）。"""
        meta = subtitles.parse(synth.LRC_SAMPLE)["meta"]
        srt, _ = subtitles.convert_text(synth.LRC_SAMPLE, "srt")
        self.assertNotIn("合成测试歌词", srt)
        back, _ = subtitles.convert_text(srt, "lrc")
        self.assertNotIn("[ti:", back)
        self.assertEqual(meta["ti"], "合成测试歌词")

    def test_srt_vtt_round_trip_keeps_milliseconds(self):
        vtt, _ = subtitles.convert_text(synth.SRT_SAMPLE, "vtt")
        self.assertIn("00:00:02.500 --> 00:00:05.000", vtt)
        back, _ = subtitles.convert_text(vtt, "srt")
        self.assertIn("00:00:02,500 --> 00:00:05,000", back)

    def test_all_four_formats_are_reachable_from_each_other(self):
        for source, text in (("lrc", synth.LRC_SAMPLE), ("srt", synth.SRT_SAMPLE),
                             ("vtt", synth.VTT_SAMPLE), ("txt", "一行\n二行\n")):
            for target in subtitles.FORMATS:
                output, stats = subtitles.convert_text(text, target, source_format=source)
                self.assertTrue(output.strip(), "%s → %s 输出为空" % (source, target))
                self.assertEqual(stats["target_format"], target)


class TransformTest(unittest.TestCase):
    def test_merge_short_joins_one_second_lines(self):
        """「每 1 秒一行」的碎句在歌词模式下应当并成一条整句。"""
        parsed = subtitles.parse(synth.LRC_FRAGMENTED)
        merged, count = subtitles.merge_short_cues(parsed["cues"], 1.5)
        self.assertEqual(count, 4, "5 行并成 1 行")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "今天天气很好我想出去走走")
        # 合并后的时间轴取**首行起点到末行终点**
        self.assertAlmostEqual(merged[0]["start"], 0.0)
        self.assertAlmostEqual(merged[0]["end"], 6.0)

    def test_merge_short_keeps_long_lines(self):
        parsed = subtitles.parse(synth.LRC_SAMPLE)
        merged, count = subtitles.merge_short_cues(parsed["cues"], 1.5)
        self.assertEqual(count, 0, "每行都长于 1.5 秒，不该合并")
        self.assertEqual(len(merged), 5)

    def test_merge_respects_max_gap(self):
        """间隔超过 max_gap 的不合并 —— 否则一句歌词会挂几十秒。"""
        text = "[00:00.00]短句一\n[00:00.50]短句二\n[00:30.00]短句三\n"
        parsed = subtitles.parse(text)
        merged, count = subtitles.merge_short_cues(parsed["cues"], 1.5, max_gap=1.5)
        self.assertEqual(count, 1, "只合并前两句，第三句隔着 30 秒不能并进来")
        self.assertEqual(len(merged), 2)
        self.assertAlmostEqual(merged[0]["end"], 30.0)

    def test_merge_joins_english_with_space(self):
        text = "[00:00.00]hello\n[00:00.50]world\n"
        merged, _ = subtitles.convert_text(text, "lrc", merge_short=1.5)
        self.assertIn("hello world", merged)

    def test_lyric_mode_default(self):
        """mode=lyric 时后端会带上默认阈值 1.5 秒（见 main.py 的 convert 任务）。"""
        self.assertAlmostEqual(subtitles.LYRIC_MERGE_SECONDS, 1.5)

    def test_offset_positive_and_negative(self):
        parsed = subtitles.parse(synth.LRC_SAMPLE)
        shifted, clamped = subtitles.shift_cues(parsed["cues"], 2.5)
        self.assertFalse(clamped)
        self.assertAlmostEqual(shifted[0]["start"], 2.5)
        self.assertAlmostEqual(shifted[1]["start"], 14.84)

        shifted, clamped = subtitles.shift_cues(parsed["cues"], -2.5)
        self.assertTrue(clamped, "第一行会变成负时间 → 必须报告被夹住")
        self.assertAlmostEqual(shifted[0]["start"], 0.0)
        self.assertAlmostEqual(shifted[1]["start"], 12.34 - 2.5)
        # 夹取语义：起点被压到 0，**终点照常平移**，所以这一条的可见时长会变短
        # （落在 0 之前的那 2.5 秒无法表示），而不是被拉长成「保持原时长」。
        self.assertAlmostEqual(shifted[0]["end"], 12.34 - 2.5)
        self.assertLess(shifted[0]["end"] - shifted[0]["start"],
                        parsed["cues"][0]["end"] - parsed["cues"][0]["start"])

    def test_offset_renders_correctly(self):
        output, stats = subtitles.convert_text(synth.LRC_SAMPLE, "lrc", offset=-2.5)
        self.assertTrue(stats["clamped"])
        self.assertIn("[00:09.84]第二行歌词", output)
        self.assertIn("[00:00.00]第一行歌词", output)

    def test_untimed_txt_is_laid_out_for_srt(self):
        output, stats = subtitles.convert_text("第一句\n第二句\n第三句\n", "srt", source_format="txt")
        self.assertTrue(stats.get("laid_out"))
        self.assertIn("00:00:00,000 --> 00:00:03,000", output)
        self.assertIn("00:00:03,000 --> 00:00:06,000", output)
        self.assertIn("00:00:06,000 --> 00:00:09,000", output)

    def test_line_duration_is_configurable(self):
        output, _ = subtitles.convert_text("甲\n乙\n", "srt", source_format="txt",
                                           line_seconds=1.0)
        self.assertIn("00:00:00,000 --> 00:00:01,000", output)

    def test_txt_output_strips_timestamps(self):
        output, _ = subtitles.convert_text(synth.SRT_SAMPLE, "txt")
        self.assertNotIn("-->", output)
        self.assertIn("First subtitle line", output)


class ConvertFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh10-conv-")
        cls.out = os.path.join(cls.tmp, "out")
        os.makedirs(cls.out)
        cls.source = os.path.join(cls.tmp, "song.lrc")
        with open(cls.source, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(synth.LRC_SAMPLE)

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_convert_file_writes_utf8_lf(self):
        record = subtitles.convert_file(self.source, self.out, "srt")
        self.assertTrue(record["ok"])
        self.assertEqual(record["source_format"], "lrc")
        self.assertEqual(record["target_format"], "srt")
        self.assertEqual(record["stats"]["cues_out"], 5)
        self.assertTrue(os.path.exists(record["output"]))
        with open(record["output"], "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r\n", raw, "输出必须是 LF 行尾")
        self.assertNotIn(b"\xef\xbb\xbf", raw, "输出不能带 BOM")
        self.assertIn("第一行歌词".encode("utf-8"), raw)

    def test_never_overwrites_existing_output(self):
        first = subtitles.convert_file(self.source, self.out, "lrc")
        second = subtitles.convert_file(self.source, self.out, "lrc")
        self.assertNotEqual(first["output"], second["output"])
        self.assertTrue(second["output"].endswith("-1.lrc"))
        self.assertTrue(os.path.exists(first["output"]))

    def test_unique_path_limit(self):
        path = subtitles.unique_path(self.out, "unique.srt")
        self.assertTrue(path.endswith("unique.srt"))

    def test_summarize_limits_and_truncates(self):
        parsed = subtitles.parse(synth.LRC_SAMPLE)
        preview = subtitles.summarize(parsed["cues"], 2)
        self.assertEqual(len(preview), 2)
        self.assertEqual(preview[0]["text"], "第一行歌词")


if __name__ == "__main__":
    unittest.main()
