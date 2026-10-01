import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "source/soundWave_lib/synths/ibmeci_process.py"


class ProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("eci_process_test", SOURCE)
        cls.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("SOUNDWAVE_TEST_TEMP"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dll = self.pe(self.root / "ECI.DLL")
        self.output = self.root / "capture.wav"
        self.app = self.root / "nvda"
        self.app.mkdir()
        self.runtime(self.app)

    def pe(self, path, machine=0x14C):
        content = bytearray(128)
        content[:2] = b"MZ"
        content[0x3C:0x40] = struct.pack("<I", 64)
        content[64:70] = b"PE\0\0" + struct.pack("<H", machine)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def runtime(self, directory, machine=0x14C):
        self.pe(directory / "python313.dll", machine)
        (directory / "library.zip").write_bytes(b"fixture")

    def job(self, **kwargs):
        return self.worker.run_job(self.dll, self.app, options={"voiceId": 65537}, text="Test",
                                   out_wav=self.output, temp_root=self.root, **kwargs)

    def test_old_32bit_nvda_runtime_is_supported(self):
        self.assertEqual(self.app, self.worker.python_runtime(self.app))

    def test_newest_runtime_is_chosen_numerically_not_lexically(self):
        self.runtime(self.app, machine=0x8664)
        for version in ("2026.2", "2026.10"):
            self.runtime(self.app / "lib" / version / "x86/synthDriverHost-runtime")
        self.assertEqual("2026.10", self.worker.python_runtime(self.app).parents[1].name)

    def test_wrong_architecture_is_rejected_before_launch(self):
        self.pe(self.dll, 0x8664)
        with mock.patch.object(self.worker.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "32-bit"):
                self.job()
            launch.assert_not_called()

    def test_pre_cancelled_job_does_not_start_a_process_or_leave_staging(self):
        cancelled = threading.Event()
        cancelled.set()
        before = set(self.root.iterdir())
        with mock.patch.object(self.worker.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "Cancelled"):
                self.job(cancel_evt=cancelled)
            launch.assert_not_called()
        self.assertEqual(before, set(self.root.iterdir()))

    def test_failure_removes_partial_audio_and_private_job_files(self):
        before = set(self.root.iterdir())
        process = mock.Mock(returncode=1)
        process.poll.return_value = 1
        def launch(arguments, **kwargs):
            job = json.loads(Path(arguments[-1]).read_text(encoding="utf-8"))
            Path(job["outputPath"]).write_bytes(b"partial output")
            Path(job["resultPath"]).write_text(json.dumps({"ok": False, "error": "expected failure"}), encoding="utf-8")
            self.assertEqual(self.worker.subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])
            self.assertEqual("1", kwargs["env"]["PYTHONDONTWRITEBYTECODE"])
            return process
        with mock.patch.object(self.worker.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, "expected failure"):
                self.job()
        self.assertEqual(before, set(self.root.iterdir()))
        process.terminate.assert_not_called()

    def test_timeout_terminates_and_reaps_the_exact_child(self):
        process = mock.Mock(returncode=None)
        process.poll.side_effect = lambda: process.returncode
        process.terminate.side_effect = lambda: setattr(process, "returncode", 1)
        before = set(self.root.iterdir())
        with mock.patch.object(self.worker.subprocess, "Popen", return_value=process), \
             mock.patch.object(self.worker.time, "monotonic", side_effect=(0, 31)):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                self.job(timeout=30)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()
        self.assertEqual(before, set(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
