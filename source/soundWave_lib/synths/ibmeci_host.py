"""Standalone ECI capture worker; never imported into NVDA's speech driver."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys
import threading
import wave


SAMPLE_RATES = (8000, 11025, 22050)
SAMPLE_RATE_PARAMETER = 5
LANGUAGE_PARAMETER = 9
SYNTH_MODE_PARAMETER = 0
INPUT_TYPE_PARAMETER = 1
WAVEFORM_MESSAGE = 0
INDEX_REPLY_MESSAGE = 2
CALLBACK_PROCESSED = 1
CALLBACK_ABORT = 2
ACTIVE_VOICE = 0
END_MARKER = 0xFFFF
BUFFER_SAMPLES = 3300
VOICE_PARAMETERS = {
    "headSize": (1, 100), "pitch": (2, 100), "inflection": (3, 100),
    "roughness": (4, 100), "breathiness": (5, 100), "speed": (6, 250),
    "volume": (7, 100),
}
LANGUAGES = {
    65536: "American English", 65537: "British English",
    131072: "Castilian Spanish", 131073: "Latin American Spanish",
    196608: "French", 196609: "French Canadian", 262144: "German",
    327680: "Italian", 393216: "Mandarin Chinese", 393217: "Taiwanese Mandarin",
    458752: "Brazilian Portuguese", 524288: "Japanese", 589824: "Finnish",
    655360: "Korean", 720896: "Cantonese", 720897: "Hong Kong Cantonese",
    786432: "Dutch", 851968: "Norwegian", 917504: "Swedish", 983040: "Danish",
    1114112: "Thai",
}
VARIANTS = ("Engine default", "Reed", "Shelley", "Sandy", "Rocko", "Glen", "Fast Flo", "Grandma", "Grandpa")
VOICE_NAME_BUFFER_BYTES = 1024


def language_label(value):
    # Code-set flags do not change the underlying language/dialect label.
    return LANGUAGES.get(value & ~0xFF00, f"Language {value}")


def language_encoding(value):
    if value & 0x0800:
        return "utf-16-le"
    family = value >> 16
    if family in (6, 11):
        return "cp950" if value & 1 else "cp936"
    return {8: "cp932", 10: "cp949", 17: "cp874"}.get(family, "cp1252")


def configure_engine(engine, handle, options):
    variant = int(options.get("variant", 0))
    if not 0 <= variant < len(VARIANTS):
        raise ValueError("Invalid ECI variant")
    rate = int(options.get("sampleRate", engine.eciGetParam(handle, SAMPLE_RATE_PARAMETER)))
    if not 0 <= rate < len(SAMPLE_RATES):
        raise ValueError("Invalid ECI sample rate")
    engine.eciSetParam(handle, SAMPLE_RATE_PARAMETER, rate)
    if engine.eciGetParam(handle, SAMPLE_RATE_PARAMETER) != rate:
        raise RuntimeError("The selected ECI sample rate is unavailable")
    if variant and not engine.eciCopyVoice(handle, variant, ACTIVE_VOICE):
        raise RuntimeError("The selected ECI variant is unavailable")
    for name, (parameter, maximum) in VOICE_PARAMETERS.items():
        if name not in options:
            continue
        value = int(options[name])
        if not 0 <= value <= maximum:
            raise ValueError(f"Invalid ECI {name}")
        engine.eciSetVoiceParam(handle, ACTIVE_VOICE, parameter, value)
        if engine.eciGetVoiceParam(handle, ACTIVE_VOICE, parameter) != value:
            raise RuntimeError(f"The engine did not accept ECI {name}: {value}")
    return SAMPLE_RATES[rate]


def load_engine(path):
    path = Path(path).resolve(strict=True)
    os.chdir(path.parent)
    ctypes.windll.kernel32.SetDllDirectoryW(str(path.parent))
    dependencies = []
    device = path.parent / "etidev.dll"
    if device.is_file():
        dependencies.append(ctypes.WinDLL(str(device)))
    engine = ctypes.WinDLL(str(path))
    signatures = {
        "eciNewEx": ([ctypes.c_int], ctypes.c_void_p),
        "eciDelete": ([ctypes.c_void_p], None),
        "eciGetAvailableLanguages": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        "eciGetParam": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        "eciSetParam": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
        "eciGetVoiceParam": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
        "eciSetVoiceParam": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_int),
        "eciCopyVoice": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
        "eciRegisterCallback": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], None),
        "eciSetOutputBuffer": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int),
        "eciAddText": ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_int),
        "eciInsertIndex": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        "eciSynthesize": ([ctypes.c_void_p], ctypes.c_int),
        "eciSynchronize": ([ctypes.c_void_p], ctypes.c_int),
        "eciStop": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(engine, name)
        function.argtypes, function.restype = arguments, result
    engine._dependencies = dependencies
    get_name = getattr(engine, "eciGetVoiceName", None)
    if get_name is not None:
        get_name.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        get_name.restype = ctypes.c_int
    return engine


def product_name(path):
    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    version.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return ""
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 4:
        return ""
    words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
    query = f"\\StringFileInfo\\{words[0]:04x}{words[1]:04x}\\ProductName"
    if not version.VerQueryValueW(data, query, ctypes.byref(pointer), ctypes.byref(length)):
        return ""
    return ctypes.wstring_at(pointer, max(0, length.value - 1)).strip()


def probe_engine(engine, dll_path):
    count = ctypes.c_int()
    engine.eciGetAvailableLanguages(None, ctypes.byref(count))
    if not 0 < count.value <= 256:
        raise RuntimeError("ECI did not report any available languages")
    values = (ctypes.c_int * count.value)()
    engine.eciGetAvailableLanguages(values, ctypes.byref(count))
    languages = []
    rate_indexes = range(3 if product_name(dll_path) == "IBMECI" else 2)
    for language in sorted(set(values[:count.value])):
        rates = []
        profiles = {}
        for index in rate_indexes:
            handle = engine.eciNewEx(language)
            if not handle:
                continue
            try:
                engine.eciSetParam(handle, SAMPLE_RATE_PARAMETER, index)
                if engine.eciGetParam(handle, SAMPLE_RATE_PARAMETER) != index:
                    continue
                rates.append(index)
                presets = []
                for variant, label in enumerate(VARIANTS):
                    if variant and not engine.eciCopyVoice(handle, variant, ACTIVE_VOICE):
                        continue
                    get_name = getattr(engine, "eciGetVoiceName", None)
                    if variant and get_name is not None:
                        buffer = ctypes.create_string_buffer(VOICE_NAME_BUFFER_BYTES)
                        if get_name(handle, variant, buffer) and buffer.value.strip():
                            label = buffer.value.decode(language_encoding(language), errors="replace").strip()
                    defaults = {name: engine.eciGetVoiceParam(handle, ACTIVE_VOICE, parameter)
                                for name, (parameter, _maximum) in VOICE_PARAMETERS.items()}
                    defaults = {name: value for name, value in defaults.items() if value >= 0}
                    presets.append({"id": variant, "label": label, "defaults": defaults})
                profiles[str(index)] = presets
            finally:
                engine.eciDelete(handle)
        if rates:
            languages.append({"id": language, "label": language_label(language), "profiles": profiles, "sampleRates": rates})
    if not languages or any(not entry["sampleRates"] or not all(entry["profiles"].values()) for entry in languages):
        raise RuntimeError("ECI did not report usable voice settings")
    return {"languages": languages}


def render_engine(engine, job):
    options = job["options"]
    language = int(options["voiceId"])
    handle = engine.eciNewEx(language)
    if not handle:
        raise RuntimeError("The selected ECI language is unavailable")
    samples = (ctypes.c_short * BUFFER_SAMPLES)()
    callback_error = []
    finished = threading.Event()
    counters = {"bytes": 0, "buffers": 0}
    output = None
    try:
        rate = configure_engine(engine, handle, options)
        output = wave.open(job["outputPath"], "wb")
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)

        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_long, ctypes.c_void_p)
        def callback(_handle, message, value, _user):
            try:
                if message == WAVEFORM_MESSAGE:
                    if not 0 <= value <= BUFFER_SAMPLES:
                        raise RuntimeError("ECI returned an invalid audio buffer")
                    output.writeframesraw(ctypes.string_at(samples, value * 2))
                    counters["bytes"] += value * 2
                    counters["buffers"] += 1
                elif message == INDEX_REPLY_MESSAGE and value == END_MARKER:
                    finished.set()
                return CALLBACK_PROCESSED
            except Exception as error:
                callback_error.append(error)
                return CALLBACK_ABORT

        engine.eciRegisterCallback(handle, callback, None)
        if not engine.eciSetOutputBuffer(handle, BUFFER_SAMPLES, samples):
            raise RuntimeError("ECI refused audio capture")
        # Manual synthesis; literal text only, not executable ECI annotations.
        engine.eciSetParam(handle, SYNTH_MODE_PARAMETER, 1)
        engine.eciSetParam(handle, INPUT_TYPE_PARAMETER, 0)
        text = str(job["text"]).replace("`", " ").replace("\x00", " ")
        encoded = text.encode(language_encoding(language), errors="replace")
        input_buffer = ctypes.create_string_buffer(encoded + b"\0\0")
        for function, arguments in ((engine.eciAddText, (handle, ctypes.cast(input_buffer, ctypes.c_char_p))),
                                    (engine.eciInsertIndex, (handle, END_MARKER)),
                                    (engine.eciSynthesize, (handle,))):
            if not function(*arguments):
                raise RuntimeError("ECI refused synthesis")
        # Synchronize is the engine's completion contract, not an idle-audio guess.
        if not engine.eciSynchronize(handle):
            raise RuntimeError("ECI did not complete synthesis")
        if callback_error:
            raise callback_error[0]
        if not finished.is_set() or not counters["bytes"]:
            raise RuntimeError("ECI did not deliver complete audio")
        return {**counters, "sampleRate": rate, "options": options}
    finally:
        # No callback may outlive the WAV writer or its backing buffer.
        engine.eciDelete(handle)
        if output is not None:
            output.close()


def run(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    try:
        engine = load_engine(job["dllPath"])
        result = probe_engine(engine, job["dllPath"]) if job["mode"] == "probe" else render_engine(engine, job)
        payload = {"ok": True, **result}
    except Exception as error:
        payload = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    Path(job["resultPath"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
