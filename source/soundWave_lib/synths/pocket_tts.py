# -*- coding: utf-8 -*-
"""Pocket TTS ONNX direct renderer for soundWave."""

from __future__ import annotations

import re
import threading
import time
import wave


def is_pocket_tts_synth(synth_name: str) -> bool:
    name = (synth_name or "").strip().lower()
    return "pocket_tts" in name or "pocket tts" in name


def _split_for_pocket(text: str, max_chars: int = 360) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    pieces = re.split(r"(?<=[.!?;:])\s+", text)
    segments: list[str] = []
    current: list[str] = []
    current_len = 0

    def emit() -> None:
        nonlocal current, current_len
        joined = " ".join(current).strip()
        if joined:
            segments.append(joined)
        current = []
        current_len = 0

    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > max_chars:
            emit()
            words = piece.split()
            buf: list[str] = []
            buf_len = 0
            for word in words:
                extra = len(word) + (1 if buf else 0)
                if buf and buf_len + extra > max_chars:
                    segments.append(" ".join(buf))
                    buf = [word]
                    buf_len = len(word)
                else:
                    buf.append(word)
                    buf_len += extra
            if buf:
                segments.append(" ".join(buf))
            continue
        extra = len(piece) + (1 if current else 0)
        if current and current_len + extra > max_chars:
            emit()
        current.append(piece)
        current_len += extra
    emit()
    return segments


def _has_tokens(engine, text: str) -> bool:
    try:
        tokens = engine._tokenize(text)  # noqa: SLF001 - defensive Pocket TTS integration
        return bool(getattr(tokens, "shape", [0, 0])[1] > 0)
    except Exception:
        return bool((text or "").strip())


def render_to_wav(text: str, out_wav: str, synth, opts=None, progress=None, cancel_evt=None) -> str:
    """Render Pocket TTS directly from its ONNX stream API."""
    cancel_evt = cancel_evt or threading.Event()
    opts = opts or {}
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    voice = str(opts.get("voice", "") or "")
    try:
        if voice and hasattr(synth, "availableVoices") and voice in synth.availableVoices:
            synth.voice = voice
    except Exception:
        pass
    try:
        if "volume" in opts and hasattr(synth, "volume"):
            synth.volume = max(0, min(100, int(opts.get("volume", 80) or 80)))
    except Exception:
        pass
    try:
        if "eosThreshold" in opts and hasattr(synth, "eosThreshold"):
            synth.eosThreshold = max(0, min(100, int(opts.get("eosThreshold", 50) or 50)))
    except Exception:
        pass

    ready = getattr(synth, "_engine_loaded_event", None)
    if ready is not None:
        deadline = time.time() + 60.0
        while not ready.is_set():
            if cancel_evt.is_set():
                raise RuntimeError("Cancelled.")
            if time.time() >= deadline:
                raise RuntimeError("Pocket TTS engine did not become ready.")
            time.sleep(0.05)

    engine = getattr(synth, "tts_engine", None)
    voice_path = getattr(synth, "_current_voice_path", None)
    if engine is None:
        raise RuntimeError("Pocket TTS engine is not loaded.")
    if not voice_path:
        raise RuntimeError("Pocket TTS has no selected voice file.")

    volume_factor = float(getattr(synth, "volume", 80) or 80) / 100.0
    total_bytes = 0
    buffers = 0
    try:
        import numpy as np
    except Exception as e:
        raise RuntimeError("Pocket TTS needs numpy to render.") from e

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        segments = [segment for segment in _split_for_pocket(text) if _has_tokens(engine, segment)]
        if not segments:
            raise RuntimeError("Pocket TTS had no speakable text to render.")
        if progress is not None:
            progress["chunksTotal"] = max(int(progress.get("chunksTotal", 1) or 1), len(segments))
        for segment_index, segment in enumerate(segments, start=1):
            if cancel_evt.is_set():
                raise RuntimeError("Cancelled.")
            if progress is not None:
                progress["chunksCurrent"] = segment_index
            for chunk in engine.stream(text=segment, voice=voice_path, target_buffer_sec=0.2):
                if cancel_evt.is_set():
                    raise RuntimeError("Cancelled.")
                if chunk is None:
                    continue
                pcm = np.clip(chunk * volume_factor, -1.0, 1.0)
                data = (pcm * 32767).astype(np.int16).tobytes()
                if not data:
                    continue
                wf.writeframes(data)
                total_bytes += len(data)
                buffers += 1
                if progress is not None:
                    progress["bytes"] = total_bytes
                    progress["buffers"] = buffers
                    progress["last_audio_ts"] = time.time()
                    progress["pcm_rate"] = 24000
                    progress["channels"] = 1
                    progress["sampwidth"] = 2
            if progress is not None:
                progress["chunksDone"] = segment_index
    if total_bytes <= 0:
        raise RuntimeError("Pocket TTS produced no audio.")
    return "Pocket TTS direct"
