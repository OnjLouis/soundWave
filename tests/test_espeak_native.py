"""Opt-in native eSpeak tests against disposable runtime copies."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import wave

from test_espeak_process import SOURCE

APP = os.environ.get('SOUNDWAVE_ESPEAK_TEST_NVDA')
TEMP = os.environ.get('SOUNDWAVE_TEST_TEMP')


@unittest.skipUnless(APP and TEMP, 'isolated native eSpeak fixture not configured')
class NativeEspeakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path(APP).resolve().is_relative_to(Path(TEMP).resolve()):
            raise RuntimeError('Native tests require a runtime copy under the disposable test root')
        package = types.ModuleType('soundWave_lib')
        package.__path__ = [str(SOURCE.parents[1])]
        package.runtime = types.SimpleNamespace(bind=lambda namespace: namespace.update(_=lambda value: value))
        with mock.patch.dict('sys.modules', {'soundWave_lib': package}):
            spec = importlib.util.spec_from_file_location('native_espeak_test', SOURCE)
            cls.client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.client)
        cls.metadata = cls.client.run_job(APP, temp_root=TEMP)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'output.wav'

    def render(self, options=None, text='One. This is an independent speech test. Finished.', **kwargs):
        result = self.client.run_job(APP, text=text, out_wav=self.output,
            options={'voice': 'en', 'variant': 'max', **(options or {})},
            temp_root=self.temp.name, timeout=30, **kwargs)
        with wave.open(str(self.output)) as output:
            self.assertEqual((output.getnchannels(), output.getsampwidth()), (1, 2))
            data = output.readframes(output.getnframes())
            self.assertEqual(result['bytes'], len(data))
            self.assertEqual(result['pcm_rate'], output.getframerate())
        return data

    def test_voice_and_variant_discovery(self):
        self.assertGreater(len(self.metadata['voices']), 100)
        self.assertIn('en', {item['id'] for item in self.metadata['voices']})
        self.assertIn('max', {item['id'] for item in self.metadata['variants']})

    def test_multilingual_voice_identifiers_render(self):
        for language, text in (('en', 'A useful voice.'), ('fr', 'Une voix utile.'),
                               ('de', 'Eine hilfreiche Stimme.'), ('es', 'Una voz útil.'),
                               ('pt', 'Uma voz útil.')):
            voice = next(item for item in self.metadata['voices'] if item['language'].startswith(language))
            with self.subTest(language=language, voice=voice['id']):
                self.assertTrue(any(self.render({'voice': voice['id']}, text=text)))

    def test_rate_boost_reduces_audio_length_and_volume_zero_is_silent(self):
        normal = self.render({'rate': 50})
        boosted = self.render({'rate': 50, 'rateBoost': True})
        self.assertLess(len(boosted), len(normal) * 0.6)
        muted = self.render({'volume': 0})
        self.assertFalse(any(muted))

    def test_pitch_zero_and_100_and_variant_change_audio(self):
        low = self.render({'pitch': 0})
        high = self.render({'pitch': 100})
        other = self.render({'pitch': 100, 'variant': 'none'})
        self.assertNotEqual(low, high)
        self.assertNotEqual(high, other)

    def test_preview_and_render_are_sample_identical(self):
        first = self.render({'rate': 65, 'pitch': 55, 'volume': 70})
        self.assertEqual(first, self.render({'rate': 65, 'pitch': 55, 'volume': 70}))

    def test_foreign_synth_markup_is_read_as_text_not_interpreted(self):
        self.assertTrue(any(self.render(text='One. <voice name="not-a-voice">test</voice> & finished.')))

    def test_bad_voice_fails_without_partial_output(self):
        with self.assertRaisesRegex(RuntimeError, 'voice'):
            self.render({'voice': 'nonexistent-fixture-voice'})
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_cancellation_reaps_worker_and_cleans_job_text_and_audio(self):
        cancel = threading.Event()
        timer = threading.Timer(0.25, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
                self.render(text='An intentionally lengthy cancellation test. ' * 20000, cancel_evt=cancel)
        finally:
            timer.cancel()
            timer.join()
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_repeated_short_utterances_leave_no_workers_or_job_directories(self):
        for text in ('a', 'space', '1.', 'finished') * 3:
            self.assertTrue(self.render(text=text))
        self.assertEqual([item.name for item in Path(self.temp.name).iterdir()], ['output.wav'])


if __name__ == '__main__':
    unittest.main()
