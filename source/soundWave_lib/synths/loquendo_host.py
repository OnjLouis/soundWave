from __future__ import annotations

import json
import os
import sys
import threading
import types
import wave
import builtins
from pathlib import Path


class CaptureWavePlayer:
    instances = []

    def __init__(self, channels, samplesPerSec, bitsPerSample, **_kwargs):
        self.channels = int(channels)
        self.samples_per_sec = int(samplesPerSec)
        self.bits_per_sample = int(bitsPerSample)
        self.pcm = bytearray()
        self.__class__.instances.append(self)

    def feed(self, data, size=None, onDone=None):
        if data:
            value = bytes(data)
            self.pcm.extend(value if size is None else value[: int(size)])
        if onDone is not None:
            onDone()

    def idle(self):
        return

    def stop(self):
        return

    def pause(self, _paused):
        return


def _write_result(path: Path, **values) -> None:
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


def _run(job_path: Path) -> None:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    result_path = Path(job["resultPath"])
    config_path = Path(job["configPath"])
    loquendo_driver_path = Path(job["loquendoDriverPath"])
    output_path = Path(job["outputPath"])

    try:
        builtins._ = lambda text: text
        builtins.pgettext = lambda _context, text: text
        builtins.ngettext = lambda singular, plural, count: singular if count == 1 else plural
        builtins.npgettext = (
            lambda _context, singular, plural, count: singular if count == 1 else plural
        )
        nvwave = types.ModuleType("nvwave")
        nvwave.WavePlayer = CaptureWavePlayer
        sys.modules["nvwave"] = nvwave

        import globalVars
        import config
        import extensionPoints
        import synthDrivers
        from synthDriverHandler import synthDoneSpeaking

        if not hasattr(config, "pre_configSave"):
            config.pre_configSave = extensionPoints.Action()
        if str(loquendo_driver_path) not in synthDrivers.__path__:
            synthDrivers.__path__.append(str(loquendo_driver_path))
        globalVars.appArgs = type("AppArgs", (), {"configPath": str(config_path)})()

        from synthDrivers.loquendo import SynthDriver

        synth = SynthDriver()
        done = threading.Event()

        def on_done(synth=None, **_kwargs):
            if synth is driver:
                done.set()

        driver = synth
        synthDoneSpeaking.register(on_done)
        try:
            options = job.get("options") or {}
            for name in (
                "forcedLanguage",
                "voice",
                "rate",
                "pitch",
                "volume",
                "languageGuesser",
                "prosody",
                "shortPauseLength",
                "mediumPauseLength",
                "longPauseLength",
                "spelling",
                "spellPunctuation",
                "numberMode",
                "dateMode",
                "timeMode",
                "timbre",
                "reverbGain",
                "reverbDelay",
                "equalizer",
                "robot",
                "whisper",
            ):
                if name in options and options[name] not in (None, "") and hasattr(synth, name):
                    setattr(synth, name, options[name])
            synth.speak([str(job.get("text") or "")])
            if not done.wait(float(job.get("timeoutSeconds") or 1800)):
                raise TimeoutError("Loquendo synthesis timed out")
        finally:
            synthDoneSpeaking.unregister(on_done)
            synth.terminate()

        players = [player for player in CaptureWavePlayer.instances if player.pcm]
        if not players:
            raise RuntimeError("Loquendo produced no audio")
        first = players[0]
        expected = (first.channels, first.samples_per_sec, first.bits_per_sample)
        if any((p.channels, p.samples_per_sec, p.bits_per_sample) != expected for p in players):
            raise RuntimeError("Loquendo changed audio format during synthesis")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(first.channels)
            output.setsampwidth(max(1, first.bits_per_sample // 8))
            output.setframerate(first.samples_per_sec)
            for player in players:
                output.writeframes(bytes(player.pcm))
        _write_result(result_path, ok=True, bytes=sum(len(p.pcm) for p in players))
    except BaseException as error:
        _write_result(result_path, ok=False, error=f"{type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    _run(Path(os.path.abspath(sys.argv[1])))
