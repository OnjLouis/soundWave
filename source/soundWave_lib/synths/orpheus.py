# -*- coding: utf-8 -*-
from __future__ import annotations

import array
import os
import struct
import threading
import wave
from ctypes import WINFUNCTYPE, c_void_p
from ctypes.wintypes import DWORD
from typing import Dict, List, Optional

import ctypes
import wx
from logHandler import log

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())
from soundWave_lib import voice_utils

class OrpheusOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent, synth, initial: Optional[dict] = None):
        super().__init__(parent, title=_("soundWave - Orpheus options"))
        self.synth = synth
        self.initial = initial or {}

        pnl = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(pnl, label=_("&Language:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self.langChoice = wx.Choice(pnl, choices=[])
        grid.Add(self.langChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(pnl, label=_("&Voice:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self.voiceChoice = wx.Choice(pnl, choices=[])
        grid.Add(self.voiceChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(pnl, label=_("&Speed (%):")), 0, wx.ALIGN_CENTER_VERTICAL)
        self.speedSpin = wx.SpinCtrl(pnl, min=20, max=400, initial=int(self.initial.get("speed", 100) or 100))
        grid.Add(self.speedSpin, 0, wx.ALIGN_LEFT)

        grid.Add(wx.StaticText(pnl, label=_("&Pitch:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pitchSpin = wx.SpinCtrl(pnl, min=0, max=100, initial=int(self.initial.get("pitch", _safe_getattr(self.synth, "pitch", 50) or 50)))
        grid.Add(self.pitchSpin, 0, wx.ALIGN_LEFT)

        grid.Add(wx.StaticText(pnl, label=_("Vol&ume:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self.volumeSpin = wx.SpinCtrl(pnl, min=0, max=100, initial=int(self.initial.get("volume", _safe_getattr(self.synth, "volume", _safe_getattr(self.synth, "_volume", 100)) or 100)))
        grid.Add(self.volumeSpin, 0, wx.ALIGN_LEFT)

        root.Add(grid, 0, wx.EXPAND | wx.ALL, 12)

        self.autoSpeakCB = _add_autospeak_checkbox(
            pnl,
            root,
            "autoTestOnChangeOrpheus",
            default=bool(self.initial.get("autoTest", True)),
        )

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(pnl, label=_("&Test"))
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
        self.helpBtn = _create_help_button(pnl)
        btnRow.Add(self.helpBtn, 0, wx.RIGHT, 8)
        btnRow.AddStretchSpacer(1)
        self.okBtn = wx.Button(pnl, wx.ID_OK)
        self.cancelBtn = wx.Button(pnl, wx.ID_CANCEL)
        try:
            self.okBtn.SetDefault()
        except Exception:
            pass
        btnRow.Add(self.okBtn, 0, wx.RIGHT, 8)
        btnRow.Add(self.cancelBtn, 0)
        root.Add(btnRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        pnl.SetSizer(root)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(pnl, 1, wx.EXPAND)
        self.SetSizerAndFit(s)

        self._langs: List[object] = []
        self._variants: List[object] = []

        self._populate_languages()
        self._apply_initial()
        self._populate_voices_for_selected_language()

        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)


        self._auto_test = _debounced_call(lambda: self._on_test(None), delay_ms=250)

        def _maybe_auto(evt):
            try:
                if self.autoSpeakCB.GetValue():
                    self._auto_test()
            except Exception:
                pass
            try:
                evt.Skip()
            except Exception:
                pass

        self.langChoice.Bind(wx.EVT_CHOICE, _maybe_auto)
        self.voiceChoice.Bind(wx.EVT_CHOICE, _maybe_auto)
        self.speedSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        self.pitchSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        self.volumeSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        _bind_numeric_page_keys(self.speedSpin, 20, 400, page_step=10, callback=self._on_speed_change)
        _bind_numeric_page_keys(self.pitchSpin, 0, 100, page_step=10, callback=self._on_pitch_change)
        _bind_numeric_page_keys(self.volumeSpin, 0, 100, page_step=10, callback=self._on_volume_change)

        self.Bind(wx.EVT_CHOICE, self._on_lang_change, self.langChoice)
        self.Bind(wx.EVT_CHOICE, self._on_voice_change, self.voiceChoice)
        self.Bind(wx.EVT_SPINCTRL, self._on_speed_change, self.speedSpin)
        self.Bind(wx.EVT_SPINCTRL, self._on_pitch_change, self.pitchSpin)
        self.Bind(wx.EVT_SPINCTRL, self._on_volume_change, self.volumeSpin)

    def _maybe_auto_test(self):
        try:
            if self.autoSpeakCB.GetValue():
                self._on_test(None)
        except Exception:
            pass

    def _on_test(self, evt):
        try:
            self.apply_to_synth()
            if hasattr(self.synth, "speak"):
                self.synth.speak([self.SAMPLE_TEXT])
        except Exception:
            pass

    def _on_lang_change(self, evt):
        self._populate_voices_for_selected_language()
        self._maybe_auto_test()

    def _on_voice_change(self, evt):
        self._maybe_auto_test()

    def _on_speed_change(self, evt):
        self._maybe_auto_test()

    def _on_pitch_change(self, evt):
        self._maybe_auto_test()

    def _on_volume_change(self, evt):
        self._maybe_auto_test()

    def _populate_languages(self):
        self.langChoice.Clear()
        self._langs = []
        voices = []
        try:
            voices = voice_utils.normalise_voice_infos(getattr(self.synth, "availableVoices", None))
        except Exception:
            voices = []
        if not voices:
            self.langChoice.Append(_("Default"), clientData="")
            self.langChoice.SetSelection(0)
            return
        for i, vi in enumerate(voices):
            label = voice_utils.orpheus_friendly_label(vi, fallback=f"Language {i+1}")
            vid = str(getattr(vi, "id", "") or str(i))
            self.langChoice.Append(label, clientData=vid)
            self._langs.append(vi)
        self.langChoice.SetSelection(0)

    def _apply_initial(self):
        # language
        lang_id = self.initial.get("languageId", None)
        if lang_id is not None:
            for i in range(self.langChoice.GetCount()):
                if str(self.langChoice.GetClientData(i)) == str(lang_id):
                    self.langChoice.SetSelection(i)
                    break
        # speed
        try:
            sp = self.initial.get("speed", None)
            if sp is not None:
                self.speedSpin.SetValue(int(sp))
        except Exception:
            pass
        try:
            pitch = self.initial.get("pitch", None)
            if pitch is not None:
                self.pitchSpin.SetValue(max(0, min(100, int(pitch))))
        except Exception:
            pass
        try:
            volume = self.initial.get("volume", None)
            if volume is not None:
                self.volumeSpin.SetValue(max(0, min(100, int(volume))))
        except Exception:
            pass

    def _populate_voices_for_selected_language(self):
        self.voiceChoice.Clear()
        lang_id = self.get_language_id()
        try:
            if hasattr(self.synth, "voice"):
                self.synth.voice = lang_id
        except Exception:
            pass

        variants = []
        try:
            variants = voice_utils.normalise_voice_infos(getattr(self.synth, "availableVariants", None))
        except Exception:
            variants = []
        self._variants = variants or []

        if not variants:
            self.voiceChoice.Append(_("Default"), clientData="")
            self.voiceChoice.SetSelection(0)
            return

        for i, vi in enumerate(variants):
            label = voice_utils.orpheus_friendly_label(vi, fallback=f"Voice {i+1}")
            vid = str(getattr(vi, "id", "") or str(i))
            self.voiceChoice.Append(label, clientData=vid)
        self.voiceChoice.SetSelection(0)

        # apply initial variant
        var_id = self.initial.get("variantId", None)
        if var_id is not None:
            for i in range(self.voiceChoice.GetCount()):
                if str(self.voiceChoice.GetClientData(i)) == str(var_id):
                    self.voiceChoice.SetSelection(i)
                    break

    def get_language_id(self) -> str:
        i = self.langChoice.GetSelection()
        if i == wx.NOT_FOUND:
            return ""
        return str(self.langChoice.GetClientData(i) or "")

    def get_variant_id(self) -> str:
        i = self.voiceChoice.GetSelection()
        if i == wx.NOT_FOUND:
            return ""
        return str(self.voiceChoice.GetClientData(i) or "")

    def get_speed(self) -> int:
        try:
            return int(self.speedSpin.GetValue())
        except Exception:
            return 100

    def get_pitch(self) -> int:
        try:
            return max(0, min(100, int(self.pitchSpin.GetValue())))
        except Exception:
            return 50

    def get_volume(self) -> int:
        try:
            return max(0, min(100, int(self.volumeSpin.GetValue())))
        except Exception:
            return 100

    def apply_to_synth(self):
        try:
            if hasattr(self.synth, "voice"):
                self.synth.voice = self.get_language_id()
        except Exception:
            pass
        try:
            if hasattr(self.synth, "variant"):
                self.synth.variant = self.get_variant_id()
        except Exception:
            pass
        try:
            if hasattr(self.synth, "rate"):
                self.synth.rate = int(self.get_speed())
        except Exception:
            pass
        try:
            if hasattr(self.synth, "pitch"):
                self.synth.pitch = int(self.get_pitch())
        except Exception:
            pass
        try:
            volume = int(self.get_volume())
            if hasattr(self.synth, "volume"):
                self.synth.volume = volume
            if hasattr(self.synth, "_volume"):
                self.synth._volume = volume
        except Exception:
            pass

    def get_options(self, persist: bool = True) -> Dict[str, object]:
        opts = {
            "languageId": self.get_language_id(),
            "voiceLabel": _choice_label(self.langChoice),
            "variantId": self.get_variant_id(),
            "variantLabel": _choice_label(self.voiceChoice),
            "speed": self.get_speed(),
            "pitch": self.get_pitch(),
            "volume": self.get_volume(),
            "autoTest": bool(self.autoSpeakCB.GetValue()),
        }
        if persist:
            _cfg_set("orpheusLanguageId", opts["languageId"])
            _cfg_set("orpheusVariantId", opts["variantId"])
            _cfg_set("orpheusSpeed", int(opts["speed"]))
            _cfg_set("orpheusPitch", int(opts["pitch"]))
            _cfg_set("orpheusVolume", int(opts["volume"]))
            _cfg_set("autoTestOnChangeOrpheus", bool(opts["autoTest"]))
        return opts

# ----------------------------
# Orpheus capture renderer
# ----------------------------
def _apply_orpheus_volume(audio_data: bytes, synth) -> bytes:
    """Match the Orpheus driver volume scaling without feeding live playback."""
    try:
        vol = int(getattr(synth, "_volume", 100))
    except Exception:
        vol = 100
    if vol <= 0:
        return b"\x00" * len(audio_data)
    if vol >= 100:
        return audio_data
    try:
        samples = array.array("h")
        samples.frombytes(audio_data)
        factor = vol / 100.0
        for i, sample in enumerate(samples):
            value = int(sample * factor)
            if value > 32767:
                value = 32767
            elif value < -32768:
                value = -32768
            samples[i] = value
        return samples.tobytes()
    except Exception:
        return audio_data


def _iter_orpheus_controls(controls):
    """Yield Orpheus control triples from either wrapper payloads or raw callback bytes."""
    if not controls:
        return
    if isinstance(controls, (bytes, bytearray)):
        usable = len(controls) - (len(controls) % 12)
        for triple in struct.iter_unpack("III", bytes(controls[:usable])):
            yield triple
        return
    for item in controls:
        try:
            pos, typ, value = item[:3]
            yield int(pos), int(typ), int(value)
        except Exception:
            continue


def _render_with_orpheus_wrapper_capture(text: str, out_wav: str, synth) -> str:
    """Capture audio from the 2026+ Orpheus wrapper driver event path.

    The 64-bit NVDA driver no longer owns the Orpheus DLL. It receives audio from
    orpheus-host.exe via _on_audio(), so capturing that event is the reliable
    offline-render hook.
    """
    if not hasattr(synth, "speak") or not hasattr(synth, "_on_audio"):
        raise RuntimeError(_("Orpheus wrapper capture is not available in this driver."))

    sample_rate = 22050
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    done_evt = threading.Event()
    frames_written = 0
    old_on_audio = getattr(synth, "_on_audio")

    wf = wave.open(out_wav, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sample_rate)

    def _capturing_on_audio(payload):
        nonlocal frames_written
        try:
            audio_data = payload.get("audio", b"") if isinstance(payload, dict) else b""
            controls = payload.get("controls", []) if isinstance(payload, dict) else []
            if audio_data:
                audio_data = _apply_orpheus_volume(bytes(audio_data), synth)
                wf.writeframesraw(audio_data)
                frames_written += len(audio_data) // 2
            for _pos, _typ, value in _iter_orpheus_controls(controls):
                if value & 0x80000000:
                    try:
                        synth.is_speaking = False
                    except Exception:
                        pass
                    try:
                        synth._work_event.set()
                    except Exception:
                        pass
                    done_evt.set()
                    break
        except Exception:
            log.error("soundWave: Orpheus wrapper capture callback failed", exc_info=True)

    try:
        synth._on_audio = _capturing_on_audio
        synth.speak([text or ""])
        if not done_evt.wait(TIMEOUT_SECONDS):
            raise RuntimeError(_("Orpheus render timed out waiting for end-of-string marker."))
    finally:
        try:
            synth._on_audio = old_on_audio
        except Exception:
            pass
        try:
            wf.close()
        except Exception:
            pass

    if frames_written <= 0 or not os.path.exists(out_wav):
        raise RuntimeError(_("Orpheus render failed: no audio was captured."))
    return "Orpheus wrapper capture"


def _render_with_orpheus_dll_capture(text: str, out_wav: str, synth) -> str:
    if ctypes is None or WINFUNCTYPE is None:
        raise RuntimeError(_("ctypes not available; cannot render with Orpheus."))

    if not getattr(synth, "lib", None):
        raise RuntimeError(_("Orpheus DLL not loaded."))
    if not hasattr(synth.lib, "TTS_SetAudioMethod"):
        raise RuntimeError(_("Orpheus DLL missing TTS_SetAudioMethod; cannot capture audio."))
    if not hasattr(synth, "speak"):
        raise RuntimeError(_("Orpheus driver missing speak(); cannot render."))

    sample_rate = 22050
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    done_evt = threading.Event()
    frames_written = 0

    wf = wave.open(out_wav, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sample_rate)

    old_callback = getattr(synth, "callback", None)

    @WINFUNCTYPE(DWORD, c_void_p, DWORD, c_void_p, DWORD)
    def cb(buf, written, ctrlbuf, ctrls):
        nonlocal frames_written
        try:
            byte_count = int(written) * 2
            if byte_count > 0:
                wf.writeframesraw(ctypes.string_at(buf, byte_count))
                frames_written += int(written)
            if ctrls and int(ctrls) > 0:
                cbytes = ctypes.string_at(ctrlbuf, int(ctrls) * 12)
                for (_pos, _typ, value) in struct.iter_unpack("III", cbytes):
                    if value & 0x80000000:
                        done_evt.set()
                        break
        except Exception:
            pass
        return 0

    try:
        synth.callback = cb
        synth.lib.TTS_SetAudioMethod(1, cb)
        synth.speak([text or ""])
        if not done_evt.wait(TIMEOUT_SECONDS):
            raise RuntimeError(_("Orpheus render timed out waiting for end-of-string marker."))
    finally:
        try:
            wf.close()
        except Exception:
            pass
        try:
            if old_callback is not None:
                synth.callback = old_callback
                synth.lib.TTS_SetAudioMethod(1, old_callback)
        except Exception:
            pass

    if frames_written <= 0 or not os.path.exists(out_wav):
        raise RuntimeError(_("Orpheus render failed: no audio was captured."))
    return "Orpheus DLL capture"


def _render_with_orpheus_capture(text: str, out_wav: str, orpheusSynth, opts: Optional[dict] = None) -> str:
    synth = orpheusSynth

    # Apply requested options (best-effort)
    if opts:
        try:
            if hasattr(synth, "voice") and "languageId" in opts:
                synth.voice = str(opts.get("languageId") or "")
        except Exception:
            pass
        try:
            if hasattr(synth, "variant") and "variantId" in opts:
                synth.variant = str(opts.get("variantId") or "")
        except Exception:
            pass
        try:
            if hasattr(synth, "rate") and "speed" in opts:
                synth.rate = int(opts.get("speed") or 100)
        except Exception:
            pass
        try:
            if hasattr(synth, "pitch") and "pitch" in opts:
                synth.pitch = int(opts.get("pitch") or 50)
        except Exception:
            pass
        try:
            if "volume" in opts:
                volume = max(0, min(100, int(opts.get("volume") or 100)))
                if hasattr(synth, "volume"):
                    synth.volume = volume
                if hasattr(synth, "_volume"):
                    synth._volume = volume
        except Exception:
            pass

    try:
        if hasattr(synth, "_on_audio"):
            return _render_with_orpheus_wrapper_capture(text, out_wav, synth)
    except Exception:
        log.error("soundWave: Orpheus wrapper capture failed", exc_info=True)
        if not getattr(synth, "lib", None):
            raise

    return _render_with_orpheus_dll_capture(text, out_wav, synth)


# ----------------------------
