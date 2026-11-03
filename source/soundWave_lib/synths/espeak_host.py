"""Private-process adapter for NVDA's installed eSpeak engine; no audio device."""
from __future__ import annotations

import builtins
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import sys
import threading
import types
import wave

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF


class CaptureError(RuntimeError):
    pass


def watch_parent(parent_pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(SYNCHRONIZE, False, int(parent_pid))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    def wait():
        try:
            if kernel.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
                os._exit(1)
        finally:
            kernel.CloseHandle(handle)
    threading.Thread(target=wait, name='eSpeak parent monitor', daemon=True).start()


class CapturePlayer:
    output_path = None
    instances = []

    def __init__(self, channels, samplesPerSec, bitsPerSample, **kwargs):
        self.samplesPerSec = samplesPerSec
        self.channels = channels
        self.sampwidth = bitsPerSample // 8
        self.bytes = 0
        self.output = None
        if self.output_path:
            self.output = wave.open(str(self.output_path), 'wb')
            self.output.setparams((channels, self.sampwidth, samplesPerSec, 0, 'NONE', 'not compressed'))
        self.instances.append(self)

    def feed(self, data, size=None, onDone=None):
        if data:
            chunk = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else ctypes.string_at(data, size)
            if size is not None:
                chunk = chunk[:size]
            if self.output:
                self.output.writeframesraw(chunk)
                self.bytes += len(chunk)
        if onDone:
            onDone()

    def idle(self):
        if self.output:
            self.output._file.flush()

    def stop(self):
        pass

    def pause(self, paused):
        pass

    def close(self):
        if self.output:
            self.output.close()
            self.output = None


def module(name, **attributes):
    value = types.ModuleType(name)
    value.__dict__.update(attributes)
    sys.modules[name] = value


def run(job):
    watch_parent(job['parentPid'])
    errors = []
    class EngineError(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage())
    logger = logging.getLogger('soundWave.eSpeak')
    logger.addHandler(EngineError())
    builtins.pgettext = lambda context, text: text
    module('nvwave', WavePlayer=CapturePlayer)
    module('globalVars', appDir=job['appDir'])
    module('config', conf={'audio': {'outputDevice': 'default'}})
    module('logHandler', log=logger)
    module('speechXml', toXmlLang=lambda value: value.replace('_', '-'))
    from synthDrivers import _espeak
    if job['mode'] == 'render':
        CapturePlayer.output_path = job['outputPath']
    done = threading.Event()
    initialized = False
    try:
        _espeak.initialize(lambda index: done.set() if index is None else None)
        initialized = True
        _espeak.setVoiceByLanguage(job.get('language') or 'en')
        voices = [{'id': os.path.basename(_espeak.decodeEspeakString(v.identifier)).lower(),
                   'name': _espeak.decodeEspeakString(v.name),
                   'language': _espeak.decodeEspeakString(v.languages[1:])} for v in _espeak.getVoiceList()]
        variants = [{'id': key, 'name': value} for key, value in _espeak.getVariantDict().items()]
        if job['mode'] == 'probe':
            current = _espeak.getCurrentVoice()
            voice = os.path.basename(_espeak.decodeEspeakString(current.identifier).split('+')[0]).lower()
            return {'ok': True, 'voices': voices, 'variants': variants, 'voice': voice}
        options = job.get('options') or {}
        voice = options.get('voice') or None
        variant = options.get('variant') or 'max'
        if voice and voice not in {item['id'] for item in voices}:
            raise CaptureError('voice')
        if variant not in {item['id'] for item in variants}:
            raise CaptureError('variant')
        _espeak.setVoiceAndVariant(voice=voice, variant=variant)
        def percent(name, default, low, high):
            value = max(0, min(100, int(options.get(name, default))))
            return int(round(low + value * (high - low) / 100))
        rate = percent('rate', 30, _espeak.minRate, _espeak.maxRate)
        if options.get('rateBoost'):
            rate *= 3
        _espeak.setParameter(_espeak.espeakRATE, rate, 0)
        _espeak.setParameter(_espeak.espeakPITCH, percent('pitch', 40, _espeak.minPitch, _espeak.maxPitch), 0)
        _espeak.setParameter(_espeak.espeakRANGE, percent('inflection', 75, _espeak.minPitch, _espeak.maxPitch), 0)
        _espeak.setParameter(_espeak.espeakVOLUME, max(0, min(100, int(options.get('volume', 100)))), 0)
        text = str(job.get('text') or '').translate({1: None, ord('<'): '&lt;', ord('>'): '&gt;', ord('['): ' ['})
        _espeak.speak(text)
        # Engine errors are recorded in memory, never logged to disk from the audio callback.
        import time
        deadline = time.monotonic() + float(job['timeoutSeconds'])
        while not done.wait(0.05):
            if errors:
                raise RuntimeError(errors[0])
            if time.monotonic() >= deadline:
                raise TimeoutError('eSpeak synthesis timed out')
        if errors:
            raise RuntimeError(errors[0])
    finally:
        if initialized:
            _espeak.terminate()
        for player in CapturePlayer.instances:
            player.close()
    player = CapturePlayer.instances[0]
    if not player.bytes:
        raise CaptureError('empty')
    return {'ok': True, 'bytes': player.bytes, 'pcm_rate': player.samplesPerSec,
            'channels': player.channels, 'sampwidth': player.sampwidth}


if __name__ == '__main__':
    job_path = Path(sys.argv[1])
    job = json.loads(job_path.read_text(encoding='utf-8'))
    job_path.unlink()  # Speech text need not remain on disk while the worker runs.
    try:
        result = run(job)
    except BaseException as error:
        result = {'ok': False, 'error': '{}: {}'.format(type(error).__name__, error),
                  'errorCode': str(error) if isinstance(error, CaptureError) else None}
    Path(job['resultPath']).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    raise SystemExit(0 if result.get('ok') else 1)
