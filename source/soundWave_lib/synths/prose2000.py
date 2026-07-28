# -*- coding: utf-8 -*-
from __future__ import annotations

import builtins
import os
import queue
import threading
import time
import wave
from typing import Any, Dict, Optional

_ = getattr(builtins, "_", lambda text: text)


def is_prose2000_synth(*parts) -> bool:
    key = "".join(str(part or "") for part in parts).replace("_", "").replace("-", "").replace(" ", "").lower()
    return "prose2000" in key


def render_to_wav(
    text: str,
    out_wav: str,
    cancel_evt: Optional[threading.Event] = None,
    progress: Optional[Dict[str, Any]] = None,
    opts: Optional[Dict[str, Any]] = None,
) -> str:
    """Render through an isolated Prose host without altering NVDA audio globals."""
    try:
        from synthDrivers import prose2000 as driver
    except Exception as error:
        raise RuntimeError(_("Prose 2000 is not installed or could not be loaded.")) from error

    if not driver.SynthDriver.check():
        raise RuntimeError(_("The Prose 2000 host or firmware files are unavailable."))

    cancel_evt = cancel_evt or threading.Event()
    opts = opts or {}
    cleaned = driver._cleanText(text or "").strip()
    if not cleaned:
        raise RuntimeError(_("There is no text to render."))
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    rate = max(0, min(100, int(opts.get("rate", 50) or 50)))
    pitch = max(0, min(100, int(opts.get("pitch", 50) or 50)))
    volume = max(0, min(100, int(opts.get("volume", 100) or 100)))
    rate_boost = bool(opts.get("rateBoost", False))
    host = driver._ProseHost()
    processor = None
    native_controls = all(
        hasattr(driver.SynthDriver, name)
        for name in ("_mapNativeRate", "_mapNativePitch", "_mapNativeVolume")
    )
    if native_controls:
        control_prefix = (
            f"\x1b[{driver.SynthDriver._mapNativeRate(rate)}r"
            "\x1b[0V"
            f"\x1b[{driver.SynthDriver._mapNativePitch(pitch)}p"
            f"\x1b[{driver.SynthDriver._mapNativeVolume(volume)}a"
        )
        cleaned = control_prefix + cleaned
    else:
        processor_type = getattr(driver, "_AudioProcessor", None)
        if processor_type is None:
            raise RuntimeError(_("The Prose 2000 host or firmware files are unavailable."))
        processor = processor_type(rate, rate_boost, volume)
    pcm = bytearray()
    generation = 1
    last_progress = time.monotonic()

    try:
        host.start()
        host.send(driver._SPEAK, generation, cleaned.encode("ascii", "replace"))
        while True:
            if cancel_evt.is_set():
                host.cancel(generation)
                raise RuntimeError(_("Cancelled."))
            try:
                message_type, message_generation, payload = host.getMessage(0.25)
            except queue.Empty:
                if time.monotonic() - last_progress > 300:
                    raise RuntimeError(_("Prose 2000 rendering timed out."))
                continue
            last_progress = time.monotonic()
            if message_type is None:
                raise RuntimeError(payload.decode("utf-8", "replace"))
            if message_generation != generation:
                continue
            if message_type == driver._AUDIO:
                audio = processor.process(payload) if processor is not None else payload
                if audio:
                    pcm.extend(audio)
                    if progress is not None:
                        progress["bytes"] = len(pcm)
                        progress["last_audio_ts"] = time.time()
                        progress["pcm_rate"] = 10000
                        progress["channels"] = 1
                        progress["sampwidth"] = 2
            elif message_type == driver._DONE:
                if processor is not None:
                    pcm.extend(processor.finish())
                break
            elif message_type == driver._CANCELLED:
                raise RuntimeError(_("Cancelled."))
            elif message_type == driver._ERROR:
                raise RuntimeError(payload.decode("utf-8", "replace"))
    finally:
        host.stop()

    if not pcm:
        raise RuntimeError(_("Prose 2000 produced no audio."))
    os.makedirs(os.path.dirname(os.path.abspath(out_wav)), exist_ok=True)
    with wave.open(out_wav, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(10000)
        output.writeframes(pcm)
    return "Prose 2000 native host capture" if native_controls else "Prose 2000 host capture"
