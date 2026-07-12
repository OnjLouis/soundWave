# -*- coding: utf-8 -*-
"""googleTtsForNvda capture helpers for soundWave."""

from __future__ import annotations

import threading
import time
import wave

from logHandler import log

DEFAULT_AUTO_TEST = True
ALLOW_AUTO_TEST = True
ALLOW_MANUAL_TEST = True


def _is_missing_bridge_function_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return "googlettsfornvdaspeak is not a function" in message


def is_google_tts_synth(synth_name: str) -> bool:
    """Return True for the googleTtsForNvda driver family."""
    name = (synth_name or "").strip().lower()
    if not name:
        return False
    return "googletts" in name or "google_tts" in name or "google-tts" in name


def sample_rate(synth) -> int:
    for attr in ("sampleRate", "_sampleRate", "samplesPerSec", "_samplesPerSec"):
        try:
            value = int(getattr(synth, attr))
            if value > 0:
                return value
        except Exception:
            pass
    return 24000


def _available_voice_ids(synth) -> list[str]:
    try:
        voices = getattr(synth, "availableVoices", None)
        if voices:
            if hasattr(voices, "keys"):
                return [str(v) for v in voices.keys() if str(v)]
            return [str(v) for v in voices if str(v)]
    except Exception:
        pass
    try:
        voice = str(getattr(synth, "voice", "") or "")
        if voice:
            return [voice]
    except Exception:
        pass
    return []


def installed_voice_ids(synth) -> set[str]:
    """Return Google TTS voice IDs whose packages are installed locally."""
    try:
        from synthDrivers.googleTtsForNvda import voice_store  # type: ignore
    except Exception:
        return set(_available_voice_ids(synth))
    try:
        catalog = synth.catalog
        installed_packages = {pkg.id for pkg in voice_store.installed_packages(catalog)}
        return {
            str(speaker.id)
            for speaker in getattr(catalog, "speakers", [])
            if getattr(speaker, "packageId", "") in installed_packages
            # The current googleTtsForNvda bridge advertises Natural/SeaNet
            # voices as installed, but the offline bridge often rejects them
            # with "Voice is not available" during real rendering. Keep the
            # SoundWave list to voices that pass the render path reliably.
            and "-seanet" not in str(getattr(speaker, "packageId", "")).lower()
        }
    except Exception:
        return set(_available_voice_ids(synth))


def _resolve_voice_id(synth, requested_voice: str) -> str:
    voice_ids = sorted(installed_voice_ids(synth))
    current_voice = ""
    try:
        current_voice = str(getattr(synth, "voice", "") or "")
    except Exception:
        current_voice = ""

    if requested_voice:
        if requested_voice in voice_ids:
            return requested_voice
        raise RuntimeError("Selected Google TTS voice is not available: %s" % requested_voice)
    if current_voice and current_voice in voice_ids:
        return current_voice
    if voice_ids:
        return voice_ids[0]
    raise RuntimeError("No Google TTS voices are available.")


def _chrome_value(synth, method_name: str, value: int, fallback):
    try:
        method = getattr(synth, method_name, None)
        if callable(method):
            return method(int(value))
    except Exception:
        pass
    return fallback(int(value))


def _build_exact_options(synth, voice_id: str, opts: dict) -> dict:
    rate = int(opts.get("rate", getattr(synth, "rate", 50) or 50) or 50)
    pitch = int(opts.get("pitch", getattr(synth, "pitch", 50) or 50) or 50)
    volume = max(0, min(100, int(opts.get("volume", getattr(synth, "volume", 100) or 100) or 100)))
    try:
        speaker = synth.catalog.speaker_for_voice(voice_id)
    except Exception as e:
        raise RuntimeError("Selected Google TTS voice is not available: %s" % voice_id) from e
    options = synth._speech_options(rate, pitch, volume, voice_id)  # noqa: SLF001 - synth-specific integration
    options["voiceId"] = speaker.id
    options["voiceName"] = speaker.name
    options["lang"] = speaker.language
    options["rate"] = _chrome_value(synth, "_rate_to_chrome", rate, lambda v: round(max(0.1, min(10.0, 0.35 + (2.0 - 0.35) * max(0, min(100, v)) / 100.0)), 3))
    options["pitch"] = _chrome_value(synth, "_pitch_to_chrome", pitch, lambda v: round(max(0.1, min(3.0, 1.0 + (-12.0 + 24.0 * max(0, min(100, v)) / 100.0) / 20.0)), 3))
    options["volume"] = max(0.0, min(1.0, volume / 100.0))
    options["outputGain"] = max(0.0, min(2.0, volume / 50.0))
    return options


def _reset_bridge(synth) -> None:
    bridge = getattr(synth, "_bridge", None)
    if bridge is None:
        return
    for method_name in ("terminate", "_close_websocket"):
        try:
            method = getattr(bridge, method_name, None)
            if callable(method):
                method()
                return
        except Exception:
            pass


def render_to_wav(text: str, out_wav: str, synth, opts=None, progress=None, cancel_evt=None) -> str:
    """Render googleTtsForNvda directly through its bridge audio callback."""
    cancel_evt = cancel_evt or threading.Event()
    opts = opts or {}
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    requested_voice = str(opts.get("voice", "") or "")
    for attr, key, default in (("rate", "rate", 50), ("pitch", "pitch", 50), ("volume", "volume", 100)):
        try:
            if key in opts and hasattr(synth, attr):
                setattr(synth, attr, int(opts.get(key, default) or default))
        except Exception:
            pass

    pcm_parts = []
    voice_id = _resolve_voice_id(synth, requested_voice)
    if cancel_evt.is_set():
        raise RuntimeError("Cancelled.")
    if progress is not None:
        progress["bytes"] = 0
        progress["buffers"] = 0
        progress["waitingText"] = "Audio progress: waiting for Google to deliver audio"

    try:
        options = _build_exact_options(synth, voice_id, opts)
        log.debug(
            "soundWave: Google TTS render using voiceId=%s voiceName=%s lang=%s",
            options.get("voiceId", ""),
            options.get("voiceName", ""),
            options.get("lang", ""),
        )

        def on_audio(pcm: bytes) -> None:
            if cancel_evt.is_set():
                return
            if pcm:
                chunk = bytes(pcm)
                pcm_parts.append(chunk)
                now = time.time()
                if progress is not None:
                    progress["bytes"] = int(progress.get("bytes", 0) or 0) + len(chunk)
                    progress["buffers"] = int(progress.get("buffers", 0) or 0) + 1
                    progress["last_audio_ts"] = now
                    progress["waitingText"] = ""
                    progress["pcm_rate"] = int(sample_rate(synth))
                    progress["channels"] = 1
                    progress["sampwidth"] = 2

        try:
            synth._bridge.speak(text or "", options, on_audio, cancel_evt)  # noqa: SLF001
        except Exception as e:
            if not _is_missing_bridge_function_error(e):
                raise
            log.debug("soundWave: Google TTS bridge was not ready; resetting bridge and retrying once")
            pcm_parts.clear()
            if progress is not None:
                progress["bytes"] = 0
                progress["buffers"] = 0
                progress["last_audio_ts"] = None
                progress["waitingText"] = "Audio progress: reconnecting to Google TTS"
            _reset_bridge(synth)
            if cancel_evt.is_set():
                raise RuntimeError("Cancelled.") from e
            synth._bridge.speak(text or "", options, on_audio, cancel_evt)  # noqa: SLF001
    except Exception as e:
        raise RuntimeError("Google TTS render failed: %s" % (str(e).strip() or e.__class__.__name__)) from e

    if cancel_evt.is_set():
        raise RuntimeError("Cancelled.")
    if not pcm_parts:
        raise RuntimeError("Google TTS produced no audio.")

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate(synth))
        for chunk in pcm_parts:
            wf.writeframes(chunk)
    return "Google TTS bridge"
