from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import globalVars

from soundWave_lib import runtime as _runtime

_runtime.bind(globals())


_COMPACT_SYNTH_ID = "loquendo32"
_HOST_POLL_SECONDS = 0.05


def is_loquendo_synth(synth_name: str, synth_label: str = "") -> bool:
    synth_id = str(synth_name or "")
    compact = "".join(ch for ch in f"{synth_id} {synth_label or ''}".casefold() if ch.isalnum())
    return synth_id.casefold() == _COMPACT_SYNTH_ID or "loquendotts7" in compact


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _installed_packages():
    config_path = Path(globalVars.appArgs.configPath)
    for metadata_path in (config_path / "addons").glob("*/loqueNVDA-package.json"):
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if value.get("schemaVersion") == 1:
            yield metadata_path, value


def _loquendo_driver_path() -> Path:
    for metadata_path, package in _installed_packages():
        if package.get("kind") == "engine" and package.get("id") == "loquendo-tts-7":
            candidate = metadata_path.parent / "synthDrivers"
            if (candidate / "loquendo" / "__init__.py").is_file():
                return candidate
    raise RuntimeError(_("The Loquendo TTS 7 NVDA add-on is not installed correctly."))


def create_options_facade():
    voices = OrderedDict()
    for _metadata_path, package in _installed_packages():
        if package.get("kind") != "voice":
            continue
        identifier = str(package.get("id") or "").strip()
        if not identifier:
            continue
        voices[identifier] = SimpleNamespace(
            id=identifier,
            name=str(package.get("displayName") or identifier),
            language=str(package.get("locale") or ""),
        )
    if not voices:
        raise RuntimeError(_("No Loquendo TTS 7 voices are installed."))
    return SimpleNamespace(
        availableVoices=voices,
        availableVariants=OrderedDict(),
        voice=next(iter(voices)),
        rate=50,
        pitch=50,
        volume=100,
        supportedSettings=(),
    )


def _x86_python_runtime() -> Path:
    app_dir = Path(globalVars.appDir)
    candidates = sorted(
        app_dir.glob("lib/*/x86/synthDriverHost-runtime"),
        key=lambda path: path.parent.parent.name,
        reverse=True,
    )
    for candidate in candidates:
        if (candidate / "library.zip").is_file() and any(candidate.glob("python3??.dll")):
            return candidate
    raise RuntimeError(_("NVDA's 32-bit speech runtime could not be found."))


def render_to_wav(
    text: str,
    out_wav: str,
    *,
    opts: Optional[Dict[str, Any]] = None,
    cancel_evt=None,
    progress: Optional[Dict[str, Any]] = None,
    timeout_seconds: float = 1800,
) -> str:
    module_dir = Path(__file__).resolve().parent
    launcher = module_dir.parent / "nativeHelpers" / "soundWavePython32.exe"
    host_script = module_dir / "loquendo_host.py"
    if not launcher.is_file() or not host_script.is_file():
        raise RuntimeError(_("SoundWave's Loquendo capture helper is missing."))

    runtime = _x86_python_runtime()
    work_dir = Path(tempfile.mkdtemp(prefix="soundWave_loquendo_"))
    job_path = work_dir / "job.json"
    result_path = work_dir / "result.json"
    job = {
        "resultPath": str(result_path),
        "configPath": str(globalVars.appArgs.configPath),
        "loquendoDriverPath": str(_loquendo_driver_path()),
        "outputPath": str(Path(out_wav).resolve()),
        "text": str(text or ""),
        "timeoutSeconds": float(timeout_seconds),
        "options": dict(opts or {}),
    }
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONHOME"] = str(runtime)
    env["PYTHONPATH"] = str(runtime / "library.zip")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = None
    completed = False
    try:
        process = subprocess.Popen(
            [str(launcher), str(runtime), str(host_script), str(job_path)],
            cwd=str(runtime),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + float(timeout_seconds) + 10.0
        while process.poll() is None:
            if cancel_evt is not None and cancel_evt.is_set():
                _stop_process(process)
                raise RuntimeError(_("Cancelled."))
            if time.monotonic() >= deadline:
                _stop_process(process)
                raise RuntimeError(_("Loquendo capture timed out."))
            time.sleep(_HOST_POLL_SECONDS)
        if not result_path.is_file():
            raise RuntimeError(
                _("Loquendo capture helper stopped with exit code {code}.").format(
                    code=process.returncode
                )
            )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or _("Loquendo capture failed.")))
        if progress is not None:
            progress["bytes"] = int(result.get("bytes") or 0)
        completed = True
        return "Loquendo TTS 7 direct capture"
    finally:
        if process is not None:
            _stop_process(process)
        if not completed:
            try:
                Path(out_wav).unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)
