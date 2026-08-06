# -*- coding: utf-8 -*-
"""Pocket TTS ONNX direct renderer for soundWave."""

from __future__ import annotations

import io
import re
import threading
import time
import wave

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())


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


def _is_transformer_state_error(error: Exception) -> bool:
    message = str(error or "")
    return "Reshape" in message and ("Input shape:{1,0" in message or "input_shape_size == size" in message)


def _split_failed_segment(segment: str) -> list[str]:
    target = max(48, min(180, len(segment) // 2))
    pieces = _split_for_pocket(segment, max_chars=target)
    if len(pieces) > 1 and all(len(piece) < len(segment) for piece in pieces):
        return pieces
    words = segment.split()
    if len(words) < 2:
        return []
    middle = len(words) // 2
    return [" ".join(words[:middle]), " ".join(words[middle:])]


def _render_segment_bytes(engine, segment: str, voice_path, volume_factor: float, np, cancel_evt, depth: int = 0):
    """Finish a segment before committing it so a failed model run can be retried safely."""
    output = io.BytesIO()
    buffers = 0
    try:
        for chunk in engine.stream(
            text=segment,
            voice=voice_path,
            target_buffer_sec=0.2,
            cancel_event=cancel_evt,
        ):
            if cancel_evt.is_set():
                raise RuntimeError(_("Cancelled."))
            if chunk is None:
                continue
            pcm = np.clip(chunk * volume_factor, -1.0, 1.0)
            data = (pcm * 32767).astype(np.int16).tobytes()
            if data:
                output.write(data)
                buffers += 1
        return output.getvalue(), buffers
    except Exception as error:
        if not _is_transformer_state_error(error):
            raise
        try:
            cache = getattr(engine, "_voice_state_cache", None)
            if cache is not None:
                cache.clear()
        except Exception:
            pass
        pieces = [piece for piece in _split_failed_segment(segment) if _has_tokens(engine, piece)]
        if len(pieces) < 2 or depth >= 12:
            raise RuntimeError(
                _("Pocket TTS could not render one short section of the text, even after splitting it into smaller parts.")
            ) from error
        combined = io.BytesIO()
        combined_buffers = 0
        for piece in pieces:
            data, piece_buffers = _render_segment_bytes(
                engine,
                piece,
                voice_path,
                volume_factor,
                np,
                cancel_evt,
                depth=depth + 1,
            )
            combined.write(data)
            combined_buffers += piece_buffers
        return combined.getvalue(), combined_buffers


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
        if "lsdSteps" in opts and hasattr(synth, "lsdSteps"):
            synth.lsdSteps = max(1, min(10, int(opts.get("lsdSteps", 10) or 10)))
    except Exception:
        pass

    ready = getattr(synth, "_engine_loaded_event", None)
    if ready is not None:
        deadline = time.time() + 60.0
        while not ready.is_set():
            if cancel_evt.is_set():
                raise RuntimeError(_("Cancelled."))
            if time.time() >= deadline:
                raise RuntimeError(_("Pocket TTS engine did not become ready."))
            time.sleep(0.05)

    engine = getattr(synth, "tts_engine", None)
    voice_path = getattr(synth, "_current_voice_path", None)
    if engine is None:
        raise RuntimeError(_("Pocket TTS engine is not loaded."))
    if not voice_path:
        raise RuntimeError(_("Pocket TTS has no selected voice file."))

    volume_factor = float(getattr(synth, "volume", 80) or 80) / 100.0
    total_bytes = 0
    buffers = 0
    try:
        import numpy as np
    except Exception as e:
        raise RuntimeError(_("Pocket TTS needs numpy to render.")) from e

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        segments = [segment for segment in _split_for_pocket(text) if _has_tokens(engine, segment)]
        if not segments:
            raise RuntimeError(_("Pocket TTS had no speakable text to render."))
        if progress is not None:
            progress["chunksTotal"] = max(int(progress.get("chunksTotal", 1) or 1), len(segments))
        for segment_index, segment in enumerate(segments, start=1):
            if cancel_evt.is_set():
                raise RuntimeError(_("Cancelled."))
            if progress is not None:
                progress["chunksCurrent"] = segment_index
            data, segment_buffers = _render_segment_bytes(
                engine,
                segment,
                voice_path,
                volume_factor,
                np,
                cancel_evt,
            )
            if data:
                wf.writeframes(data)
                total_bytes += len(data)
                buffers += segment_buffers
            if progress is not None:
                progress["bytes"] = total_bytes
                progress["buffers"] = buffers
                progress["last_audio_ts"] = time.time() if data else progress.get("last_audio_ts")
                progress["pcm_rate"] = 24000
                progress["channels"] = 1
                progress["sampwidth"] = 2
            if progress is not None:
                progress["chunksDone"] = segment_index
    if total_bytes <= 0:
        raise RuntimeError(_("Pocket TTS produced no audio."))
    return "Pocket TTS direct"
