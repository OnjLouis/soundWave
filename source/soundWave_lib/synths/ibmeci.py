# -*- coding: utf-8 -*-
from __future__ import annotations

import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from typing import List, Optional

import ui
import wx

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())

# --- IBM ECI voice metadata (used to present friendly names when .SYN files are available) ---
# Many ECI/Eloquence drops ship language voices as <CODE>.SYN beside ECI.DLL.
# IDs below follow common ECI language IDs used by Eloquence/ECI builds.
_ECI_LANGS = {
    "esm": (131073, "Latin American Spanish"),
    "esp": (131072, "Castilian Spanish"),
    "ptb": (458752, "Brazilian Portuguese"),
    "frc": (196609, "French Canadian"),
    "fra": (196608, "French"),
    "fin": (589824, "Finnish"),
    "deu": (262144, "German"),
    "ita": (327680, "Italian"),
    "enu": (65536, "American English"),
    "eng": (65537, "British English"),
    "chs": (393216, "Mandarin Chinese"),
    "jpn": (524288, "Japanese"),
    "kor": (655360, "Korean"),
}

def _eci_enumerate_voices_from_syn(dll_path):
    """Return a list of (voiceId:int, label:str). Includes Default (0) first."""
    items = [(0, "Default (0)")]
    try:
        base_dir = os.path.dirname(os.path.abspath(dll_path))
        for fn in os.listdir(base_dir):
            if not fn.lower().endswith(".syn"):
                continue
            code = fn.lower()[:-4]
            info = _ECI_LANGS.get(code)
            if not info:
                continue
            vid, name = info
            items.append((int(vid), f"{name} ({vid})"))
    except Exception:
        pass
    # de-dup + stable sort (keep Default first)
    seen = set()
    out = []
    for vid, label in items:
        if vid in seen:
            continue
        seen.add(vid)
        out.append((vid, label))
    if len(out) > 1:
        out = [out[0]] + sorted(out[1:], key=lambda x: x[1].lower())
    return out

class IbmEciOptionsDialog(wx.Dialog):
    """Eloquence/IBMTTS options: voice and speed."""
    SAMPLE_TEXT = "This is a voice and speed test."

    def __init__(self, parent, initial=None):
        super().__init__(parent, title="soundWave - IBM ECI options")
        self.initial = initial or {}
        if not self.initial.get("dllPath"):
            found_dll = _find_ibmeci_dll()
            if found_dll:
                self.initial["dllPath"] = found_dll
        self.dllPath = str(self.initial.get("dllPath", "") or "")

        pnl = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        # Primary controls: Voice + Speed
        grid = wx.FlexGridSizer(rows=2, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        # Voice (friendly list from .SYN when available)
        grid.Add(wx.StaticText(pnl, label="&Voice:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._voiceItems = _eci_enumerate_voices_from_syn(self.initial.get("dllPath", "") or "")
        self.voiceChoice = wx.Choice(pnl, choices=[lbl for (_vid, lbl) in self._voiceItems])
        grid.Add(self.voiceChoice, 1, wx.EXPAND)

        # Speed
        grid.Add(wx.StaticText(pnl, label="&Speed:"), 0, wx.ALIGN_CENTER_VERTICAL)
        speedRow = wx.BoxSizer(wx.HORIZONTAL)
        self.speedSpin = wx.SpinCtrl(pnl, min=0, max=250, initial=int(self.initial.get("speed", 110) or 110))
        speedRow.Add(self.speedSpin, 0, wx.RIGHT, 8)
        speedRow.Add(wx.StaticText(pnl, label="(0–250; default ~110)"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(speedRow, 0, wx.ALIGN_LEFT)

        # Apply initial voice selection (by stored voiceId)
        try:
            init_vid = int(self.initial.get("voiceId", 0) or 0)
        except Exception:
            init_vid = 0
        sel = 0
        for i, (vid, _lbl) in enumerate(self._voiceItems):
            try:
                if int(vid) == init_vid:
                    sel = i
                    break
            except Exception:
                continue
        try:
            self.voiceChoice.SetSelection(sel)
        except Exception:
            pass

        root.Add(grid, 0, wx.EXPAND | wx.ALL, 12)

        self.autoSpeakCB = _add_autospeak_checkbox(pnl, root, "autoTestOnChangeIbmEci", default=True)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(pnl, label="&Test")
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
        self.voiceChoice.Bind(wx.EVT_CHOICE, _maybe_auto)
        self.speedSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        _bind_numeric_page_keys(self.speedSpin, 0, 250, page_step=10, callback=_maybe_auto)

    def _on_test(self, evt):
        dll_path = (self.dllPath or "").strip()
        if not dll_path or not os.path.isfile(dll_path):
            _error("SoundWave could not find the installed Eloquence/IBMTTS speech engine.")
            return
        try:
            tmp_dir = tempfile.mkdtemp(prefix="soundWave_test_")
            tmp_wav = os.path.join(tmp_dir, "test.wav")
            cancel_evt = threading.Event()
            _render_with_ibmeci_dll(
                text=self.SAMPLE_TEXT,
                out_wav=tmp_wav,
                dll_path=dll_path,
                voice_id=int(self.get_options().get("voiceId", 0)),
                sample_rate_param=2,
                speed=int(self.speedSpin.GetValue()),
                progress=None,
                cancel_evt=cancel_evt,
            )
            _play_wav(tmp_wav)
            _defer_delete_dir(tmp_dir, tmp_wav)
        except Exception as e:
            _error(f"Test failed:\n{e}")

    def get_options(self):
        return {
            "dllPath": (self.dllPath or "").strip(),
            "voiceId": int(self._voiceItems[self.voiceChoice.GetSelection()][0]) if self._voiceItems and self.voiceChoice.GetSelection() >= 0 else 0,
            "voiceLabel": _choice_label(self.voiceChoice),
            "speed": int(self.speedSpin.GetValue()),
            "autoTest": bool(self.autoSpeakCB.GetValue()) if hasattr(self, "autoSpeakCB") else True,
        }

# ----------------------------


# IBM ECI renderer (DLL)
# ----------------------------
_ECIMessage_eciWaveformBuffer = 0
_ECIMessage_eciIndexReply = 2
_END_STRING_MARK = 0xFFFF

def _find_ibmeci_dll(preferred_addon: str = "") -> str:
    """Find a bundled IBM ECI DLL from installed Eloquence/IBMTTS add-ons."""
    candidates = []
    addon_names = []
    preferred_addon = (preferred_addon or "").strip()
    if preferred_addon:
        addon_names.append(preferred_addon)
    for name in ("Eloquence", "IBMTTS"):
        if name.lower() not in [x.lower() for x in addon_names]:
            addon_names.append(name)
    try:
        addons_dir = os.path.join(os.path.expandvars("%APPDATA%"), "nvda", "addons")
        for addon_name in addon_names:
            base = os.path.join(addons_dir, addon_name)
            candidates.append(os.path.join(base, "synthDrivers", "eloquence", "ECI.DLL"))
            candidates.append(os.path.join(base, "synthDrivers", "ibmtts", "ECI.DLL"))
            candidates.append(os.path.join(base, "synthDrivers", "ibmtts", "ibmeci", "ECI.DLL"))
    except Exception:
        pass
    try:
        import addonHandler
        addon = addonHandler.getCodeAddon()
        if addon:
            root = getattr(addon, "path", "") or ""
            if root:
                candidates.append(os.path.join(root, "synthDrivers", "ibmtts", "ECI.DLL"))
    except Exception:
        pass
    for path in candidates:
        try:
            if path and os.path.isfile(path):
                return path
        except Exception:
            pass
    try:
        addons_dir = os.path.join(os.path.expandvars("%APPDATA%"), "nvda", "addons")
        for addon_name in addon_names:
            for path in glob.glob(os.path.join(addons_dir, addon_name, "**", "ECI.DLL"), recursive=True):
                if os.path.isfile(path):
                    return path
    except Exception:
        pass
    return ""


def _pe_machine_type(path: str) -> int:
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return 0
            f.seek(0x3C)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            f.seek(pe_offset)
            if f.read(4) != b"PE\x00\x00":
                return 0
            return struct.unpack("<H", f.read(2))[0]
    except Exception:
        return 0


def _is_32bit_dll(path: str) -> bool:
    return _pe_machine_type(path) == 0x014C


def _render_with_ibmeci_proxy32(text: str, out_wav: str, dll_path: str, voice_id: int = 0, sample_rate_param: int = 2, speed: int = 110, progress: Optional[dict] = None, cancel_evt: Optional[threading.Event] = None):
    """Render IBM ECI through the installed IBMTTS 32-bit host bridge."""
    from ctypes import string_at
    try:
        from synthDrivers._proxyEci import EciDLL
    except Exception as e:
        raise RuntimeError("IBMTTS 32-bit proxy is not available; install or update the IBMTTS add-on.") from e

    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"
    if not dll_path or not os.path.isfile(dll_path):
        raise RuntimeError("The Eloquence/IBMTTS speech engine could not be found.")

    samples = 3300
    pcm_rate = 11025
    if progress is not None:
        try:
            progress.setdefault("buffers", 0)
            progress.setdefault("bytes", 0)
            progress.setdefault("last_audio_ts", None)
            progress.setdefault("started_ts", time.time())
            progress.setdefault("pcm_rate", int(pcm_rate))
            progress.setdefault("channels", 1)
            progress.setdefault("sampwidth", 2)
            progress["usingIbmttsProxy32"] = True
        except Exception:
            pass

    done_evt = threading.Event()
    first_audio_evt = threading.Event()
    err_holder = {"err": None}
    buffer_ptr = {"ptr": None}

    wf = wave.open(out_wav, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(int(pcm_rate))

    def eci_callback(h, ms, lp, dt):
        try:
            if int(ms) in (0, _ECIMessage_eciWaveformBuffer):
                n = int(lp)
                ptr = buffer_ptr.get("ptr")
                if n > 0 and ptr:
                    data = string_at(ptr, n * 2)
                    wf.writeframesraw(data)
                    first_audio_evt.set()
                    try:
                        if progress is not None:
                            progress["buffers"] = int(progress.get("buffers", 0)) + 1
                            progress["bytes"] = int(progress.get("bytes", 0)) + len(data)
                            progress["last_audio_ts"] = time.time()
                    except Exception:
                        pass
            elif int(ms) in (2, _ECIMessage_eciIndexReply) and int(lp) == _END_STRING_MARK:
                done_evt.set()
        except Exception as e:
            err_holder["err"] = e
            done_evt.set()
        return 1

    dll = None
    handle = None
    try:
        dll = EciDLL(dll_path)
        try:
            handle = dll.eciNewEx(int(voice_id))
        except Exception:
            handle = 0
        if not handle:
            handle = dll.eciNewEx(0)
        if not handle:
            raise RuntimeError("eciNewEx failed (no handle).")

        try:
            req_vid = int(voice_id)
        except Exception:
            req_vid = 0
        if req_vid:
            try:
                dll.eciSetVoiceParam(handle, 0, 9, req_vid)
            except Exception:
                pass
        try:
            sp = max(0, min(250, int(speed)))
        except Exception:
            sp = 110
        try:
            dll.eciSetVoiceParam(handle, 0, 6, int(sp))
        except Exception:
            pass

        dll.eciRegisterCallback(handle, eci_callback, None)
        dll.eciSetOutputBuffer(handle, samples)
        buffer_ptr["ptr"] = dll.get_audio_buffer_ptr()
        if not buffer_ptr["ptr"]:
            raise RuntimeError("IBMTTS proxy did not expose an audio buffer.")

        try: dll.eciSetParam(handle, 0, 1)
        except Exception: pass
        try: dll.eciSetParam(handle, 1, 1)
        except Exception: pass

        dll.eciAddText(handle, (text or "").encode("mbcs", errors="replace"))
        dll.eciInsertIndex(handle, _END_STRING_MARK)
        dll.eciSynthesize(handle)

        startup_deadline = time.time() + 5.0
        while not first_audio_evt.is_set():
            if err_holder["err"] is not None:
                raise err_holder["err"]
            if cancel_evt is not None and cancel_evt.is_set():
                try: dll.eciStop(handle)
                except Exception: pass
                raise RuntimeError("Cancelled.")
            if done_evt.is_set():
                break
            if time.time() >= startup_deadline:
                raise RuntimeError("IBM ECI proxy produced no audio.")
            time.sleep(0.02)

        deadline = time.time() + float(TIMEOUT_SECONDS)
        while not done_evt.is_set():
            if err_holder["err"] is not None:
                raise err_holder["err"]
            if cancel_evt is not None and cancel_evt.is_set():
                try: dll.eciStop(handle)
                except Exception: pass
                raise RuntimeError("Cancelled.")
            if time.time() >= deadline:
                raise RuntimeError("IBM ECI proxy render timed out.")
            try:
                last = progress.get("last_audio_ts") if progress is not None else None
                if last and time.time() - float(last) > 3.0:
                    break
            except Exception:
                pass
            time.sleep(0.02)
    finally:
        try:
            wf.close()
        except Exception:
            pass
        try:
            if dll is not None and handle:
                dll.eciDelete(handle)
        except Exception:
            pass

    if not os.path.isfile(out_wav) or os.path.getsize(out_wav) <= 44:
        raise RuntimeError("IBM ECI proxy render failed: no audio was captured.")


def _render_with_ibmeci_dll(text: str, out_wav: str, dll_path: str, voice_id: int = 0, sample_rate_param: int = 2, speed: int = 110, progress: Optional[dict] = None, cancel_evt: Optional[threading.Event] = None):
    # IBM ECI DLL renderer using a dedicated host thread (mirrors IBMTTS design).
    # Some ECI builds only start delivering waveform buffers reliably when synthesis runs
    # in a thread with a Windows message queue.
    from ctypes import byref, create_string_buffer, c_int, c_void_p, pointer, string_at, windll, wintypes, WINFUNCTYPE
    import os
    import time
    import threading
    import wave

    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"
    if not dll_path or not os.path.isfile(dll_path):
        raise RuntimeError("The Eloquence/IBMTTS speech engine could not be found.")
    if _is_32bit_dll(dll_path) and sys.maxsize > 2**32:
        return _render_with_ibmeci_proxy32(
            text=text,
            out_wav=out_wav,
            dll_path=dll_path,
            voice_id=voice_id,
            sample_rate_param=sample_rate_param,
            speed=speed,
            progress=progress,
            cancel_evt=cancel_evt,
        )

    samples = 3300
    buffer = create_string_buffer(samples * 2)

    rate_map = {0: 8000, 1: 11025, 2: 11025}
    pcm_rate = rate_map.get(int(sample_rate_param), 11025)

    # progress init
    if progress is not None:
        try:
            progress.setdefault("buffers", 0)
            progress.setdefault("bytes", 0)
            progress.setdefault("last_audio_ts", None)
            progress.setdefault("started_ts", time.time())
            # For progress UI (rendered seconds + realtime factor)
            progress.setdefault("pcm_rate", int(pcm_rate))
            progress.setdefault("channels", 1)
            progress.setdefault("sampwidth", 2)
        except Exception:
            pass

    first_audio_evt = threading.Event()
    done_evt = threading.Event()
    err_holder = {"err": None}

    def _eci_thread_main():
        user32 = windll.user32
        msg = wintypes.MSG()
        # Create a message queue for this thread
        try:
            user32.PeekMessageA(byref(msg), None, 0, 0, 0)
        except Exception:
            pass

        dll = windll.LoadLibrary(dll_path)

        # Prototypes (minimum)
        dll.eciNewEx.argtypes = [c_int]
        dll.eciNewEx.restype = c_void_p
        dll.eciDelete.argtypes = [c_void_p]
        dll.eciDelete.restype = None
        dll.eciRegisterCallback.argtypes = [c_void_p, c_void_p, c_void_p]
        dll.eciRegisterCallback.restype = None  # void
        dll.eciSetOutputBuffer.argtypes = [c_void_p, c_int, c_void_p]
        dll.eciSetOutputBuffer.restype = c_int  # 1 success, 0 fail
        dll.eciSetParam.argtypes = [c_void_p, c_int, c_int]
        dll.eciSetParam.restype = c_int
        # Optional voice parameter setter (preferred for speed)
        try:
            dll.eciSetVoiceParam.argtypes = [c_void_p, c_int, c_int, c_int]
            dll.eciSetVoiceParam.restype = c_int
        except Exception:
            pass
        # Optional voice selector (lets us init with voice 0 then switch)
        try:
            dll.eciSetVoice.argtypes = [c_void_p, c_int]
            dll.eciSetVoice.restype = c_int
        except Exception:
            # Some builds don't export eciSetVoice
            pass

        dll.eciAddText.argtypes = [c_void_p, c_void_p]
        dll.eciAddText.restype = c_int
        dll.eciInsertIndex.argtypes = [c_void_p, c_int]
        dll.eciInsertIndex.restype = c_int
        dll.eciSynthesize.argtypes = [c_void_p]
        dll.eciSynthesize.restype = c_int
        dll.eciStop.argtypes = [c_void_p]
        dll.eciStop.restype = c_int

        wf = None
        handle = None
        wrote_any = {"v": False}

        @WINFUNCTYPE(c_int, c_int, c_int, c_int, c_int, c_void_p)
        def eciCallback(h, ms, lp, dt, userData):
            try:
                if int(ms) in (0, _ECIMessage_eciWaveformBuffer):
                    n = int(lp)
                    if n > 0 and wf is not None:
                        wf.writeframesraw(string_at(buffer, n * 2))
                        wrote_any["v"] = True
                        first_audio_evt.set()
                        try:
                            if progress is not None:
                                progress["buffers"] = int(progress.get("buffers", 0)) + 1
                                progress["bytes"] = int(progress.get("bytes", 0)) + int(n * 2)
                                progress["last_audio_ts"] = time.time()
                        except Exception:
                            pass
                elif int(ms) in (2, _ECIMessage_eciIndexReply) and int(lp) == _END_STRING_MARK:
                    done_evt.set()
            except Exception as e:
                try:
                    err_holder["err"] = e
                    done_evt.set()
                except Exception:
                    pass
            return 1

        try:
            wf = wave.open(out_wav, "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(pcm_rate))

            handle = dll.eciNewEx(int(voice_id))
            if not handle and int(voice_id) != 0:
                # Some ECI builds fail to init for non-zero voice IDs.
                # Retry with voice 0 so we at least render, rather than failing hard.
                try:
                    handle = dll.eciNewEx(0)
                    if progress is not None and handle:
                        progress["voiceIdInitFallbackTo0"] = True
                except Exception:
                    pass
            if not handle:
                raise RuntimeError("eciNewEx failed (no handle).")

            # If we had to fall back to voice 0 for initialization, try switching to the requested voice now.
            # Many ECI builds only initialize reliably with voice 0, but *can* switch voices post-init.
            voice_switch_ok = False
            voice_switch_attempted = False
            try:
                req_vid = int(voice_id)
            except Exception:
                req_vid = 0
            if req_vid != 0:
                voice_switch_attempted = True
                # Try a few known ECI ways to select voice after initialization.
                # Different ECI/Eloquence drops expose different symbols.
                try:
                    if hasattr(dll, "eciSetVoice"):
                        rv = int(dll.eciSetVoice(handle, req_vid))
                        voice_switch_ok = (rv != 0)
                    else:
                        voice_switch_ok = False
                except Exception:
                    voice_switch_ok = False

                if not voice_switch_ok:
                    # Some builds don't export eciSetVoice, but will accept voice via param 9.
                    try:
                        if hasattr(dll, "eciSetVoiceParam"):
                            rv = int(dll.eciSetVoiceParam(handle, 0, 9, req_vid))
                            voice_switch_ok = (rv != 0)
                    except Exception:
                        pass

                if not voice_switch_ok:
                    try:
                        if hasattr(dll, "eciSetParam"):
                            rv = int(dll.eciSetParam(handle, 9, req_vid))
                            voice_switch_ok = (rv != 0)
                    except Exception:
                        pass
                if progress is not None:
                    progress["eciVoiceSwitchAttempted"] = True
                    progress["eciVoiceSwitchOk"] = bool(voice_switch_ok)
                # If switching isn't possible, keep going but make it explicit (test/render will sound like voice 0).
                if not voice_switch_ok:
                    try:
                        ui.message(_("ECI voice switching not supported; using voice 0."))
                    except Exception:
                        pass

            # Voice speed (ECI voice parameter: eciSpeed = 6). Range: 0-250.
            try:
                sp = int(speed)
            except Exception:
                sp = 110
            if sp < 0:
                sp = 0
            if sp > 250:
                sp = 250
            try:
                # voiceNumber=0 refers to the current voice.
                if hasattr(dll, 'eciSetVoiceParam'):
                    dll.eciSetVoiceParam(handle, 0, 6, int(sp))
            except Exception:
                pass

            _cb_ref = eciCallback  # keep alive
            dll.eciRegisterCallback(handle, eciCallback, None)

            ok = int(dll.eciSetOutputBuffer(handle, int(samples), pointer(buffer)))
            if not ok:
                raise RuntimeError("eciSetOutputBuffer failed (no callback/buffer accepted).")

            # Critical params (IBMTTS)
            try: dll.eciSetParam(handle, 0, 1)  # eciSynthMode
            except Exception: pass
            try: dll.eciSetParam(handle, 1, 1)  # eciInputType
            except Exception: pass
            # try: dll.eciSetParam(handle, 5, int(sample_rate_param))  # eciSampleRate  # disabled: DLL appears to output fixed rate
            except Exception: pass

            b = (text or "").encode("mbcs", errors="replace")
            dll.eciAddText(handle, b)
            dll.eciInsertIndex(handle, _END_STRING_MARK)
            dll.eciSynthesize(handle)

            start_time = time.time()

            # Message pump + cancel loop until done
            while not done_evt.is_set():
                if cancel_evt is not None and cancel_evt.is_set():
                    try: dll.eciStop(handle)
                    except Exception: pass
                    raise RuntimeError("Cancelled.")
                # hard timeout safeguard
                if (time.time() - start_time) > TIMEOUT_SECONDS:
                    done_evt.set()
                    raise RuntimeError("IBM ECI render timed out.")
                # pump any pending messages (keeps some ECI builds happy)
                try:
                    while user32.PeekMessageA(byref(msg), None, 0, 0, 1):
                        user32.TranslateMessage(byref(msg))
                        user32.DispatchMessageA(byref(msg))
                except Exception:
                    pass
                
                # Watchdog: if we've produced audio but haven't received any callbacks recently,
                # assume synthesis has finished even if the index reply wasn't delivered.
                try:
                    if progress is not None and progress.get("last_audio_ts") and first_audio_evt.is_set():
                        if (time.time() - float(progress.get("last_audio_ts"))) > 3.0:
                            done_evt.set()
                except Exception:
                    pass
                time.sleep(0.01)

            if not wrote_any["v"]:
                raise RuntimeError("IBM ECI produced no audio (no waveform buffers).")
        except Exception as e:
            err_holder["err"] = e
        finally:
            try:
                if wf is not None:
                    wf.close()
            except Exception:
                pass
            try:
                if handle:
                    dll.eciDelete(handle)
            except Exception:
                pass

    t = threading.Thread(target=_eci_thread_main, name="soundWave-ECI", daemon=True)
    t.start()

    # Fail fast if we never receive any waveform buffers
    startup_deadline = time.time() + 2.0
    while True:
        if err_holder["err"] is not None:
            raise err_holder["err"]
        if cancel_evt is not None and cancel_evt.is_set():
            raise RuntimeError("Cancelled.")
        if first_audio_evt.is_set():
            break
        if time.time() >= startup_deadline:
            raise RuntimeError("IBM ECI produced no audio (no buffers within 2 seconds).")
        time.sleep(0.02)

    # Wait for completion or error (TIMEOUT_SECONDS)
    deadline = time.time() + float(TIMEOUT_SECONDS)
    while True:
        if err_holder["err"] is not None:
            raise err_holder["err"]
        if done_evt.is_set():
            break
        if cancel_evt is not None and cancel_evt.is_set():
            raise RuntimeError("Cancelled.")
        if time.time() >= deadline:
            raise RuntimeError("IBM ECI render timed out.")
        time.sleep(0.05)
