# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import wave
from typing import Any, Dict, List, Optional, Tuple

import wx

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())

# DECtalk offline rendering + options
# ----------------------------
def _has_dectalk() -> bool:
    try:
        import synthDrivers.dectalk  # noqa: F401
        return True
    except Exception:
        return False


# Friendly DECtalk voice list (matches common Windows DECtalk voices).
_DECTALK_VOICES: List[Tuple[str, str]] = [
    ("Paul", "Perfect Paul"),
    ("Betty", "Beautiful Betty"),
    ("Harry", "Huge Harry"),
    ("Frank", "Frail Frank"),
    ("Dennis", "Doctor Dennis"),
    ("Kit", "Kit The Kid"),
    ("Ursula", "Uppity Ursula"),
    ("Rita", "Rough Rita"),
    ("Wendy", "Whispering Wendy"),
]


class DectalkOptionsDialog(wx.Dialog):
    def __init__(self, parent, initial: Optional[Dict[str, Any]] = None):
        super().__init__(parent, title="soundWave - DECtalk options")
        initial = initial or {}

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Voice (primary)
        rowV = wx.BoxSizer(wx.HORIZONTAL)
        rowV.Add(wx.StaticText(self, label="&Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voiceChoice = wx.Choice(self, choices=[name for _, name in _DECTALK_VOICES])
        init_voice = str(initial.get("voice", "") or _cfg_get("dectalkVoice", "Paul") or "Paul")
        codes = [c for c, _ in _DECTALK_VOICES]
        try:
            self.voiceChoice.SetSelection(max(0, codes.index(init_voice)))
        except Exception:
            self.voiceChoice.SetSelection(0)
        rowV.Add(self.voiceChoice, 1, wx.EXPAND)
        sizer.Add(rowV, 0, wx.EXPAND | wx.ALL, 10)

        # Rate (primary)
        rowR = wx.BoxSizer(wx.HORIZONTAL)
        rowR.Add(wx.StaticText(self, label="&Rate:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        init_rate = int(initial.get("rate", _cfg_get("dectalkRate", 180) or 180) or 180)
        self.rateSpin = wx.SpinCtrl(self, min=75, max=650, initial=init_rate)
        rowR.Add(self.rateSpin, 0)
        sizer.Add(rowR, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Auto test
        self.autoTest = wx.CheckBox(self, label="&Auto speak when changing settings")
        self.autoTest.SetValue(bool(initial.get("autoTest", _cfg_get_bool("autoTestOnChangeDectalk", True))))
        sizer.Add(self.autoTest, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Buttons row: Test + OK/Cancel
        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(self, label="&Test")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
        self.helpBtn = _create_help_button(self)
        btnRow.Add(self.helpBtn, 0, wx.RIGHT, 8)
        btnRow.AddStretchSpacer(1)
        okCancel = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        btnRow.Add(okCancel, 0, wx.EXPAND)
        sizer.Add(btnRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizer(sizer)
        self.Fit()
        self.SetMinSize((520, -1))

        # Events
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)
        self.voiceChoice.Bind(wx.EVT_CHOICE, self._on_changed)
        self.rateSpin.Bind(wx.EVT_SPINCTRL, self._on_changed)
        self.rateSpin.Bind(wx.EVT_TEXT, self._on_changed)
        _bind_numeric_page_keys(self.rateSpin, 75, 650, page_step=25, callback=self._on_changed)

    def _get_selected_voice_code(self) -> str:
        idx = self.voiceChoice.GetSelection()
        if idx < 0:
            idx = 0
        return _DECTALK_VOICES[idx][0]

    def _on_changed(self, evt):
        if self.autoTest.IsChecked():
            self._on_test(None)
        try:
            if evt is not None:
                evt.Skip()
        except Exception:
            pass

    def _on_test(self, evt):
        sample = "This is a DECtalk test."
        tmp_dir = tempfile.mkdtemp(prefix="soundWave_dectalk_test_")
        tmp_wav = os.path.join(tmp_dir, "test.wav")
        try:
            _render_with_dectalk_offline(
                text=sample,
                out_wav=tmp_wav,
                voice=self._get_selected_voice_code(),
                rate=int(self.rateSpin.GetValue()),
                cancel_evt=threading.Event(),
                progress=None,
            )
            _play_wav_file(tmp_wav)
        except Exception as e:
            _error(f"DECtalk test failed:\n{e}")
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    def get_options(self, persist: bool = True) -> Dict[str, Any]:
        opts = {
            "voice": self._get_selected_voice_code(),
            "voiceLabel": _choice_label(self.voiceChoice),
            "rate": int(self.rateSpin.GetValue()),
            "autoTest": bool(self.autoTest.IsChecked()),
        }
        if persist:
            _cfg_set("dectalkVoice", str(opts["voice"]))
            _cfg_set("dectalkRate", int(opts["rate"]))
            _cfg_set("autoTestOnChangeDectalk", bool(opts["autoTest"]))
        return opts


# Minimal ctypes bindings for DECtalk "Open in Memory" rendering.
# Mirrors the NVDA synth driver approach but writes PCM to WAV.
import ctypes
from ctypes import (
    byref,
    c_char,
    c_int,
    c_void_p,
    cast,
    cdll,
    create_string_buffer,
    POINTER,
    pointer,
    sizeof,
    string_at,
    Structure,
    windll,
)
from ctypes.wintypes import DWORD, MSG
import wave


_DECTALK_FORMAT = 0x00000004  # 11025Hz mono 16-bit
_DECTALK_SAMPLES = 2048
_DECTALK_INDEX_ARRAY_SIZE = 1


class _DT_TTS_INDEX_T(Structure):
    _fields_ = [
        ("dwIndexValue", DWORD),
        ("dwIndexSampleNumber", DWORD),
        ("dwReserved", DWORD),
    ]


class _DT_TTS_BUFFER_T(Structure):
    _fields_ = [
        ("lpData", POINTER(c_char * (_DECTALK_SAMPLES * 2))),
        ("lpPhonemeArray", c_void_p),
        ("lpIndexArray", POINTER(_DECTALK_INDEX_ARRAY_SIZE * _DT_TTS_INDEX_T)),
        ("dwMaximumBufferLength", DWORD),
        ("dwMaximumNumberOfPhonemeChanges", DWORD),
        ("dwMaximumNumberOfIndexMarks", DWORD),
        ("dwBufferLength", DWORD),
        ("dwNumberOfPhonemeChanges", DWORD),
        ("dwNumberOfIndexMarks", DWORD),
        ("dwReserved", DWORD),
    ]


class _WNDCLASSEXW(Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("style", DWORD),
        ("lpfnWndProc", c_void_p),
        ("cbClsExtra", c_int),
        ("cbWndExtra", c_int),
        ("hInstance", c_void_p),
        ("hIcon", c_void_p),
        ("hCursor", c_void_p),
        ("hbrBackground", c_void_p),
        ("lpszMenuName", c_void_p),
        ("lpszClassName", c_void_p),
        ("hIconSm", c_void_p),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)


def _render_with_dectalk_offline(
    text: str,
    out_wav: str,
    voice: str = "Paul",
    rate: int = 180,
    cancel_evt: Optional[threading.Event] = None,
    progress: Optional[Dict[str, Any]] = None,
) -> str:
    # Offline render using DECtalk DLL (OpenInMemory) and a hidden message window.
    cancel_evt = cancel_evt or threading.Event()

    try:
        import synthDrivers.dectalk as _dt
        dll_path = getattr(_dt, "DECTALK_PATH", "") or ""
    except Exception:
        dll_path = ""

    if not dll_path or not os.path.isfile(dll_path):
        raise RuntimeError("DECtalk addon not installed or dectalk.dll not found.")

    dll_dir = os.path.dirname(os.path.abspath(dll_path))
    dectalk = cdll.LoadLibrary(dll_path)

    def _errcheck(res, func, args):
        if res != 0:
            raise RuntimeError("%s: code %d" % (getattr(func, "__name__", "DECtalkCall"), res))
        return res

    for fn in (
        "TextToSpeechStartup",
        "TextToSpeechSpeak",
        "TextToSpeechAddBuffer",
        "TextToSpeechOpenInMemory",
        "TextToSpeechShutdown",
    ):
        try:
            getattr(dectalk, fn).errcheck = _errcheck
        except Exception:
            pass

    try:
        windll.kernel32.SetDllDirectoryW(dll_dir)
    except Exception:
        pass

    cwd0 = os.getcwd()
    try:
        os.chdir(dll_dir)
    except Exception:
        pass

    pcm = bytearray()
    done = {"flag": False}

    appInstance = windll.kernel32.GetModuleHandleW(None)
    cls_name = "soundWaveDtMsgWnd"
    cls_name_w = ctypes.c_wchar_p(cls_name)

    wmIndex = windll.user32.RegisterWindowMessageW("DECtalkIndexMessage")
    wmBuffer = windll.user32.RegisterWindowMessageW("DECtalkBufferMessage")

    handle = c_void_p()

    @_WNDPROC
    def wndproc(hwnd, msg, wParam, lParam):
        try:
            if msg == wmBuffer:
                lpBuffer = cast(lParam, POINTER(_DT_TTS_BUFFER_T))
                buf_len = int(lpBuffer.contents.dwBufferLength)
                if buf_len:
                    pcm.extend(string_at(lpBuffer.contents.lpData, buf_len))
                if buf_len == 0:
                    done["flag"] = True
                lpBuffer.contents.dwBufferLength = 0
                try:
                    dectalk.TextToSpeechAddBuffer(handle, lpBuffer)
                except Exception:
                    pass
                return 0
            if msg == wmIndex:
                # Some builds use index-reply 32000 for end.
                try:
                    idx = int(cast(lParam, POINTER(_DT_TTS_INDEX_T)).contents.dwIndexValue)
                    if idx == 32000:
                        done["flag"] = True
                except Exception:
                    pass
                return 0
        except Exception:
            done["flag"] = True
        return windll.user32.DefWindowProcW(hwnd, msg, wParam, lParam)

    wc = _WNDCLASSEXW()
    wc.cbSize = sizeof(wc)
    wc.hInstance = appInstance
    wc.lpszClassName = ctypes.cast(cls_name_w, c_void_p)
    wc.lpfnWndProc = ctypes.cast(wndproc, c_void_p)
    atom = windll.user32.RegisterClassExW(byref(wc))
    if not atom:
        raise RuntimeError("DECtalk: could not register message window class.")

    hwnd = windll.user32.CreateWindowExW(
        0,
        atom,
        cls_name_w,
        0,
        0, 0, 0, 0,
        None, None,
        appInstance,
        None,
    )
    if not hwnd:
        raise RuntimeError("DECtalk: could not create message window.")

    # DO_NOT_USE_AUDIO_DEVICE = 0x80000000
    dectalk.TextToSpeechStartup(hwnd, byref(handle), -1, 0x80000000)

    # Prepare in-memory buffer
    mem_buffer = _DT_TTS_BUFFER_T()
    index_array = (_DT_TTS_INDEX_T * _DECTALK_INDEX_ARRAY_SIZE)()
    buf = create_string_buffer(_DECTALK_SAMPLES * 2)
    mem_buffer.lpData = pointer(buf)
    mem_buffer.lpIndexArray = pointer(index_array)
    mem_buffer.dwMaximumBufferLength = _DECTALK_SAMPLES * 2
    mem_buffer.dwMaximumNumberOfIndexMarks = _DECTALK_INDEX_ARRAY_SIZE

    dectalk.TextToSpeechOpenInMemory(handle, _DECTALK_FORMAT)
    dectalk.TextToSpeechAddBuffer(handle, byref(mem_buffer))

    # Set voice + rate (best-effort)
    try:
        dectalk.TextToSpeechSpeak(handle, f"[:name {voice[0].lower()}]".encode("latin-1"), 1)
    except Exception:
        pass
    try:
        r = max(75, min(650, int(rate)))
        dectalk.TextToSpeechSpeak(handle, b"[:rate %d]" % r, 1)
    except Exception:
        pass

    speak_text = (text or "") + " [:index reply 32000]"
    dectalk.TextToSpeechSpeak(handle, speak_text.encode("iso8859-1", "ignore"), 1)

    msg = MSG()
    last_msg_ts = time.time()
    while not done["flag"] and not cancel_evt.is_set():
        while windll.user32.PeekMessageW(byref(msg), hwnd, 0, 0, 1):
            windll.user32.TranslateMessage(byref(msg))
            windll.user32.DispatchMessageW(byref(msg))
            last_msg_ts = time.time()
        time.sleep(0.001)
        if (time.time() - last_msg_ts) > 3.0 and len(pcm) > 0:
            # No messages for a while - assume done
            done["flag"] = True

    try:
        dectalk.TextToSpeechShutdown(handle)
    except Exception:
        pass
    try:
        windll.user32.DestroyWindow(hwnd)
    except Exception:
        pass
    try:
        windll.user32.UnregisterClassW(atom, appInstance)
    except Exception:
        pass

    try:
        os.chdir(cwd0)
    except Exception:
        pass
    try:
        windll.kernel32.SetDllDirectoryW(None)
    except Exception:
        pass

    if cancel_evt.is_set():
        raise RuntimeError("Cancelled.")

    # Write WAV
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(11025)
        wf.writeframes(bytes(pcm))

    return "DECTALK"


# ----------------------------
