"""Independent eSpeak workers without initializing NVDA's live singleton."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'source/soundWave_lib/synths/espeak.py'


class EspeakProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = types.SimpleNamespace(bind=lambda namespace: namespace.update(_=lambda value: value))
        package = types.ModuleType('soundWave_lib')
        package.__path__ = [str(SOURCE.parents[1])]
        package.runtime = runtime
        with mock.patch.dict('sys.modules', {'soundWave_lib': package}):
            spec = importlib.util.spec_from_file_location('espeak_process_test', SOURCE)
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('SOUNDWAVE_TEST_TEMP'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'runtime'
        self.app.mkdir()
        self.output = self.root / 'test.wav'
        self.pe(self.app / 'python313.dll', 0x8664)
        self.pe(self.app / 'synthDrivers/espeak.dll', 0x8664)
        (self.app / 'library.zip').write_bytes(b'fixture')

    def pe(self, path, machine):
        data = bytearray(128)
        data[:2] = b'MZ'
        data[0x3c:0x40] = struct.pack('<I', 64)
        data[64:70] = b'PE\0\0' + struct.pack('<H', machine)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def run_job(self, **kwargs):
        return self.module.run_job(self.app, text='Private fixture', out_wav=self.output,
                                   temp_root=self.root, **kwargs)

    def test_exact_synth_aliases_only(self):
        for name in ('espeak', 'eSpeak NG', 'espeak-ng'):
            self.assertTrue(self.module.is_espeak_synth(name))
        for name in ('other-espeak', 'phononLocal', ''):
            self.assertFalse(self.module.is_espeak_synth(name))

    def test_options_facades_are_metadata_only_and_do_not_share_mutable_settings(self):
        metadata = {'voices': [{'id': 'fr', 'name': 'French', 'language': 'fr'}],
                    'variants': [{'id': 'max', 'name': 'Max'}], 'voice': 'fr'}
        with mock.patch.object(self.module, '_app_dir', return_value=str(self.app)), \
             mock.patch.object(self.module, '_ui_language', return_value='fr'), \
             mock.patch.object(self.module, '_probe', return_value=metadata) as probe:
            first = self.module.create_options_facade()
            first.voice = 'changed'
            first.rate = 100
            first.terminate()
            second = self.module.create_options_facade()
            self.assertEqual((second.voice, second.rate), ('fr', 30))
            self.assertEqual(second.supportedSettings[0].id, 'rateBoost')
            probe.assert_called_with(str(self.app), 'fr')

    def test_matching_runtime_and_launcher_for_each_architecture(self):
        for machine, suffix in ((0x8664, '64'), (0x14c, '32')):
            self.pe(self.app / 'python313.dll', machine)
            self.pe(self.app / 'synthDrivers/espeak.dll', machine)
            runtime, launcher = self.module.runtime_and_launcher(self.app)
            self.assertEqual(runtime, self.app)
            self.assertEqual(launcher.name, 'soundWavePython' + suffix + '.exe')

    def test_wrong_architecture_does_not_launch(self):
        self.pe(self.app / 'synthDrivers/espeak.dll', 0x14c)
        with mock.patch.object(self.module.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'architecture'):
                self.run_job()
            launch.assert_not_called()

    def test_pre_cancelled_job_never_starts_or_creates_scratch(self):
        cancel = threading.Event()
        cancel.set()
        before = set(self.root.iterdir())
        with mock.patch.object(self.module.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
                self.run_job(cancel_evt=cancel)
            launch.assert_not_called()
        self.assertEqual(before, set(self.root.iterdir()))

    def test_success_uses_private_runtime_and_reaps_before_cleanup(self):
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        def launch(args, **kwargs):
            self.assertEqual(args[2], '-S')
            self.assertEqual(kwargs['creationflags'], self.module.subprocess.CREATE_NO_WINDOW)
            self.assertEqual(kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
            self.assertEqual(kwargs['env']['PYTHONHOME'], str(self.app))
            job = json.loads(Path(args[-1]).read_text())
            self.assertEqual(job['text'], 'Private fixture')
            self.assertEqual(job['parentPid'], os.getpid())
            Path(job['outputPath']).write_bytes(b'audio')
            Path(job['resultPath']).write_text(json.dumps({'ok': True, 'bytes': 5}))
            return process
        with mock.patch.object(self.module.subprocess, 'Popen', side_effect=launch):
            self.assertEqual(self.run_job()['bytes'], 5)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ['runtime', 'test.wav'])
        process.terminate.assert_not_called()

    def test_failed_worker_removes_partial_audio_and_job_text(self):
        process = mock.Mock(returncode=1)
        process.poll.return_value = 1
        def launch(args, **kwargs):
            job = json.loads(Path(args[-1]).read_text())
            self.output.write_bytes(b'partial')
            Path(job['resultPath']).write_text(json.dumps({'ok': False, 'error': 'Expected failure'}))
            return process
        with mock.patch.object(self.module.subprocess, 'Popen', side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, 'Expected failure'):
                self.run_job()
        self.assertEqual(list(self.root.iterdir()), [self.app])

    def test_timeout_reaps_only_owned_worker(self):
        process = mock.Mock(returncode=None)
        process.poll.side_effect = lambda: process.returncode
        process.terminate.side_effect = lambda: setattr(process, 'returncode', 1)
        with mock.patch.object(self.module.subprocess, 'Popen', return_value=process), \
             mock.patch.object(self.module.time, 'monotonic', side_effect=(0, 31)):
            with self.assertRaisesRegex(TimeoutError, 'timed out'):
                self.run_job(timeout=30)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(list(self.root.iterdir()), [self.app])

    def test_success_exit_without_valid_result_is_failure(self):
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        with mock.patch.object(self.module.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'result'):
                self.run_job()


if __name__ == '__main__':
    unittest.main()
