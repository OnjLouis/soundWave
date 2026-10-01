"""Bounded private ECI jobs using NVDA's bundled 32-bit Python runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time

PE_MACHINE_I386 = 0x14C
PROBE_TIMEOUT_SECONDS = 30
RENDER_TIMEOUT_SECONDS = 1800

def machine_type(path):
    try:
        with open(path, "rb") as stream:
            if stream.read(2) != b"MZ":
                return 0
            stream.seek(0x3C)
            offset = struct.unpack("<I", stream.read(4))[0]
            stream.seek(offset)
            if stream.read(4) != b"PE\0\0":
                return 0
            return struct.unpack("<H", stream.read(2))[0]
    except (OSError, struct.error):
        return 0


def python_runtime(app_dir):
    app_dir = Path(app_dir)
    candidates = sorted(app_dir.glob("lib/*/x86/synthDriverHost-runtime"),
                        key=lambda path: tuple(int(part) for part in path.parents[1].name.split(".") if part.isdigit()),
                        reverse=True)
    candidates.append(app_dir)  # NVDA 2025.1/2026.1 already use a 32-bit runtime.
    for candidate in candidates:
        if (candidate / "library.zip").is_file() and any(machine_type(dll) == PE_MACHINE_I386 for dll in candidate.glob("python3??.dll")):
            return candidate
    raise RuntimeError("NVDA's 32-bit speech runtime could not be found")


def stop_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_job(dll_path, app_dir, *, options=None, text=None, out_wav=None,
            timeout=RENDER_TIMEOUT_SECONDS, progress=None, cancel_evt=None, temp_root=None):
    if machine_type(dll_path) != PE_MACHINE_I386:
        raise RuntimeError("SoundWave requires a 32-bit ECI speech library")
    runtime = python_runtime(app_dir)
    module_dir = Path(__file__).resolve().parent
    launcher = module_dir.parent / "nativeHelpers/soundWavePython32.exe"
    if not launcher.is_file():
        raise RuntimeError("SoundWave's 32-bit capture helper is missing")
    process = None
    work_dir = Path(tempfile.mkdtemp(prefix="soundWave_eci_", dir=temp_root))
    succeeded = False
    try:
        if cancel_evt is not None and cancel_evt.is_set():
            raise RuntimeError("Cancelled.")
        job = {"mode": "probe" if text is None else "render", "dllPath": str(Path(dll_path).resolve()),
               "resultPath": str(work_dir / "result.json"), "options": dict(options or {}),
               "text": text, "outputPath": str(Path(out_wav).resolve()) if out_wav else None}
        job_path = work_dir / "job.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        environment = os.environ.copy()
        environment.update(PYTHONHOME=str(runtime), PYTHONPATH=str(runtime / "library.zip"), PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([str(launcher), str(runtime), str(module_dir / "ibmeci_host.py"), str(job_path)],
            cwd=runtime, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel_evt is not None and cancel_evt.is_set():
                raise RuntimeError("Cancelled.")
            if time.monotonic() > deadline:
                raise TimeoutError("IBM ECI capture timed out")
            if progress is not None and out_wav and os.path.isfile(out_wav):
                size = max(0, os.path.getsize(out_wav) - 44)
                if size > progress.get("bytes", 0):
                    progress.update(bytes=size, last_audio_ts=time.time())
            time.sleep(0.05)
        if cancel_evt is not None and cancel_evt.is_set():
            raise RuntimeError("Cancelled.")
        result_path = Path(job["resultPath"])
        if not result_path.is_file():
            raise RuntimeError(f"IBM ECI capture helper exited without a result (exit {process.returncode})")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if process.returncode or not result.get("ok"):
            raise RuntimeError(result.get("error") or f"IBM ECI capture helper failed (exit {process.returncode})")
        if progress is not None:
            progress.update({key: result[key] for key in ("bytes", "buffers", "sampleRate") if key in result})
        succeeded = True
        return result
    finally:
        if process is not None:
            stop_process(process)
        if not succeeded and out_wav:
            Path(out_wav).unlink(missing_ok=True)
        shutil.rmtree(work_dir)
