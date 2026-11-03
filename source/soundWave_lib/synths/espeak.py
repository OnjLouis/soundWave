"""eSpeak discovery and rendering without touching NVDA's shared engine."""
from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

from soundWave_lib import runtime as _runtime
from soundWave_lib.synths.ibmeci_process import machine_type, stop_process

_runtime.bind(globals())

_PROBE_TIMEOUT_SECONDS = 30
_RENDER_TIMEOUT_SECONDS = 1800
_LAUNCHERS = {0x14C: 'soundWavePython32.exe', 0x8664: 'soundWavePython64.exe'}


def is_espeak_synth(name):
    return ''.join(char for char in str(name or '').casefold() if char.isalnum()) in ('espeak', 'espeakng')


def runtime_and_launcher(app_dir):
    runtime = Path(app_dir)
    engine_machine = machine_type(runtime / 'synthDrivers/espeak.dll')
    if engine_machine not in _LAUNCHERS or not (runtime / 'library.zip').is_file():
        raise RuntimeError(_("NVDA's eSpeak runtime is missing or has an unsupported architecture."))
    if not any(machine_type(path) == engine_machine for path in runtime.glob('python3*.dll')
               if path.stem[7:].isdigit()):
        raise RuntimeError(_("NVDA's Python and eSpeak engine architectures do not match."))
    launcher = Path(__file__).resolve().parent.parent / 'nativeHelpers' / _LAUNCHERS[engine_machine]
    if not launcher.is_file():
        raise RuntimeError(_("SoundWave's eSpeak capture helper is missing."))
    return runtime, launcher


def run_job(app_dir, *, text=None, out_wav=None, options=None, language='en', timeout=None,
            cancel_evt=None, progress=None, temp_root=None):
    if cancel_evt is not None and cancel_evt.is_set():
        raise RuntimeError(_("Cancelled."))
    runtime, launcher = runtime_and_launcher(app_dir)
    timeout = float(timeout if timeout is not None else
                    (_PROBE_TIMEOUT_SECONDS if text is None else _RENDER_TIMEOUT_SECONDS))
    work_dir = Path(tempfile.mkdtemp(prefix='soundWave_espeak_', dir=temp_root))
    process = None
    succeeded = False
    try:
        result_path = work_dir / 'result.json'
        job = {'mode': 'probe' if text is None else 'render', 'appDir': str(runtime),
               'parentPid': os.getpid(), 'resultPath': str(result_path),
               'outputPath': str(Path(out_wav).resolve()) if out_wav else None,
               'language': str(language or 'en'), 'text': text, 'options': dict(options or {}),
               'timeoutSeconds': timeout}
        job_path = work_dir / 'job.json'
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        environment = os.environ.copy()
        environment.update(PYTHONHOME=str(runtime), PYTHONPATH=str(runtime / 'library.zip'),
                           PYTHONDONTWRITEBYTECODE='1')
        with (work_dir / 'worker.log').open('wb') as errors:
            process = subprocess.Popen(
                [str(launcher), str(runtime), '-S', str(Path(__file__).with_name('espeak_host.py')), str(job_path)],
                cwd=runtime, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=errors, creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancel_evt is not None and cancel_evt.is_set():
                    raise RuntimeError(_("Cancelled."))
                if time.monotonic() > deadline:
                    raise TimeoutError(_("eSpeak capture timed out."))
                if progress is not None and out_wav and os.path.isfile(out_wav):
                    size = max(0, os.path.getsize(out_wav) - 44)
                    if size > progress.get('bytes', 0):
                        progress.update(bytes=size, last_audio_ts=time.time())
                time.sleep(0.05)
        if cancel_evt is not None and cancel_evt.is_set():
            raise RuntimeError(_("Cancelled."))
        if not result_path.is_file():
            with (work_dir / 'worker.log').open('rb') as errors:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 2048))
                detail = errors.read(2048).decode('utf-8', errors='replace').strip()
            raise RuntimeError(_("eSpeak capture helper exited without a result (exit {code}).").format(
                code=process.returncode) + ('\n' + detail if detail else ''))
        result = json.loads(result_path.read_text(encoding='utf-8'))
        if process.returncode or not result.get('ok'):
            messages = {
                'voice': _("The selected eSpeak voice is no longer available."),
                'variant': _("The selected eSpeak variant is no longer available."),
                'empty': _("eSpeak produced no audio."),
            }
            raise RuntimeError(messages.get(result.get('errorCode')) or
                _("eSpeak capture failed: {error}").format(error=result.get('error') or process.returncode))
        if progress is not None:
            progress.update({key: result[key] for key in ('bytes', 'pcm_rate', 'channels', 'sampwidth') if key in result})
        succeeded = True
        return result
    finally:
        if process is not None:
            stop_process(process)
        if not succeeded and out_wav:
            try:
                Path(out_wav).unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(work_dir)


def _app_dir():
    import globalVars
    return globalVars.appDir


def _ui_language():
    try:
        import languageHandler
        return languageHandler.getLanguage()
    except ImportError:
        return 'en'


@lru_cache(maxsize=2)
def _probe(app_dir, language):
    return run_job(app_dir, language=language)


def create_options_facade():
    result = _probe(_app_dir(), _ui_language())
    voices = OrderedDict((item['id'], SimpleNamespace(**item)) for item in result['voices'])
    variants = OrderedDict((item['id'], SimpleNamespace(**item)) for item in result['variants'])
    return SimpleNamespace(availableVoices=voices, availableVariants=variants,
                           voice=result['voice'], variant='max', rate=30, pitch=40, volume=100,
                           rateBoost=False, supportedSettings=(SimpleNamespace(id='rateBoost'),),
                           terminate=lambda: None)


def render_to_wav(text, out_wav, *, opts=None, cancel_evt=None, progress=None):
    if not str(out_wav).lower().endswith('.wav'):
        out_wav = str(out_wav) + '.wav'
    run_job(_app_dir(), text=str(text or ''), out_wav=out_wav, options=opts,
            language=_ui_language(), cancel_evt=cancel_evt, progress=progress)
    return 'eSpeak NG isolated capture'
