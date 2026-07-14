# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import re
import threading
import time
import wave
from typing import Any, Dict, List, Optional

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())


def _normalise_synth_key(*parts) -> str:
    text = " ".join(str(part or "") for part in parts)
    return text.replace("_", "").replace("-", "").replace(" ", "").lower()


def is_orpheus_classic_synth(*parts) -> bool:
    return "orpheusclassic" in _normalise_synth_key(*parts)


class _OrpheusClassicCapturePlayer:
    def __init__(self, channels=2, samplesPerSec=22050, bitsPerSample=16, **kwargs):
        self.channels = int(channels or 2)
        self.samplesPerSec = int(samplesPerSec or 22050)
        self.bitsPerSample = int(bitsPerSample or 16)
        self.pcm = bytearray()
        self.last_audio_ts = 0.0
        self.total_bytes = 0

    def feed(self, data, size=None, onDone=None):
        chunk = b""
        if data:
            try:
                if isinstance(data, (bytes, bytearray, memoryview)):
                    chunk = bytes(data)
                    if size is not None:
                        chunk = chunk[:int(size)]
                elif size is None:
                    chunk = bytes(data)
                else:
                    chunk = ctypes.string_at(data, int(size))
            except Exception:
                try:
                    chunk = bytes(data)
                    if size is not None:
                        chunk = chunk[:int(size)]
                except Exception:
                    chunk = b""
        if chunk:
            self.pcm.extend(chunk)
            self.total_bytes += len(chunk)
            self.last_audio_ts = time.time()
        if onDone is not None:
            try:
                onDone()
            except Exception:
                pass

    def idle(self):
        return

    def sync(self):
        return

    def waitDone(self):
        return

    def stop(self):
        return

    def pause(self, switch):
        return

    def setVolume(self, *args, **kwargs):
        return

    def close(self):
        return


def _apply_opts(synth, opts: Optional[Dict[str, Any]]) -> None:
    if not opts:
        return
    for attr in ("voice", "variant"):
        try:
            value = str(opts.get(attr, "") or "")
            if value and hasattr(synth, attr):
                setattr(synth, attr, value)
        except Exception:
            pass
    for attr, default in (("rate", 50), ("pitch", 50), ("volume", 100)):
        try:
            if attr in opts and hasattr(synth, attr):
                setattr(synth, attr, int(opts.get(attr, default) or default))
        except Exception:
            pass


def _split_for_classic_capture(text: str) -> List[str]:
    """Keep Classic render utterances short enough for the old SAM host."""
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    pieces: List[str] = []
    for line in source.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"(?<=[.!?;:])\s+", line)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            while len(part) > 220:
                cut = part.rfind(" ", 0, 220)
                if cut < 80:
                    cut = 220
                pieces.append(part[:cut].strip())
                part = part[cut:].strip()
            if part:
                pieces.append(part)
    return pieces or [text or ""]


def _wait_for_classic_player(
    synth,
    player: _OrpheusClassicCapturePlayer,
    start_bytes: int,
    cancel_evt: threading.Event,
    progress: Optional[Dict[str, Any]],
    expected_text_len: int,
) -> None:
    deadline = time.time() + float(TIMEOUT_SECONDS or 300)
    quiet_seconds = max(0.55, min(2.25, expected_text_len / 120.0))
    saw_new_audio = False
    while time.time() < deadline:
        if cancel_evt.is_set():
            try:
                synth.cancel()
            except Exception:
                pass
            raise RuntimeError(_("Cancelled."))
        total = int(player.total_bytes)
        if total > start_bytes:
            saw_new_audio = True
        if progress is not None:
            progress["bytes"] = total
            progress["last_audio_ts"] = player.last_audio_ts or None
            progress["pcm_rate"] = int(player.samplesPerSec)
            progress["channels"] = int(player.channels)
            progress["sampwidth"] = int(player.bitsPerSample // 8)
        if saw_new_audio and player.last_audio_ts and time.time() - player.last_audio_ts >= quiet_seconds:
            return
        time.sleep(0.04)
    raise RuntimeError(_("Orpheus Classic capture timed out."))


def render_with_orpheus_classic_capture(
    text: str,
    out_wav: str,
    synth_name: str = "orpheusClassic",
    cancel_evt: Optional[threading.Event] = None,
    progress: Optional[Dict[str, Any]] = None,
    opts: Optional[Dict[str, Any]] = None,
) -> str:
    """Render Classic Orpheus through its normal driver while capturing audio.

    The generic NVDA capture path closes as soon as the driver emits
    synthDoneSpeaking. Classic Orpheus can do that before SoundWave has a useful
    capture. This path avoids that signal entirely and treats the captured audio
    stream as the source of truth.
    """
    cancel_evt = cancel_evt or threading.Event()
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    synth = _get_synth_instance(synth_name)
    if synth is None or not hasattr(synth, "speak"):
        raise RuntimeError(_("Couldn't create Orpheus Classic for capture."))

    original_player = getattr(synth, "player", None)
    player = _OrpheusClassicCapturePlayer()
    synth.player = player
    try:
        _apply_opts(synth, opts)
        pieces = _split_for_classic_capture(text)
        for piece in pieces:
            start_bytes = int(player.total_bytes)
            synth.speak([piece])
            _wait_for_classic_player(
                synth,
                player,
                start_bytes,
                cancel_evt,
                progress,
                len(piece),
            )
    finally:
        try:
            synth.cancel()
        except Exception:
            pass
        try:
            synth.player = original_player
        except Exception:
            pass
        try:
            synth.terminate()
        except Exception:
            pass

    if player.total_bytes <= 0:
        raise RuntimeError(_("Orpheus Classic capture produced no audio."))

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(player.channels)
        wf.setsampwidth(max(1, player.bitsPerSample // 8))
        wf.setframerate(player.samplesPerSec)
        wf.writeframes(bytes(player.pcm))
    return "Orpheus Classic capture"
