"""音频元数据解析用例（合成素材，逐字段断言）。

每个格式的样本都由 ``tests/synth.py`` 按字节构造，期望值全部是**手算出来的常数**
（比如 MP3 的时长 = 100 帧 × 1152 样本 / 44100 Hz = 2.6122… 秒），
所以这里验证的是「解析结果是否等于规范算出来的值」，不是「解析器自己是否自洽」。
"""

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from app import audiometa  # noqa: E402

from tests import synth  # noqa: E402


class TempFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh10-meta-")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path


class WavMetadataTest(TempFiles):
    def test_wav_16bit_mono(self):
        path = self.write("a.wav", synth.build_wav(synth.STANDARD_SEGMENTS))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["format"], "WAV")
        self.assertEqual(info["codec"], "PCM")
        self.assertEqual(info["codec_id"], 1)
        self.assertEqual(info["sample_rate"], 44100)
        self.assertEqual(info["channels"], 1)
        self.assertEqual(info["bit_depth"], 16)
        self.assertAlmostEqual(info["duration"], 2.0, places=6)
        self.assertEqual(info["total_samples"], 88200)
        self.assertEqual(info["bitrate"], 44100 * 1 * 16)   # 采样率 × 声道 × 位深
        self.assertTrue(info["lossless"])

    def test_wav_24bit_stereo(self):
        path = self.write("b.wav", synth.build_wav(synth.STANDARD_SEGMENTS, channels=2,
                                                   bit_depth=24, gains=[1.0, 0.5]))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["channels"], 2)
        self.assertEqual(info["bit_depth"], 24)
        self.assertAlmostEqual(info["duration"], 2.0, places=6)
        self.assertEqual(info["bitrate"], 44100 * 2 * 24)

    def test_wav_riff_info_tags(self):
        data = bytearray(synth.build_wav(synth.STANDARD_SEGMENTS))
        # 在文件末尾追加一个 LIST/INFO 块（解析器按块长度遍历，追加在后面也能读到）
        payload = b"INFO" + b"INAM" + struct.pack("<I", 6) + b"Title\x00"
        payload += b"IART" + struct.pack("<I", 7) + b"Artist\x00"
        chunk = b"LIST" + struct.pack("<I", len(payload)) + payload
        data += chunk
        # RIFF 头部的大小要跟着改，否则解析器会在 data 块之后停下
        struct.pack_into("<I", data, 4, len(data) - 8)
        path = self.write("c.wav", bytes(data))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["tags"].get("title"), "Title")
        self.assertEqual(info["tags"].get("artist"), "Artist")
        self.assertAlmostEqual(info["duration"], 2.0, places=6)

    def test_wav_with_empty_data_chunk_reports_zero(self):
        """空 WAV：元数据这条路**不报错**，如实给出 0 时长。

        硬报错留给真正要解码的波形分析（``pcm.read_wav_info`` 会拒绝）——
        元数据读取是很轻的操作，因为一个空文件就抛异常会连累批量扫描。
        """
        path = self.write("empty.wav", synth.build_wav([]))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["duration"], 0.0)
        self.assertEqual(info["data_size"], 0)

    def test_not_a_wav(self):
        path = self.write("fake.wav", b"NOTARIFF" + b"\x00" * 64)
        with self.assertRaises(audiometa.AudioMetaError):
            audiometa.read_metadata(path)


class FlacMetadataTest(TempFiles):
    def test_streaminfo_fields(self):
        path = self.write("a.flac", synth.build_flac(sample_rate=48000, channels=1,
                                                     bit_depth=24, total_samples=96000))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["format"], "FLAC")
        self.assertEqual(info["codec"], "FLAC")
        self.assertEqual(info["sample_rate"], 48000)
        self.assertEqual(info["channels"], 1)
        self.assertEqual(info["bit_depth"], 24)
        self.assertEqual(info["total_samples"], 96000)
        self.assertAlmostEqual(info["duration"], 2.0, places=6)   # 96000 / 48000
        self.assertTrue(info["lossless"])

    def test_vorbis_comment_tags(self):
        path = self.write("b.flac", synth.build_flac(
            tags={"TITLE": "合成标题", "ARTIST": "合成歌手", "ALBUM": "合成专辑"}))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["tags"]["title"], "合成标题")
        self.assertEqual(info["tags"]["artist"], "合成歌手")
        self.assertEqual(info["tags"]["album"], "合成专辑")

    def test_flac_without_tags_still_parses(self):
        path = self.write("c.flac", synth.build_flac())
        info = audiometa.read_metadata(path)
        self.assertEqual(info["tags"], {})
        self.assertAlmostEqual(info["duration"], 2.0, places=6)


class Mp3MetadataTest(TempFiles):
    def test_duration_from_xing_frame_count(self):
        """100 帧 × 1152 样本/帧 ÷ 44100 Hz = 2.6122448… 秒（手算值）。

        ``read_metadata`` 会把时长收敛到毫秒（2.612），所以这里按 3 位小数比 ——
        差的 0.00024 秒是「四舍五入到毫秒」本身，不是解析误差。
        """
        path = self.write("a.mp3", synth.build_mp3(frames=100))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["format"], "MP3")
        self.assertEqual(info["codec"], "MPEG Layer III")
        self.assertFalse(info["duration_estimated"], "有 Xing 头时不该标成估算")
        self.assertAlmostEqual(info["duration"], 100 * 1152 / 44100.0, places=3)
        self.assertEqual(info["duration"], 2.612)

    def test_id3v2_text_tags(self):
        path = self.write("b.mp3", synth.build_mp3(title="标题", artist="艺术家"))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["tags"]["title"], "标题")
        self.assertEqual(info["tags"]["artist"], "艺术家")
        self.assertEqual(info["tags"]["id3_version"], "2.3")

    def test_id3v24_synchsafe_frame_size(self):
        path = self.write("c.mp3", synth.build_mp3(title="V4 标题", version=4))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["tags"]["title"], "V4 标题")
        self.assertEqual(info["tags"]["id3_version"], "2.4")

    def test_sample_rate_and_channels_from_frame_header(self):
        path = self.write("d.mp3", synth.build_mp3(channel_mode=3, rate_index=1))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["sample_rate"], 48000)
        self.assertEqual(info["channels"], 1)

    def test_unsigned_byte_is_not_a_frame(self):
        path = self.write("bad.mp3", b"\x00" * 4096)
        with self.assertRaises(audiometa.AudioMetaError):
            audiometa.read_metadata(path)


class OggMetadataTest(TempFiles):
    def test_vorbis_identification_and_granule(self):
        path = self.write("a.ogg", synth.build_ogg(sample_rate=44100, channels=2,
                                                   total_samples=88200))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["format"], "OGG")
        self.assertEqual(info["codec"], "Vorbis")
        self.assertEqual(info["sample_rate"], 44100)
        self.assertEqual(info["channels"], 2)
        self.assertAlmostEqual(info["duration"], 2.0, places=6)   # 88200 / 44100

    def test_vorbis_comment_tags(self):
        path = self.write("b.ogg", synth.build_ogg(tags={"TITLE": "合成标题",
                                                         "ARTIST": "合成歌手"}))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["tags"]["title"], "合成标题")
        self.assertEqual(info["tags"]["artist"], "合成歌手")

    def test_opus_uses_48k_granule(self):
        """Opus 的 granule 一律按 48 kHz 计，识别头里的 input rate 不能拿来算时长。

        样本里 input_sample_rate=44100、granule=96000：
        按 48 kHz 算 = 2.0 秒（正确）；按 44100 算 = 2.177 秒（错误）。
        """
        path = self.write("c.opus", synth.build_opus(input_sample_rate=44100,
                                                     granule=96000))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["codec"], "Opus")
        self.assertEqual(info["sample_rate"], 48000)
        self.assertEqual(info["input_sample_rate"], 44100)
        self.assertAlmostEqual(info["duration"], 2.0, places=6)

    def test_not_an_ogg(self):
        path = self.write("bad.ogg", b"NOTOGGS" + b"\x00" * 128)
        with self.assertRaises(audiometa.AudioMetaError):
            audiometa.read_metadata(path)


class M4aMetadataTest(TempFiles):
    def test_moov_fields(self):
        path = self.write("a.m4a", synth.build_m4a(sample_rate=44100, channels=2,
                                                   bit_depth=16, duration=2.0))
        info = audiometa.read_metadata(path)
        self.assertEqual(info["format"], "M4A")
        self.assertEqual(info["codec"], "mp4a")
        self.assertEqual(info["sample_rate"], 44100)
        self.assertEqual(info["channels"], 2)
        self.assertEqual(info["bit_depth"], 16)
        self.assertAlmostEqual(info["duration"], 2.0, places=6)
        self.assertEqual(info["track_count"], 1)
        self.assertEqual(info["audio_tracks"][0]["kind"], "audio")
        self.assertEqual(info["audio_tracks"][0]["language"], "eng")

    def test_duration_scales_with_timescale(self):
        """时长 = mdhd.duration / mdhd.timescale —— 换个 timescale 数值必须跟着变。"""
        path = self.write("b.m4a", synth.build_m4a(duration=3.5, timescale=48000))
        info = audiometa.read_metadata(path)
        self.assertAlmostEqual(info["duration"], 3.5, places=6)

    def test_missing_moov(self):
        data = synth.build_m4a()
        head = data[:data.index(b"moov") - 4]
        path = self.write("c.m4a", head + b"free" + struct.pack(">I", 8))
        with self.assertRaises(audiometa.AudioMetaError):
            audiometa.read_metadata(path)


class DispatchTest(TempFiles):
    def test_unsupported_extension(self):
        path = self.write("a.txt", b"hello")
        with self.assertRaises(audiometa.UnsupportedFormat):
            audiometa.read_metadata(path)

    def test_missing_file(self):
        with self.assertRaises(audiometa.AudioMetaError):
            audiometa.read_metadata(os.path.join(self.tmp, "nope.wav"))

    def test_ext_sets(self):
        self.assertIn(".wav", audiometa.DECODABLE_EXTS)
        self.assertNotIn(".mp3", audiometa.DECODABLE_EXTS)
        self.assertIn(".m4a", audiometa.AUDIO_EXTS)
        for ext in (".lrc", ".srt", ".vtt", ".txt"):
            self.assertIn(ext, audiometa.SUBTITLE_EXTS)


if __name__ == "__main__":
    unittest.main()
