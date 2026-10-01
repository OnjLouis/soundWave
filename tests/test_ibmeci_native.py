"""Opt-in capture tests: point environment variables at a disposable engine copy."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave


SOURCE = Path(__file__).resolve().parents[1] / "source/soundWave_lib/synths/ibmeci_process.py"
DLL = os.environ.get("SOUNDWAVE_ECI_TEST_DLL")
APP_DIR = os.environ.get("SOUNDWAVE_ECI_TEST_NVDA")
TEMP = os.environ.get("SOUNDWAVE_TEST_TEMP")


@unittest.skipUnless(DLL and APP_DIR and TEMP, "isolated native ECI fixture not configured")
class NativeEciTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path(DLL).resolve().is_relative_to(Path(TEMP).resolve()):
            raise RuntimeError("Native tests require an engine copy under the disposable test root")
        spec = importlib.util.spec_from_file_location("native_eci_process", SOURCE)
        cls.process = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.process)
        cls.metadata = cls.process.run_job(DLL, APP_DIR, timeout=30, temp_root=TEMP)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "capture.wav"

    def render(self, options, text="This is a voice test. One, two, three. The final word is finished.", **kwargs):
        return self.process.run_job(DLL, APP_DIR, options=options, text=text, out_wav=self.output,
                                    timeout=30, temp_root=self.temp.name, **kwargs)

    def test_reported_languages_are_renderable_without_filename_guessing(self):
        self.assertIn(65537, {entry["id"] for entry in self.metadata["languages"]})
        for language in self.metadata["languages"]:
            with self.subTest(language=language["label"]):
                result = self.render({"voiceId": language["id"], "sampleRate": language["sampleRates"][0]})
                self.assertGreater(result["bytes"], 0)

    def test_every_offered_rate_and_preset_produces_valid_pcm(self):
        language = next(item for item in self.metadata["languages"] if item["id"] == 65537)
        signatures = set()
        for rate in language["sampleRates"]:
            for variant in language["profiles"][str(rate)]:
                options = {"voiceId": language["id"], "sampleRate": rate, "variant": variant["id"], **variant["defaults"]}
                result = self.render(options)
                with wave.open(str(self.output)) as stream:
                    self.assertEqual((1, 2, (8000, 11025, 22050)[rate]),
                                     (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()))
                    self.assertEqual(result["bytes"], stream.getnframes() * 2)
                    self.assertGreater(stream.getnframes(), stream.getframerate())
                    signatures.add(hashlib.sha256(stream.readframes(stream.getnframes())).hexdigest())
        self.assertGreater(len(signatures), 2)

    def test_preview_and_render_are_identical(self):
        options = {"voiceId": 65537, "sampleRate": 1, "variant": 2, "speed": 110,
                   "pitch": 65, "inflection": 40, "headSize": 55, "volume": 80,
                   "roughness": 0, "breathiness": 0}
        self.render(options)
        first = self.output.read_bytes()
        self.render(options)
        self.assertEqual(first, self.output.read_bytes())

    def test_rate_changes_pcm_sample_count_not_just_the_wav_header(self):
        measurements = []
        for rate in (0, 1):
            self.render({"voiceId": 65537, "sampleRate": rate, "variant": 1, "speed": 110})
            with wave.open(str(self.output)) as stream:
                measurements.append((stream.getnframes(), stream.getnframes() / stream.getframerate()))
        self.assertGreater(measurements[1][0], measurements[0][0] * 1.3)
        self.assertAlmostEqual(measurements[0][1], measurements[1][1], delta=0.1)

    def test_cancel_kills_only_the_private_worker_and_removes_partial_output(self):
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "Cancelled"):
                self.render({"voiceId": 65537, "sampleRate": 1}, text="A lengthy cancellation test. " * 50000, cancel_evt=cancel)
        finally:
            timer.cancel()
            timer.join()
        self.assertLess(time.monotonic() - start, 6)
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(Path(self.temp.name).iterdir()))

    def test_failed_job_removes_output_and_staging(self):
        with self.assertRaisesRegex(RuntimeError, "sample rate"):
            self.render({"voiceId": 65537, "sampleRate": 99})
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(Path(self.temp.name).iterdir()))


if __name__ == "__main__":
    unittest.main()
