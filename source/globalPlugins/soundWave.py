# -*- coding: utf-8 -*-
"""
soundWave - NVDA global plugin (single-file script)

Merged build (Sonata offline + Orpheus/ECI/SAPI offline) with FTR metrics and per-synth record paths.

What this build does:
- Offline/FTR rendering for:
  - Sonata Neural Voices (gRPC): renders to WAV without hijacking NVDA live speech.
  - Orpheus (DLL capture): renders to WAV using driver's capture callback.
  - SAPI5 (COM): renders to WAV; optionally choose a SAPI voice (covers Eloquence/DecTalk/BestSpeech if installed as SAPI voices).
  - IBM ECI (DLL): renders to WAV when SOUNDWAVE_IBMECI_DLL env var is set.

- Faster-than-realtime (FTR) is computed when a WAV is produced (wall_time / wav_duration).
- Record path includes a per-synth subfolder under a configurable base directory.
- Test buttons:
  - Sonata options dialog includes a "Test" button (renders and plays a short sample using the selected voice/speed).
  - SAPI5 options dialog includes a "Test" button (renders and plays a short sample using selected voice/rate).

Notes / limits:
- Many NVDA synth drivers do not expose an offline/file render API. Those will show as "not supported for offline/FTR" here.
- MP3 export is offered if ffmpeg is on PATH.

Gesture:
  NVDA+Control+=  -> Render (offline when supported; includes FTR stats)

Drop into:
  addons\\soundWave\\globalPlugins\\soundWave.py
"""

from __future__ import annotations

import os
import glob
import array
import importlib
import re
import sys
import time
import json
import types
import wave
import shutil
import struct
import pathlib
import tempfile
import threading
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any

import wx
import ctypes
from ctypes import cdll, windll  # needed for DECtalk loader


def _normalize_renderer(sel):
    """Map UI selection / synth ids to internal renderer ids."""
    if sel is None:
        return None
    s = str(sel).strip().lower()
    aliases = {
        "sapi": "sapi5",
        "sapi5": "sapi5",
        "microsoft sapi 5": "sapi5",
        "sonata": "sonata",
        "orpheus": "orpheus",
        "ibmtts": "ibmeci",
        "ibm tts": "ibmeci",
        "ibm eci": "ibmeci",
        "eloquence": "ibmeci",
        "dectalk": "dectalk",
        "bestspeech": "sapi5",
    }
    return aliases.get(s, str(sel))

def _ensure_dir(path: str) -> None:
    """Create directory if it doesn't exist (idempotent)."""
    if not path:
        return
    os.makedirs(path, exist_ok=True)

def _get_cfg_bool(key: str, default: bool = False) -> bool:
    """Read a boolean from NVDA config under [soundWave], with fallback."""
    try:
        import config
        sect = config.conf.get('soundWave', {})
        return bool(sect.get(key, default))
    except Exception:
        return bool(default)

def _set_cfg_bool(key: str, value: bool) -> None:
    """Persist a boolean to NVDA config under [soundWave]."""
    try:
        import config
        if 'soundWave' not in config.conf:
            config.conf['soundWave'] = {}
        config.conf['soundWave'][key] = bool(value)
        config.conf.save()
    except Exception:
        pass

import globalPluginHandler
import gui
import ui
import api
import config
import synthDriverHandler
from scriptHandler import script
from logHandler import log
# Translation / _
try:
    import addonHandler
    addonHandler.initTranslation()
except Exception:
    pass
try:
    from gettext import gettext as _
except Exception:
    _ = lambda s: s
try:
    from ._onjGithubUpdater import GitHubReleaseUpdater
except Exception:
    GitHubReleaseUpdater = None


def _normalize_synth_list_item(item):
    """Return (synthId, label) from NVDA synth list items across NVDA versions.

    NVDA has historically returned tuples like (id, label) but some builds/addons
    return (label, id). We detect by a heuristic:
      - ids are usually lowercase and contain no spaces
      - labels often contain spaces / uppercase.
    """
    try:
        a, b = item[0], item[1]
    except Exception:
        return ("", "")
    a = "" if a is None else str(a)
    b = "" if b is None else str(b)

    def _looks_like_id(x: str) -> bool:
        x2 = x.strip()
        if not x2:
            return False
        if " " in x2:
            return False
        # NVDA synth ids are typically ascii-ish and lowercase
        return x2.lower() == x2

    if _looks_like_id(a) and not _looks_like_id(b):
        return (a, b)
    if _looks_like_id(b) and not _looks_like_id(a):
        return (b, a)
    # fallback: assume (id, label)
    return (a, b)


def _resolve_synth_id(requested: str):
    """Resolve a synth driver id by id or display label substring."""
    try:
        lst = synthDriverHandler.getSynthList()
    except Exception:
        lst = None
    if not lst:
        return requested

    req = (requested or "").strip()
    if not req:
        return requested

    # First: exact id match
    for item in lst:
        sid, label = _normalize_synth_list_item(item)
        if (sid or "").lower() == req.lower():
            return sid

    # Second: label substring match
    for item in lst:
        sid, label = _normalize_synth_list_item(item)
        if req.lower() in (label or "").lower():
            return sid

    return requested


def _get_synth_instance(name: str):
    """Best-effort creation of an NVDA synth driver instance across NVDA versions.

    Strategy:
      1) Use synthDriverHandler.getSynthInstance if available.
      2) Import synthDrivers.<id> and instantiate SynthDriver directly.
      3) Fall back to synthDriverHandler internals if present.
    """
    sid = _resolve_synth_id(name)

    # 1) Public helper (present in some NVDA versions)
    try:
        fn = getattr(synthDriverHandler, "getSynthInstance", None)
        if callable(fn):
            return fn(sid)
    except Exception:
        pass

    # 2) Direct import (works well for addon synths)
    try:
        mod = importlib.import_module(f"synthDrivers.{sid}")
        drv_cls = getattr(mod, "SynthDriver", None)
        if isinstance(drv_cls, type):
            inst = drv_cls()
            try:
                if hasattr(inst, "initialize") and callable(getattr(inst, "initialize")):
                    inst.initialize()
            except Exception:
                pass
            return inst
    except Exception:
        pass

    # 3) Private helpers (best-effort)
    for attr in ("_getSynthDriver", "getSynthDriver", "_getSynthDriverInstance"):
        try:
            fn = getattr(synthDriverHandler, attr, None)
            if callable(fn):
                drv = fn(sid)
                inst = drv() if isinstance(drv, type) else drv
                try:
                    if hasattr(inst, "initialize") and callable(getattr(inst, "initialize")):
                        inst.initialize()
                except Exception:
                    pass
                return inst
        except Exception:
            continue

    return None

def _cfg_get_bool(key: str, default: bool = False) -> bool:
    v = _cfg_get(key, None)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")

def _add_autospeak_checkbox(parent, sizer, key: str, default: bool = True):
    cb = wx.CheckBox(parent, label="Auto speak when changing settings")
    cb.SetValue(_cfg_get_bool(key, default))
    def _on_toggle(evt):
        _cfg_set(key, bool(cb.GetValue()))
        evt.Skip()
    cb.Bind(wx.EVT_CHECKBOX, _on_toggle)
    sizer.Add(cb, 0, wx.ALL, 5)
    return cb

def _debounced_call(fn, delay_ms: int = 250):
    state = {"timer": None}
    def _call(*args, **kwargs):
        try:
            if state["timer"] is not None:
                state["timer"].Stop()
        except Exception:
            pass
        def _run():
            try:
                fn(*args, **kwargs)
            except Exception:
                pass
        state["timer"] = wx.CallLater(delay_ms, _run)
    return _call



try:
    import nvwave
except Exception:
    nvwave = None

try:
    import comtypes
    import comtypes.client
except Exception:
    comtypes = None

try:
    import ctypes
    from ctypes import WINFUNCTYPE, c_void_p, c_int
    from ctypes.wintypes import DWORD
except Exception:
    ctypes = None
    WINFUNCTYPE = None
    DWORD = None

import addonHandler
addonHandler.initTranslation()
from gettext import gettext as _


ADDON_NAME = "soundWave"
CFG_SECTION = "soundWave"
TIMEOUT_SECONDS = 300
ORPHEUS_FALLBACK_SYNTH = "espeak"
RENDER_CHUNK_CHARS = 12000
CHUNK_RENDER_MIN_CHARS = 24000
MAX_WAV_DATA_BYTES = 3600 * 1024 * 1024


def _get_current_synth_name() -> str:
    try:
        s = synthDriverHandler.getSynth()
        return (getattr(s, "name", "") or "").lower()
    except Exception:
        return ""


def _safe_set_synth(name_or_instance) -> bool:
    """Best-effort synth switch.

    NVDA's synthDriverHandler.setSynth sometimes accepts a name and sometimes accepts
    an instance, depending on version. This helper tries both.
    """
    try:
        synthDriverHandler.setSynth(name_or_instance)
        return True
    except Exception:
        pass
    # If we were given a name, try to fetch an instance.
    try:
        if isinstance(name_or_instance, str):
            inst = synthDriverHandler.getSynthInstance(name_or_instance)
            synthDriverHandler.setSynth(inst)
            return True
    except Exception:
        pass
    return False

# Registry entries you asked to include in record paths.
# These are treated as *labels*; for SAPI5 we can auto-select voices by name when present.
SYNTH_REGISTRY = {
    "Sonata": {"id": "sonata", "recordSubdir": "Sonata", "supportsFTR": True},
    "Orpheus": {"id": "orpheus", "recordSubdir": "Orpheus", "supportsFTR": True},
    "SAPI5": {"id": "sapi5", "recordSubdir": "SAPI5", "supportsFTR": True},
    "IBM ECI": {"id": "ibmeci", "recordSubdir": "IBM_ECI", "supportsFTR": True},

    # requested names (typically SAPI voices on most systems)
    "BestSpeech": {"id": "bestspeech", "recordSubdir": "BestSpeech", "supportsFTR": True},
    "Keynote Gold": {"id": "bestspeech", "recordSubdir": "KeynoteGold", "supportsFTR": True},
    "Eloquence": {"id": "eloquence", "recordSubdir": "Eloquence", "supportsFTR": True},
    "DecTalk": {"id": "dectalk", "recordSubdir": "DecTalk", "supportsFTR": True},
    "DECtalk": {"id": "dectalk", "recordSubdir": "DecTalk", "supportsFTR": True},
}


# ----------------------------
# Config helpers
# ----------------------------
def _cfg_get(key: str, default=None):
    try:
        sec = config.conf.get(CFG_SECTION)
        if sec is None:
            return default
        return sec.get(key, default)
    except Exception:
        return default


def _cfg_set(key: str, value):
    try:
        if CFG_SECTION not in config.conf:
            config.conf[CFG_SECTION] = {}
        config.conf[CFG_SECTION][key] = value
        try:
            config.conf.save()
        except Exception:
            pass
    except Exception:
        pass


def _safe_config_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_") or "default"


def _safe_getattr(obj, name: str, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


# ----------------------------
# UI helpers
# ----------------------------
def _show_modal(dlg: wx.Dialog) -> int:
    """Reliable dialog pattern across NVDA builds."""
    res = wx.ID_CANCEL
    mf = getattr(gui, "mainFrame", None)
    _install_enter_to_ok(dlg)
    try:
        if mf:
            try:
                mf.prePopup()
            except Exception:
                pass
        try:
            res = dlg.ShowModal()
        except RuntimeError:
            res = wx.ID_CANCEL
        finally:
            if mf:
                try:
                    mf.postPopup()
                except Exception:
                    pass
    except Exception:
        res = wx.ID_CANCEL
    return res


def _install_enter_to_ok(dlg: wx.Dialog):
    if getattr(dlg, "_soundWaveEnterHookInstalled", False):
        return
    try:
        dlg._soundWaveEnterHookInstalled = True
    except Exception:
        pass

    def _on_char_hook(evt):
        key = evt.GetKeyCode()
        if key not in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            evt.Skip()
            return
        focus = wx.Window.FindFocus()
        try:
            if isinstance(focus, wx.Button):
                evt.Skip()
                return
            if isinstance(focus, wx.TextCtrl) and (focus.GetWindowStyleFlag() & wx.TE_MULTILINE):
                evt.Skip()
                return
        except Exception:
            pass
        try:
            ok = dlg.FindWindowById(wx.ID_OK)
            if ok and ok.IsEnabled():
                dlg.EndModal(wx.ID_OK)
                return
        except Exception:
            pass
        evt.Skip()

    try:
        dlg.Bind(wx.EVT_CHAR_HOOK, _on_char_hook)
    except Exception:
        pass


def _info(msg: str, title: str = ADDON_NAME):
    try:
        gui.messageBox(msg, title, wx.OK | wx.ICON_INFORMATION)
    except Exception:
        ui.message(msg)


def _error(msg: str, title: str = ADDON_NAME):
    try:
        gui.messageBox(msg, title, wx.OK | wx.ICON_ERROR)
    except Exception:
        ui.message(msg)


def _get_clipboard_text() -> str:
    # Prefer api.getClipData for NVDA, fall back to wx clipboard.
    try:
        txt = api.getClipData() or ""
        if txt:
            return txt
    except Exception:
        pass

    txt = ""
    try:
        if wx.TheClipboard.Open():
            try:
                data = wx.TextDataObject()
                if wx.TheClipboard.GetData(data):
                    txt = data.GetText() or ""
            finally:
                wx.TheClipboard.Close()
    except Exception:
        txt = ""
    return txt



def _pick_input_text(parent) -> tuple[str, str]:
    """Pick input text for rendering.

    Returns (text, baseLabel) where baseLabel is 'clipboard', a file base name, or 'typed'.
    """
    clip = _get_clipboard_text().strip()
    if clip:
        try:
            res = gui.messageBox(
                "Use the current clipboard text as input?\n\n"
                f"Preview (first 200 chars):\n{clip[:200]}",
                ADDON_NAME,
                wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION,
            )
        except Exception:
            res = wx.YES
        if res == wx.YES:
            return clip, "clipboard"
        if res == wx.CANCEL:
            return "", ""

    try:
        source_dlg = wx.SingleChoiceDialog(
            parent,
            "Choose input source:",
            ADDON_NAME,
            ["Open text file", "Type or paste text"],
        )
        try:
            if _show_modal(source_dlg) != wx.ID_OK:
                return "", ""
            input_source = source_dlg.GetSelection()
        finally:
            try:
                source_dlg.Destroy()
            except Exception:
                pass
    except Exception:
        input_source = 1

    if input_source == 0:
        last_input_dir = str(_cfg_get("lastInputDir", "") or "")
        if not last_input_dir or not os.path.isdir(os.path.expandvars(os.path.expanduser(last_input_dir))):
            last_input_dir = os.path.expanduser("~")
        fd = wx.FileDialog(
            parent,
            message="Open text to render",
            defaultDir=os.path.expandvars(os.path.expanduser(last_input_dir)),
            wildcard="Text files (*.txt)|*.txt|Markdown files (*.md)|*.md|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if _show_modal(fd) != wx.ID_OK:
                return "", ""
            in_path = fd.GetPath()
        finally:
            try:
                fd.Destroy()
            except Exception:
                pass
        try:
            _cfg_set("lastInputDir", os.path.dirname(in_path))
        except Exception:
            pass
        txt = None
        last_err = None
        for enc in ("utf-8-sig", "utf-16", "mbcs"):
            try:
                with open(in_path, "r", encoding=enc) as f:
                    txt = f.read()
                break
            except Exception as e:
                last_err = e
        if txt is None:
            _error(f"Couldn't read input file:\n{last_err}")
            return "", ""
        txt = txt.strip()
        if not txt:
            _error("The selected text file is empty.")
            return "", ""
        return txt, os.path.splitext(os.path.basename(in_path))[0] or "file"

    # Ask for manual input (multi-line).
    try:
        dlg = wx.TextEntryDialog(
            parent,
            "Enter text to render:",
            ADDON_NAME,
            value=clip if clip else "",
            style=wx.TE_MULTILINE | wx.OK | wx.CANCEL,
        )
        try:
            if _show_modal(dlg) != wx.ID_OK:
                return "", ""
            txt = (dlg.GetValue() or "").strip()
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass
    except Exception as e:
        _error(f"Couldn't open input dialog: {e}")
        return "", ""

    if not txt:
        return "", ""
    return txt, "typed"

class _RenderProgressDialog(wx.Dialog):
    """Common render progress dialog used for all synths.

    Provides a pulsing gauge plus an optional Details view that updates once per second.
    """
    def __init__(self, parent, title: str = "soundWave"):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._cancelled = False
        self._detailsShown = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Summary (non-tabbable heading; details field is the tabbable read-only control)
        self.summary = wx.StaticText(self, label="Rendering…")
        # Slightly larger font for readability
        try:
            f = self.summary.GetFont()
            f.PointSize = max(f.PointSize + 2, f.PointSize)
            self.summary.SetFont(f)
        except Exception:
            pass
        sizer.Add(self.summary, 0, wx.ALL | wx.EXPAND, 10)

        self.gauge = wx.Gauge(self, range=1, style=wx.GA_HORIZONTAL)
        try:
            self.gauge.SetName(_('Progress'))
        except Exception:
            pass
        self.gauge.Pulse()
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # Details (hidden by default)
        self.details = wx.TextCtrl(
            self,
            value="",
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
            size=(-1, 120),
        )
        self.details.Hide()
        self.details.Disable()
        self.details.SetValue("Rendering…")
        sizer.Add(self.details, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        btnSizer = wx.BoxSizer(wx.HORIZONTAL)

        self.detailsBtn = wx.Button(self, label="Show details")
        self.detailsBtn.Bind(wx.EVT_BUTTON, self._on_toggle_details)
        btnSizer.Add(self.detailsBtn, 0, wx.ALL, 10)

        btnSizer.AddStretchSpacer(1)

        self.cancelBtn = wx.Button(self, label="Cancel")
        self.cancelBtn.Bind(wx.EVT_BUTTON, self._on_cancel)
        btnSizer.Add(self.cancelBtn, 0, wx.ALL, 10)

        sizer.Add(btnSizer, 0, wx.EXPAND)

        self.SetSizer(sizer)
        self.SetMinSize((520, 200))
        self.Fit()

        # Allow ESC to cancel
        try:
            self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        except Exception:
            pass

        # Tab order: Show/Hide details -> Cancel (and when details are shown: Details -> Show/Hide details -> Cancel)
        try:            self.cancelBtn.MoveAfterInTabOrder(self.detailsBtn)
        except Exception:
            pass

    @property
    def cancelled(self) -> bool:
        return bool(self._cancelled)

    def _on_char_hook(self, evt):
        try:
            if evt.GetKeyCode() == wx.WXK_ESCAPE:
                # Mirror Cancel button behavior
                self._on_cancel(None)
                return
        except Exception:
            pass
        try:
            evt.Skip()
        except Exception:
            pass

    def _on_cancel(self, evt):
        # Mark cancelled and request the worker to stop.
        self._cancelled = True
        try:
            self.set_summary("Cancelling…")
        except Exception:
            pass
        try:
            # If the dialog was given a shared cancel event, signal it.
            ce = getattr(self, "_cancel_evt", None)
            if ce is not None:
                ce.set()
        except Exception:
            pass
        try:
            self.cancelBtn.Disable()
        except Exception:
            pass
        # Do not trap the user in the progress dialog: close the UI promptly.
        # The worker may still be unwinding in the background, but the dialog exits.
        self._closing = True
        wx.CallAfter(self.Destroy)

    def _on_toggle_details(self, evt):
        self._detailsShown = not self._detailsShown
        if self._detailsShown:
            self.details.Enable()
            # Populate immediately so the first focus landing isn't an empty field.
            if not self.details.GetValue().strip():
                try:
                    self.details.SetValue(str(self.summary.GetLabel() or "Rendering…"))
                except Exception:
                    self.details.SetValue("Rendering…")
            self.details.Show()
            self.detailsBtn.SetLabel("Hide details")
            try:
                self.detailsBtn.MoveAfterInTabOrder(self.details)
                self.cancelBtn.MoveAfterInTabOrder(self.detailsBtn)
            except Exception:
                pass
            try:
                self.details.SetFocus()
            except Exception:
                pass
        else:
            self.details.Hide()
            self.details.Disable()
            self.detailsBtn.SetLabel("Show details")
            try:
                self.cancelBtn.MoveAfterInTabOrder(self.detailsBtn)
            except Exception:
                pass
        self.Layout()
        self.Fit()

    def set_summary(self, text: str):
        try:
            self.summary.SetValue(text)
        except Exception:
            try:
                self.summary.SetLabel(text)
            except Exception:
                pass
        try:
            self.gauge.Pulse()
        except Exception:
            pass

    def set_details_lines(self, lines: List[str]):
        try:
            self.details.SetValue("\n".join([str(x) for x in (lines or [])]))
        except Exception:
            pass

def _play_wav(path: str) -> str:
    """
    Best-effort WAV playback for test buttons.
    Returns the playback method used: "nvwave", "winsound", or "shell".
    """
    if not path or not os.path.exists(path):
        return "none"

    # 1) Prefer nvwave (in-house NVDA playback)
    if nvwave is not None:
        try:
            with wave.open(path, "rb") as wf:
                ch = wf.getnchannels()
                sr = wf.getframerate()
                sw = wf.getsampwidth()
            player = nvwave.WavePlayer(channels=ch, samplesPerSec=sr, bitsPerSample=sw * 8)
            # nvwave can play straight from file
            player.play(path)
            return "nvwave"
        except Exception:
            pass

    # 2) Fall back to winsound (still in-process; no media player UI)
    try:
        import winsound  # type: ignore
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return "winsound"
    except Exception:
        pass

    # 3) Last resort: shell open (default media player)
    try:
        os.startfile(path)  # type: ignore[attr-defined]
        return "shell"
    except Exception:
        return "none"


def _play_wav_file(path: str) -> str:
    """Backward-compatible alias used by some synth test handlers."""
    return _play_wav(path)


def _defer_delete_dir(dirpath: str, wav_for_duration: Optional[str] = None, extra_seconds: float = 2.0):
    """
    Defer deleting a temp folder long enough for async playback methods to read the file.
    Uses WAV duration if available; otherwise falls back to a safe delay.
    """
    delay_ms = int((10.0 + extra_seconds) * 1000)
    if wav_for_duration and os.path.exists(wav_for_duration):
        try:
            d = _wav_duration_seconds(wav_for_duration)
            delay_ms = int((max(1.0, d) + extra_seconds) * 1000)
        except Exception:
            pass

    def _do():
        try:
            shutil.rmtree(dirpath, ignore_errors=True)
        except Exception:
            pass

    try:
        wx.CallLater(delay_ms, _do)
    except Exception:
        # If wx isn't available for any reason, just don't delete immediately.
        pass


def _pick_fallback_synth_name() -> Optional[str]:
    try:
        available = [n for n, _d in _list_nvda_synths()]
    except Exception:
        available = []
    for cand in ("espeak", "espeak-ng", "espeakng"):
        if cand in available:
            return cand
    # As a last resort, pick the first synth if present
    for cand in available:
        if (cand or "").lower() != "orpheus":
            return cand
    return None


def _with_temporary_nvda_synth(temp_synth_name: str, fn):
    """
    Temporarily switch NVDA's live synth (global) while running fn(), then restore.
    This is used when a synth can't be instantiated for offline capture while it's active.
    """
    old = None
    try:
        old = synthDriverHandler.getSynth().name
    except Exception:
        old = None

    switched = False
    try:
        if temp_synth_name and old and old != temp_synth_name:
            try:
                synthDriverHandler.setSynth(temp_synth_name)
                switched = True
            except Exception:
                switched = False
        return fn()
    finally:
        if switched and old:
            try:
                synthDriverHandler.setSynth(old)
            except Exception:
                pass


def _get_record_base_dir() -> str:
    base = _cfg_get("recordBaseDir", "") or ""
    return os.path.expandvars(os.path.expanduser(base))


def _build_default_output_dir(synth_label: str) -> str:
    last_dir = str(_cfg_get("lastOutputDir", "") or "")
    if last_dir:
        last_dir = os.path.expandvars(os.path.expanduser(last_dir))
        if os.path.isdir(last_dir):
            return last_dir

    base = _get_record_base_dir()
    if not base:
        return os.path.expanduser("~")
    sub = SYNTH_REGISTRY.get(synth_label, {}).get("recordSubdir", "")
    if not sub:
        sub = re.sub(r"[^A-Za-z0-9._-]+", "_", str(synth_label or "UnknownSynth")).strip("_") or "UnknownSynth"
    out_dir = os.path.join(base, sub)
    _ensure_dir(out_dir)
    return out_dir


def _safe_filename_piece(value: str, fallback: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    return value[:80]


def _build_suggested_output_base(input_base: str, synth_label: str) -> str:
    synth_part = _safe_filename_piece(synth_label, "Synth")
    input_part = _safe_filename_piece(input_base or "output", "output")
    if input_part.lower() == synth_part.lower():
        return input_part
    return f"{input_part} - {synth_part}"


def _pick_output_path(parent, suggestion_base: str, synth_label: str):
    has_ffmpeg = bool(shutil.which("ffmpeg"))
    wildcard = "WAV audio (*.wav)|*.wav"
    if has_ffmpeg:
        wildcard += "|MP3 audio (*.mp3)|*.mp3"

    default_dir = _build_default_output_dir(synth_label)
    default_file = f"{suggestion_base}.wav"

    fd = wx.FileDialog(
        parent,
        message="Save audio as…",
        defaultDir=default_dir,
        defaultFile=default_file,
        wildcard=wildcard,
        style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
    )
    try:
        if _show_modal(fd) != wx.ID_OK:
            return (None, None)
        out_path = fd.GetPath()
        filt = fd.GetFilterIndex()
    finally:
        try:
            fd.Destroy()
        except Exception:
            pass

    ext = os.path.splitext(out_path)[1].lower()
    if ext in (".wav", ".mp3"):
        fmt = ext[1:]
    else:
        if has_ffmpeg and filt == 1:
            fmt = "mp3"
            out_path += ".mp3"
        else:
            fmt = "wav"
            out_path += ".wav"

    if fmt == "mp3" and not has_ffmpeg:
        _error("MP3 requires ffmpeg on PATH. Please choose WAV.")
        return (None, None)

    try:
        out_dir = os.path.dirname(out_path)
        if out_dir:
            _cfg_set("lastOutputDir", out_dir)
    except Exception:
        pass

    return (out_path, fmt)


# ----------------------------
# SAPI5 offline rendering + options
# ----------------------------
def _list_sapi5_voices() -> List[str]:
    if comtypes is None:
        return []
    comtypes.CoInitialize()
    try:
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        voices = voice.GetVoices()
        names = []
        for i in range(int(voices.Count)):
            v = voices.Item(i)
            desc = v.GetDescription()
            if desc:
                names.append(str(desc))
        return names
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _render_with_sapi5(text: str, out_wav: str, voice_name: Optional[str] = None, rate: int = 0):
    if comtypes is None:
        raise RuntimeError("comtypes not available; cannot use SAPI5 renderer.")
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    comtypes.CoInitialize()
    try:
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        try:
            voice.Rate = int(rate)
        except Exception:
            pass

        if voice_name:
            try:
                voices = voice.GetVoices()
                chosen = None
                for i in range(int(voices.Count)):
                    v = voices.Item(i)
                    desc = str(v.GetDescription() or "")
                    if desc.lower() == voice_name.lower():
                        chosen = v
                        break
                if chosen is None:
                    # best-effort contains
                    for i in range(int(voices.Count)):
                        v = voices.Item(i)
                        desc = str(v.GetDescription() or "")
                        if voice_name.lower() in desc.lower():
                            chosen = v
                            break
                if chosen is not None:
                    voice.Voice = chosen
            except Exception:
                pass

        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        # 22kHz 16-bit mono is SAFT22kHz16BitMono = 22
        stream.Format.Type = 22
        stream.Open(out_wav, 3, False)
        voice.AudioOutputStream = stream
        voice.Speak(text or "", 0)
        stream.Close()
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _get_32bit_powershell() -> str:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    ps = os.path.join(windir, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe")
    return ps if os.path.isfile(ps) else ""


def _run_hidden_subprocess(args, timeout=None):
    startupinfo = None
    creationflags = 0
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    except Exception:
        startupinfo = None
        creationflags = 0
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def _list_sapi5_32_voices() -> List[str]:
    ps = _get_32bit_powershell()
    if not ps:
        return []
    cmd = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$v=New-Object -ComObject SAPI.SpVoice;"
        "foreach($t in @($v.GetVoices())){$t.GetDescription()}"
    )
    try:
        proc = _run_hidden_subprocess(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            timeout=20,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out = proc.stdout.decode("utf-8", errors="replace")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _has_sapi5_32() -> bool:
    if not _get_32bit_powershell():
        return False
    try:
        for n, d in _list_nvda_synths():
            joined = f"{n} {d}".lower()
            if "speech api version 5" in joined and "32" in joined:
                return True
            if n.lower() in ("_sapi5", "sapi5_32", "sapi5"):
                if "32" in joined or "_sapi5" in n.lower():
                    return True
    except Exception:
        pass
    return False


def _render_with_sapi5_32(text: str, out_wav: str, voice_name: Optional[str] = None, rate: int = 0):
    ps = _get_32bit_powershell()
    if not ps:
        raise RuntimeError("32-bit PowerShell was not found; cannot render 32-bit SAPI voices.")
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    tmp_dir = tempfile.mkdtemp(prefix="soundWave_sapi32_")
    text_path = os.path.join(tmp_dir, "input.txt")
    script_path = os.path.join(tmp_dir, "render.ps1")
    try:
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text or "")
        script = r'''
param(
    [string]$TextPath,
    [string]$OutPath,
    [string]$VoiceName,
    [int]$Rate
)
$ErrorActionPreference = "Stop"
$text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
$voice = New-Object -ComObject SAPI.SpVoice
if ($VoiceName) {
    foreach ($token in @($voice.GetVoices())) {
        $desc = [string]$token.GetDescription()
        if ($desc -ieq $VoiceName -or $desc.ToLowerInvariant().Contains($VoiceName.ToLowerInvariant())) {
            $voice.Voice = $token
            break
        }
    }
}
$voice.Rate = $Rate
$stream = New-Object -ComObject SAPI.SpFileStream
$stream.Format.Type = 22
if (Test-Path -LiteralPath $OutPath) {
    Remove-Item -LiteralPath $OutPath -Force
}
$stream.Open($OutPath, 3, $false)
$voice.AudioOutputStream = $stream
[void]$voice.Speak($text, 0)
$stream.Close()
'''
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        proc = _run_hidden_subprocess(
            [
                ps,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_path,
                text_path,
                out_wav,
                str(voice_name or ""),
                str(int(rate or 0)),
            ],
            timeout=TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            if not err:
                err = proc.stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError("32-bit SAPI render failed: %s" % (err or "unknown error"))
        if not os.path.isfile(out_wav) or os.path.getsize(out_wav) <= 44:
            raise RuntimeError("32-bit SAPI render failed: output file was not created.")
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    return "SAPI5 32-bit file render"



    def _maybe_auto_test(self, evt=None):
        try:
            if hasattr(self, 'autoTestChk') and self.autoTestChk.GetValue():
                _set_cfg_bool('autoTestOnChange', True)
                if hasattr(self, '_on_test'):
                    self._on_test(None)
            else:
                _set_cfg_bool('autoTestOnChange', False)
        except Exception:
            pass

class Sapi5OptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(
        self,
        parent,
        title: str = "soundWave - SAPI5 options",
        voice_list_fn=None,
        render_fn=None,
        cfg_prefix: str = "sapi5",
    ):
        super().__init__(parent, title=title)
        self._voice_list_fn = voice_list_fn or _list_sapi5_voices
        self._render_fn = render_fn or _render_with_sapi5
        self._cfg_prefix = cfg_prefix
        self.voices = self._voice_list_fn() or []

        sizer = wx.BoxSizer(wx.VERTICAL)

        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(self, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voiceChoice = wx.Choice(self, choices=(self.voices if self.voices else ["(no voices found)"]))
        row1.Add(self.voiceChoice, 1, wx.EXPAND)
        sizer.Add(row1, 0, wx.EXPAND | wx.ALL, 10)

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(self, label="Rate:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.rateSpin = wx.SpinCtrl(self, min=-10, max=10, initial=0)
        row2.Add(self.rateSpin, 0)
        sizer.Add(row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.autoTest = wx.CheckBox(self, label="Auto-speak when changing voice or rate")
        self.autoTest.SetValue(bool(_get_cfg_bool(f"autoTestOnChange{cfg_prefix}", True)))
        sizer.Add(self.autoTest, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(self, label="Test (&T)")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
        btnRow.AddStretchSpacer(1)

        self.okBtn = wx.Button(self, wx.ID_OK)
        self.cancelBtn = wx.Button(self, wx.ID_CANCEL)
        try:
            self.okBtn.SetDefault()
        except Exception:
            pass
        btnRow.Add(self.okBtn, 0, wx.RIGHT, 8)
        btnRow.Add(self.cancelBtn, 0)
        sizer.Add(btnRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizerAndFit(sizer)

        # Load persisted selections
        voice_name = str(_cfg_get(f"{cfg_prefix}VoiceName", "") or "")
        rate = int(_cfg_get(f"{cfg_prefix}Rate", 0) or 0)

        if self.voices:
            if voice_name and voice_name in self.voices:
                self.voiceChoice.SetSelection(self.voices.index(voice_name))
            else:
                self.voiceChoice.SetSelection(0)
        else:
            self.voiceChoice.SetSelection(0)

        try:
            self.rateSpin.SetValue(rate)
        except Exception:
            pass

        # Events
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)
        self.voiceChoice.Bind(wx.EVT_CHOICE, self._on_change)
        self.rateSpin.Bind(wx.EVT_SPINCTRL, self._on_change)

    def _on_change(self, evt):
        if self.autoTest.IsChecked():
            self._on_test(None)

    def _on_test(self, evt):
        try:
            voice = self.get_voice_name()
            rate = self.get_rate()
            # quick temp file
            tmp = os.path.join(tempfile.gettempdir(), "soundWave_sapi_test.wav")
            self._render_fn(self.SAMPLE_TEXT, tmp, voice_name=voice, rate=rate)
            _play_wav(tmp)
        except Exception as e:
            _error("Test failed:\n" + str(e))

    def get_voice_name(self) -> str:
        if not self.voices:
            return ""
        i = self.voiceChoice.GetSelection()
        if i == wx.NOT_FOUND:
            return ""
        return str(self.voiceChoice.GetString(i))

    def get_rate(self) -> int:
        try:
            return int(self.rateSpin.GetValue())
        except Exception:
            return 0

    def get_options(self, persist: bool = True) -> dict:
        opts = {
            "voiceName": self.get_voice_name(),
            "rate": self.get_rate(),
            "autoTest": bool(self.autoTest.IsChecked()),
        }
        if persist:
            _cfg_set(f"{self._cfg_prefix}VoiceName", opts["voiceName"])
            _cfg_set(f"{self._cfg_prefix}Rate", int(opts["rate"]))
            _set_cfg_bool(f"autoTestOnChange{self._cfg_prefix}", bool(opts["autoTest"]))
        return opts


class OrpheusOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent, synth, initial: Optional[dict] = None):
        super().__init__(parent, title="soundWave - Orpheus options")
        self.synth = synth
        self.initial = initial or {}

        # --- Safety: keep Orpheus instance alive, but switch NVDA's live synth to eSpeak while this dialog is open.
        self._prevSynthName = ""
        self._patchedOrpheus = False
        self._origTerminate = None
        self._origCancel = None
        try:
            cur = synthDriverHandler.getSynth()
            self._prevSynthName = str(getattr(cur, "name", "") or "")
        except Exception:
            self._prevSynthName = ""

        # Patch terminate/cancel so NVDA won't unload Orpheus when we switch away.
        try:
            self._origTerminate = getattr(self.synth, "terminate", None)
            self._origCancel = getattr(self.synth, "cancel", None)
            def _noop(*a, **k):
                return None
            if callable(self._origTerminate):
                setattr(self.synth, "terminate", _noop)
            if callable(self._origCancel):
                setattr(self.synth, "cancel", _noop)
            self._patchedOrpheus = True
        except Exception:
            self._patchedOrpheus = False

        # Switch live synth to eSpeak for watchdog safety while navigating Orpheus options.
        try:
            _safe_set_synth("espeak")
        except Exception:
            pass


        pnl = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=4, cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(pnl, label="Language:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.langChoice = wx.Choice(pnl, choices=[])
        grid.Add(self.langChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(pnl, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.voiceChoice = wx.Choice(pnl, choices=[])
        grid.Add(self.voiceChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(pnl, label="Speed (%):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.speedSpin = wx.SpinCtrl(pnl, min=20, max=400, initial=int(self.initial.get("speed", 100) or 100))
        grid.Add(self.speedSpin, 0, wx.ALIGN_LEFT)

        self.autoTestChk = wx.CheckBox(pnl, label="Auto-speak when changing options")
        self.autoTestChk.SetValue(bool(self.initial.get("autoTest", False)))
        grid.Add(self.autoTestChk, 0, wx.TOP, 6)
        grid.AddSpacer(0)

        root.Add(grid, 0, wx.EXPAND | wx.ALL, 12)

        self.autoSpeakCB = _add_autospeak_checkbox(pnl, root, "orpheusAutoSpeak", default=True)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(pnl, label="Test")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
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

        self.Bind(wx.EVT_CHOICE, self._on_lang_change, self.langChoice)
        self.Bind(wx.EVT_CHOICE, self._on_voice_change, self.voiceChoice)
        self.Bind(wx.EVT_SPINCTRL, self._on_speed_change, self.speedSpin)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    def _on_destroy(self, evt):
        # Restore live synth and Orpheus methods when closing.
        try:
            if self._patchedOrpheus:
                if callable(self._origTerminate):
                    setattr(self.synth, "terminate", self._origTerminate)
                if callable(self._origCancel):
                    setattr(self.synth, "cancel", self._origCancel)
        except Exception:
            pass
        try:
            # Restore previous synth if it was Orpheus; otherwise just try to return to Orpheus.
            if (self._prevSynthName or "").lower() == "orpheus":
                _safe_set_synth("orpheus")
            elif self._prevSynthName:
                _safe_set_synth(self._prevSynthName)
            else:
                _safe_set_synth("orpheus")
        except Exception:
            pass
        try:
            evt.Skip()
        except Exception:
            pass


    def _maybe_auto_test(self):
        try:
            if self.autoTestChk.GetValue():
                self._on_test(None)
        except Exception:
            pass

    def _on_test(self, evt):
        # Run Orpheus preview off the GUI thread to reduce watchdog hangs.
        def _run():
            try:
                self.apply_to_synth()
                if hasattr(self.synth, "speak"):
                    self.synth.speak([self.SAMPLE_TEXT])
            except Exception:
                pass
        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            _run()

    def _on_lang_change(self, evt):
        self._populate_voices_for_selected_language()
        self._maybe_auto_test()

    def _on_voice_change(self, evt):
        self._maybe_auto_test()

    def _on_speed_change(self, evt):
        self._maybe_auto_test()

    def _populate_languages(self):
        self.langChoice.Clear()
        self._langs = []
        voices = []
        try:
            voices = _normalise_voice_infos(getattr(self.synth, "availableVoices", None))
        except Exception:
            voices = []
        if not voices:
            self.langChoice.Append("Default", clientData="")
            self.langChoice.SetSelection(0)
            return
        for i, vi in enumerate(voices):
            label = _orpheus_friendly_label(vi, fallback=f"Language {i+1}")
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
            variants = _normalise_voice_infos(getattr(self.synth, "availableVariants", None))
        except Exception:
            variants = []
        self._variants = variants or []

        if not variants:
            self.voiceChoice.Append("Default", clientData="")
            self.voiceChoice.SetSelection(0)
            return

        for i, vi in enumerate(variants):
            label = _orpheus_friendly_label(vi, fallback=f"Voice {i+1}")
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

    def get_options(self, persist: bool = True) -> Dict[str, object]:
        opts = {
            "languageId": self.get_language_id(),
            "variantId": self.get_variant_id(),
            "speed": self.get_speed(),
            "autoTest": bool(self.autoTestChk.GetValue()),
        }
        if persist:
            _cfg_set("orpheusLanguageId", opts["languageId"])
            _cfg_set("orpheusVariantId", opts["variantId"])
            _cfg_set("orpheusSpeed", int(opts["speed"]))
            _set_cfg_bool("autoTestOnChangeOrpheus", bool(opts["autoTest"]))
        return opts

# ----------------------------
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
        rowV.Add(wx.StaticText(self, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
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
        rowR.Add(wx.StaticText(self, label="Rate:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        init_rate = int(initial.get("rate", _cfg_get("dectalkRate", 180) or 180) or 180)
        self.rateSpin = wx.SpinCtrl(self, min=75, max=650, initial=init_rate)
        rowR.Add(self.rateSpin, 0)
        sizer.Add(rowR, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Auto test
        self.autoTest = wx.CheckBox(self, label="Auto speak when changing settings")
        self.autoTest.SetValue(bool(initial.get("autoTest", _get_cfg_bool("autoTestOnChangeDectalk", True))))
        sizer.Add(self.autoTest, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Buttons row: Test + OK/Cancel
        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(self, label="Test")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
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

    def _get_selected_voice_code(self) -> str:
        idx = self.voiceChoice.GetSelection()
        if idx < 0:
            idx = 0
        return _DECTALK_VOICES[idx][0]

    def _on_changed(self, evt):
        if self.autoTest.IsChecked():
            self._on_test(None)
        evt.Skip()

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
            "rate": int(self.rateSpin.GetValue()),
            "autoTest": bool(self.autoTest.IsChecked()),
        }
        if persist:
            _cfg_set("dectalkVoice", str(opts["voice"]))
            _cfg_set("dectalkRate", int(opts["rate"]))
            _set_cfg_bool("autoTestOnChangeDectalk", bool(opts["autoTest"]))
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
# Keynote Gold / BestSpeech offline rendering + options
# ----------------------------

def _has_bestspeech() -> bool:
    try:
        import synthDrivers.bestspeech  # noqa: F401
        return True
    except Exception:
        return False


def _list_bestspeech_voices() -> List[str]:
    """Return BestSpeech voice IDs (e.g. 'fred')."""
    try:
        import synthDrivers.bestspeech as bs
        drv = bs.SynthDriver()
        try:
            m = drv._getAvailableVoices()
            # keys are voice ids
            return sorted([str(k) for k in (m or {}).keys()])
        finally:
            try:
                drv.terminate()
            except Exception:
                pass
    except Exception:
        return [
            "fred", "sara", "hary", "wendy", "dexter", "alien", "kit",
            "bruno", "ghost", "peeper", "dracula", "granny", "martha", "tim"
        ]


class _BestSpeechCapturePlayer:
    """A minimal nvwave.WavePlayer-like object that captures PCM instead of playing it."""

    def __init__(self):
        self.pcm = bytearray()
        self._on_done = None

    def feed(self, data, size, onDone=None):
        try:
            n = int(size or 0)
        except Exception:
            n = 0

        if onDone is not None:
            # Driver uses this to signal completion.
            self._on_done = onDone

        if n > 0 and data:
            try:
                self.pcm.extend(ctypes.string_at(data, n))
            except Exception:
                try:
                    # data may already be a bytes-like object
                    self.pcm.extend(bytes(data)[:n])
                except Exception:
                    pass
        else:
            # size==0 buffer; if completion callback is present, call it right away
            if onDone is not None:
                try:
                    onDone()
                except Exception:
                    pass

    def idle(self):
        # In the real driver this blocks until the wave player drains.
        # For capture, we don't need to block.
        return

    def stop(self):
        return

    def pause(self, switch):
        return


class _GenericCaptureWavePlayer:
    """Capture-only stand-in for nvwave.WavePlayer."""

    def __init__(self, channels=1, samplesPerSec=22050, bitsPerSample=16, **kwargs):
        self.channels = int(channels or 1)
        self.samplesPerSec = int(samplesPerSec or 22050)
        self.bitsPerSample = int(bitsPerSample or 16)
        self.pcm = bytearray()
        self.last_audio_ts = None
        self.done_evt = threading.Event()

    def feed(self, data, size=None, onDone=None):
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

    def stop(self):
        return

    def pause(self, switch):
        return

    def close(self):
        self.done_evt.set()


def _make_capture_wave_player_factory(players: List[_GenericCaptureWavePlayer]):
    def _factory(*args, **kwargs):
        channels = kwargs.pop("channels", args[0] if len(args) > 0 else 1)
        samplesPerSec = kwargs.pop("samplesPerSec", args[1] if len(args) > 1 else 22050)
        bitsPerSample = kwargs.pop("bitsPerSample", args[2] if len(args) > 2 else 16)
        player = _GenericCaptureWavePlayer(
            channels=channels,
            samplesPerSec=samplesPerSec,
            bitsPerSample=bitsPerSample,
            **kwargs,
        )
        players.append(player)
        return player
    return _factory


def _render_with_nvda_generic_capture(
    text: str,
    out_wav: str,
    synth_name: str,
    cancel_evt: Optional[threading.Event] = None,
    progress: Optional[Dict[str, Any]] = None,
    opts: Optional[Dict[str, Any]] = None,
) -> str:
    """Best-effort generic capture using NVDA's normal synth driver audio path.

    This works for drivers that create an nvwave.WavePlayer while the driver is
    instantiated or while speech is generated. It is intentionally a fallback
    path: synth-specific renderers can still be faster or more exact.
    """
    if nvwave is None:
        raise RuntimeError("NVDA audio module is not available; generic capture cannot run.")
    cancel_evt = cancel_evt or threading.Event()
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    players: List[_GenericCaptureWavePlayer] = []
    old_wave_player = getattr(nvwave, "WavePlayer", None)
    synth = None
    done_evt = threading.Event()
    saw_done_notification = False
    expects_done_notification = False

    def _on_synth_done(synth=None, **kwargs):
        nonlocal saw_done_notification
        if synth is None:
            return
        if synth is not None and synth is current_synth_holder.get("synth"):
            saw_done_notification = True
            done_evt.set()

    current_synth_holder = {"synth": None}
    try:
        nvwave.WavePlayer = _make_capture_wave_player_factory(players)
        synth = _get_synth_instance(synth_name)
        current_synth_holder["synth"] = synth
        if synth is None or not hasattr(synth, "speak"):
            raise RuntimeError("Couldn't create NVDA synth instance for %s." % synth_name)
        if (synth_name or "").lower() == "worldvoice" and not hasattr(synth, "_voiceManager"):
            raise RuntimeError(
                "WorldVoice did not initialize its voice manager. Its workspace engines appear to be missing "
                "or failing to load; see the NVDA log for the missing WorldVoice-workspace DLLs."
            )
        if opts:
            try:
                voice = str(opts.get("voice", "") or "")
                if voice and hasattr(synth, "voice"):
                    synth.voice = voice
            except Exception:
                pass
            try:
                variant = str(opts.get("variant", "") or "")
                if variant and hasattr(synth, "variant"):
                    synth.variant = variant
            except Exception:
                pass
            try:
                if "rate" in opts and hasattr(synth, "rate"):
                    synth.rate = int(opts.get("rate", 50) or 50)
            except Exception:
                pass
        try:
            expects_done_notification = synthDriverHandler.synthDoneSpeaking in (getattr(synth, "supportedNotifications", set()) or set())
        except Exception:
            expects_done_notification = False
        try:
            synthDriverHandler.synthDoneSpeaking.register(_on_synth_done)
        except Exception:
            pass
        synth.speak([text or ""])

        fallback_quiet_seconds = max(8.0, min(30.0, len(text or "") / 1000.0))
        deadline = time.time() + float(TIMEOUT_SECONDS)
        while time.time() < deadline:
            if cancel_evt.is_set():
                try:
                    synth.cancel()
                except Exception:
                    pass
                raise RuntimeError("Cancelled.")
            total_bytes = sum(len(p.pcm) for p in players)
            last_audio = max([p.last_audio_ts or 0 for p in players] or [0])
            if progress is not None:
                progress["bytes"] = total_bytes
                progress["last_audio_ts"] = last_audio or None
                if players:
                    progress["pcm_rate"] = int(players[0].samplesPerSec)
                    progress["channels"] = int(players[0].channels)
                    progress["sampwidth"] = int(players[0].bitsPerSample // 8)
            if total_bytes > 0 and done_evt.is_set():
                break
            # Some older/nonstandard synths may not notify synthDoneSpeaking.
            # Keep this as a conservative fallback only; short quiet gaps are
            # common in long renders and must not be treated as completion.
            if total_bytes > 0 and not expects_done_notification and not saw_done_notification and last_audio and time.time() - last_audio >= fallback_quiet_seconds:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Generic NVDA capture timed out.")
    finally:
        try:
            synthDriverHandler.synthDoneSpeaking.unregister(_on_synth_done)
        except Exception:
            pass
        try:
            if old_wave_player is not None:
                nvwave.WavePlayer = old_wave_player
        except Exception:
            pass
        if synth is not None:
            try:
                synth.cancel()
            except Exception:
                pass
            try:
                synth.terminate()
            except Exception:
                pass

    active = [p for p in players if p.pcm]
    if not active:
        raise RuntimeError(
            "Generic NVDA capture produced no audio. This synth may not use NVDA's WavePlayer path."
        )
    first = active[0]
    expected = (first.channels, first.samplesPerSec, first.bitsPerSample)
    for player in active:
        fmt = (player.channels, player.samplesPerSec, player.bitsPerSample)
        if fmt != expected:
            raise RuntimeError("Generic NVDA capture saw mixed audio formats between player instances.")

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(first.channels)
        wf.setsampwidth(max(1, first.bitsPerSample // 8))
        wf.setframerate(first.samplesPerSec)
        for player in active:
            wf.writeframes(bytes(player.pcm))

    return "NVDA generic capture"


def _render_with_bestspeech_offline(
    text: str,
    out_wav: str,
    voice: str = "fred",
    rate: int = 90,
    rate_boost: bool = False,
    cancel_evt: Optional[threading.Event] = None,
    progress: Optional[Dict[str, Any]] = None,
) -> str:
    """Render BestSpeech/Keynote Gold to a WAV by calling the synth driver's own _speakBg.

    This is the most reliable path because it matches what the addon does in normal NVDA speech,
    but swaps the WavePlayer with a capture-only implementation.
    """
    cancel_evt = cancel_evt or threading.Event()
    if not out_wav.lower().endswith(".wav"):
        out_wav += ".wav"

    if ctypes is None:
        raise RuntimeError("ctypes not available; cannot render BestSpeech.")

    try:
        import synthDrivers.bestspeech as bs
    except Exception as e:
        raise RuntimeError("BestSpeech addon not installed (synthDrivers.bestspeech not found).") from e

    drv = bs.SynthDriver()
    cap = _BestSpeechCapturePlayer()
    done_evt = threading.Event()

    # Wrap cap.feed so we can track "done"
    orig_feed = cap.feed

    def _feed(data, size, onDone=None):
        if cancel_evt.is_set():
            return
        if onDone is not None:
            # mark done when the driver signals completion
            def _done_wrapper():
                try:
                    onDone()
                finally:
                    done_evt.set()
            return orig_feed(data, size, onDone=_done_wrapper)
        return orig_feed(data, size, onDone=None)

    cap.feed = _feed  # type: ignore

    try:
        # Apply options (voice id is what the addon expects)
        try:
            drv.voice = str(voice or "fred")
        except Exception:
            pass
        try:
            drv.rate = int(rate)
        except Exception:
            pass
        try:
            drv.rateBoost = bool(rate_boost)
        except Exception:
            # some addon versions expose rateBoost only as internal field
            try:
                drv._rateBoost = bool(rate_boost)
            except Exception:
                pass

        # Swap player
        try:
            drv.player = cap
        except Exception:
            pass

        # Build text exactly like the driver does (including its control sequences)
        lst = ["~n10,0]" if getattr(drv, "_abbreviations", True) else "~n10,1]",
               "~~1,0]" if getattr(drv, "_phrasePrediction", True) else "~~1,1]"]
        lst.append(text or "")
        t = " ".join(lst)
        try:
            if getattr(drv, "_numberProcessing", False):
                t = drv._formatNumbers(t)
        except Exception:
            pass
        # Use the addon's voice preset table directly so voice selection always affects *all* parameters.
        preset = {}
        try:
            preset = dict(getattr(bs, "voices", {}) or {}).get(str(voice or "fred"), {}) or {}
        except Exception:
            preset = {}
        v_headsize = preset.get("headsize", getattr(drv, "headsize", 1))
        v_excitation = preset.get("excitation", getattr(drv, "_excitation", 3))
        v_inflection = preset.get("inflection", getattr(drv, "_inflection", 110))
        v_unvoiced = preset.get("unvoicedVolume", getattr(drv, "_unvoicedVolume", 0))
        v_pitch = preset.get("pitch", getattr(drv, "_pitch", 130))

        t = f"~r{getattr(drv, '_rate', 175)}]~e{v_excitation}]~v{v_headsize}]~f{v_pitch}]~g{getattr(drv, '_volume', 100)}]~u{v_unvoiced}]~h{v_inflection}]{t} ~|"

        idx = []
        # Run driver speak routine in a worker thread so we can time out/cancel safely
        err_holder = {"err": None}

        def _run():
            try:
                drv._speakBg(t, idx)
            except Exception as e:
                err_holder["err"] = e
            finally:
                done_evt.set()

        th = threading.Thread(target=_run, daemon=True)
        th.start()

        deadline = time.time() + float(TIMEOUT_SECONDS)
        while not done_evt.is_set():
            if cancel_evt.is_set():
                try:
                    drv.cancel()
                except Exception:
                    pass
                raise RuntimeError("Cancelled.")
            if time.time() >= deadline:
                try:
                    drv.cancel()
                except Exception:
                    pass
                raise RuntimeError("BestSpeech render timed out.")
            time.sleep(0.02)

        if err_holder["err"] is not None:
            raise RuntimeError("BestSpeech driver speak() failed.") from err_holder["err"]

        pcm = bytes(cap.pcm)
        if not pcm:
            raise RuntimeError("BestSpeech produced no audio (no callback data).")

        # BestSpeech default format: 11025Hz mono 16-bit
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(11025)
            wf.writeframes(pcm)

        return "BestSpeech"
    finally:
        try:
            drv.terminate()
        except Exception:
            pass


class BestSpeechOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent, initial: Optional[dict] = None):
        super().__init__(parent, title="soundWave - Keynote Gold options")
        initial = initial or {}

        self.voices = _list_bestspeech_voices()

        sizer = wx.BoxSizer(wx.VERTICAL)

        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(self, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voiceChoice = wx.Choice(self, choices=self.voices if self.voices else ["(no voices found)"])
        row1.Add(self.voiceChoice, 1, wx.EXPAND)
        sizer.Add(row1, 0, wx.EXPAND | wx.ALL, 10)

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(self, label="Speed:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.rateSpin = wx.SpinCtrl(self, min=0, max=100, initial=int(initial.get("rate", _cfg_get("bestspeechRate", 90) or 90) or 90))
        row2.Add(self.rateSpin, 0)
        sizer.Add(row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.rateBoostChk = wx.CheckBox(self, label="Rate boost (faster)")
        self.rateBoostChk.SetValue(bool(initial.get("rateBoost", _cfg_get_bool("bestspeechRateBoost", False))))
        sizer.Add(self.rateBoostChk, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.autoSpeakCB = _add_autospeak_checkbox(self, sizer, "bestspeechAutoSpeak", default=True)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(self, label="Test (&T)")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
        btnRow.AddStretchSpacer(1)
        okCancel = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        btnRow.Add(okCancel, 0, wx.EXPAND)
        sizer.Add(btnRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizerAndFit(sizer)
        self.SetMinSize((520, -1))

        # Load persisted
        saved_voice = str(initial.get("voice", _cfg_get("bestspeechVoice", "") or "") or "")
        if self.voices:
            if saved_voice and saved_voice in self.voices:
                self.voiceChoice.SetSelection(self.voices.index(saved_voice))
            else:
                self.voiceChoice.SetSelection(0)

        # Focus voice list for accessibility
        try:
            wx.CallAfter(self.voiceChoice.SetFocus)
        except Exception:
            pass

        # Events
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)

        def _maybe_auto(evt):
            try:
                if self.autoSpeakCB.GetValue():
                    self._on_test(None)
            except Exception:
                pass
            evt.Skip()

        self.voiceChoice.Bind(wx.EVT_CHOICE, _maybe_auto)
        self.rateSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        self.rateBoostChk.Bind(wx.EVT_CHECKBOX, _maybe_auto)

    def _get_voice(self) -> str:
        if not self.voices:
            return ""
        i = self.voiceChoice.GetSelection()
        if i == wx.NOT_FOUND:
            i = 0
        return str(self.voiceChoice.GetString(i))

    def _on_test(self, evt):
        tmp_dir = tempfile.mkdtemp(prefix="soundWave_bestspeech_test_")
        tmp_wav = os.path.join(tmp_dir, "test.wav")
        err = None
        try:
            _render_with_bestspeech_offline(
                text=self.SAMPLE_TEXT,
                out_wav=tmp_wav,
                voice=self._get_voice() or "fred",
                rate=int(self.rateSpin.GetValue()),
                rate_boost=bool(self.rateBoostChk.GetValue()),
            )
            _play_wav(tmp_wav)
        except Exception as e:
            err = e
        finally:
            # Delay deletion slightly if playback is async
            _defer_delete_dir(tmp_dir, wav_for_duration=tmp_wav, extra_seconds=2.0)

        if err:
            _error(f"Keynote Gold test failed:\n{err}")

    def get_options(self, persist: bool = True) -> dict:
        opts = {
            "voice": self._get_voice(),
            "rate": int(self.rateSpin.GetValue()),
            "rateBoost": bool(self.rateBoostChk.GetValue()),
            "autoSpeak": bool(self.autoSpeakCB.GetValue()),
        }
        if persist:
            _cfg_set("bestspeechVoice", str(opts["voice"] or ""))
            _cfg_set("bestspeechRate", int(opts["rate"]))
            _cfg_set("bestspeechRateBoost", bool(opts["rateBoost"]))
            _set_cfg_bool("bestspeechAutoSpeak", bool(opts["autoSpeak"]))
        return opts

# ----------------------------
# Synth discovery + selection
# ----------------------------
def _has_sonata() -> bool:
    try:
        import synthDrivers.sonata_neural_voices  # noqa: F401
        return True
    except Exception:
        return False


def _list_nvda_synths() -> List[Tuple[str, str]]:
    """Returns list of (name, displayName)."""
    res: List[Tuple[str, str]] = []
    try:
        lst = synthDriverHandler.getSynthList()
        for info in lst:
            n = getattr(info, "name", "") or ""
            d = getattr(info, "displayName", "") or ""
            if not n:
                n, d = _normalize_synth_list_item(info)
            d = d or n
            if n:
                res.append((n, d))
    except Exception:
        pass
    return res


class SynthSelectDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="soundWave - Synthesizer")
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(self, label="Choose a synthesizer to render with:"), 0, wx.ALL, 10)

        choices: List[str] = []
        self._choice_meta: List[Dict[str, str]] = []

        if _has_sonata():
            choices.append("Sonata")
            self._choice_meta.append({"kind": "sonata", "label": "Sonata"})

        # Offline renderers we implement
        # Orpheus capture requires Orpheus to be the current NVDA synth (reuses live instance).
        try:
            _cur = synthDriverHandler.getSynth().name
        except Exception:
            _cur = ''
        if (_cur or '').lower() == 'orpheus':
            choices.append("Orpheus")
            self._choice_meta.append({"kind": "orpheus", "label": "Orpheus"})

        choices.append("SAPI5")
        self._choice_meta.append({"kind": "sapi5", "label": "SAPI5"})
        if _has_sapi5_32():
            choices.append("SAPI5 32-bit")
            self._choice_meta.append({"kind": "sapi5_32", "label": "SAPI5 32-bit"})

        choices.append("IBM ECI")
        self._choice_meta.append({"kind": "ibmeci", "label": "IBM ECI"})
        if _has_dectalk():
            choices.append("DECtalk")
            self._choice_meta.append({"kind": "dectalk", "label": "DECtalk"})
        if _has_bestspeech():
            choices.append("Keynote Gold")
            self._choice_meta.append({"kind": "bestspeech", "label": "Keynote Gold"})



        # Enumerate other NVDA synth drivers through the generic NVDA capture path.
        other = _list_nvda_synths()
        dedicated_ids = {"sonata", "sonata_neural_voices", "orpheus", "sapi5", "_sapi5", "sapi5_32", "ibmeci", "dectalk", "bestspeech"}
        non_rendering_ids = {"nospeech", "no_speech", "silence"}
        generic_other = [
            (n, d)
            for n, d in other
            if (n or "").lower() not in dedicated_ids
            and (n or "").replace("-", "").replace("_", "").lower() not in non_rendering_ids
            and "no speech" not in f"{n} {d}".lower()
            and "speech api version 5" not in f"{n} {d}".lower()
        ]
        if generic_other:
            for n, d in generic_other:
                choices.append(d)
                self._choice_meta.append({"kind": "nvda", "label": d, "nvdaName": n})

        self.choice = wx.Choice(self, choices=choices)
        sizer.Add(self.choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # record base dir
        baseRow = wx.BoxSizer(wx.HORIZONTAL)
        baseRow.Add(wx.StaticText(self, label="Record base folder:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.baseDir = wx.TextCtrl(self, value=str(_cfg_get("recordBaseDir", "") or ""))
        baseRow.Add(self.baseDir, 1, wx.EXPAND | wx.RIGHT, 8)
        self.browseBtn = wx.Button(self, label="Browse…")
        self.browseBtn.Bind(wx.EVT_BUTTON, self._on_browse)
        baseRow.Add(self.browseBtn, 0)
        sizer.Add(baseRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticText(
            self,
            label="Tip: output defaults into a per-synth subfolder under the base folder.\n"
                  "Leave blank to use your home folder."
        ), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        
        btns = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(btns, 0, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(sizer)
        self.SetMinSize((720, 260))
        self.Fit()

        # Restore selection
        last = int(_cfg_get("lastSynthChoiceIndex", 0) or 0)
        if 0 <= last < len(choices):
            self.choice.SetSelection(last)
        else:
            self.choice.SetSelection(0)

        try:
            self.choice.SetFocus()
        except Exception:
            pass

    def _on_browse(self, evt):
        dlg = wx.DirDialog(self, "Choose record base folder")
        try:
            if _show_modal(dlg) == wx.ID_OK:
                self.baseDir.SetValue(dlg.GetPath())
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass

    def get_choice(self) -> Dict[str, str]:
        idx = self.choice.GetSelection()
        if idx < 0:
            idx = 0

        _cfg_set("lastSynthChoiceIndex", idx)
        _cfg_set("recordBaseDir", self.baseDir.GetValue() or "")



        meta = self._choice_meta[idx]
        return dict(meta)




def _normalise_voice_infos(raw) -> List[object]:
    if raw is None:
        return []
    try:
        if isinstance(raw, dict):
            return list(raw.values())
    except Exception:
        pass
    try:
        if hasattr(raw, "values") and callable(raw.values):
            return list(raw.values())
    except Exception:
        pass
    try:
        return list(raw)
    except Exception:
        return []


def _looks_numeric_label(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    if value.isdigit():
        return True
    return bool(re.match(r"^(language|voice)\s+\d+$", value, re.I))


_FRIENDLY_LOCALE_NAMES = {
    "en": "English",
    "en-gb": "UK English",
    "en_gb": "UK English",
    "en-us": "US English",
    "en_us": "US English",
    "es": "Spanish",
    "es-mx": "Mexican Spanish",
    "es_mx": "Mexican Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt-br": "Brazilian Portuguese",
    "pt_br": "Brazilian Portuguese",
    "pt-pt": "Portuguese",
    "pt_pt": "Portuguese",
    "nl": "Dutch",
    "el": "Greek",
    "hu": "Hungarian",
    "hr": "Croatian",
    "ro": "Romanian",
    "cs": "Czech",
    "da": "Danish",
    "sv": "Swedish",
    "nb-no": "Norwegian",
    "nb_no": "Norwegian",
    "pl": "Polish",
    "ms": "Malay",
    "zh": "Chinese",
    "fi": "Finnish",
    "lt": "Lithuanian",
    "cy": "Welsh",
}


def _friendly_locale_name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    key = value.replace("_", "-").lower()
    if key in _FRIENDLY_LOCALE_NAMES:
        return _FRIENDLY_LOCALE_NAMES[key]
    try:
        import languageHandler
        label = languageHandler.getLanguageDescription(value)
        if label:
            return str(label)
    except Exception:
        pass
    return value


def _voice_info_text(vi, *names: str) -> str:
    for name in names:
        try:
            value = getattr(vi, name, "")
        except Exception:
            value = ""
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
    return ""


def _orpheus_friendly_label(vi, fallback: str = "") -> str:
    """
    Orpheus VoiceInfo objects typically have:
      - name: human language name (e.g. English)
      - language: locale code (e.g. en-gb)
      - description: sometimes set for variants
    We prefer: "description/name (language)" when both exist and differ.
    """
    name = _voice_info_text(vi, "displayName", "displayNameWithAccelerator", "name")
    locale = _voice_info_text(vi, "language", "locale")
    desc = _voice_info_text(vi, "description")

    base = desc or name
    friendly_locale = _friendly_locale_name(locale)
    if _looks_numeric_label(base) and friendly_locale and not _looks_numeric_label(friendly_locale):
        base = friendly_locale
    if base and friendly_locale and friendly_locale.lower() not in base.lower() and not _looks_numeric_label(base):
        return f"{base} ({friendly_locale})"
    return base or fallback


def _voice_choice_label(vi, fallback: str = "") -> str:
    label = _voice_info_text(vi, "displayName", "displayNameWithAccelerator", "description", "name")
    locale = _voice_info_text(vi, "language", "locale")
    friendly_locale = _friendly_locale_name(locale)
    if _looks_numeric_label(label) and friendly_locale and not _looks_numeric_label(friendly_locale):
        label = friendly_locale
    if label and friendly_locale and friendly_locale.lower() not in label.lower() and not _looks_numeric_label(label):
        return f"{label} ({friendly_locale})"
    return label or friendly_locale or fallback


class GenericNvdaOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent, synth_id: str, synth_label: str):
        super().__init__(parent, title=f"soundWave - {synth_label} options", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.synth_id = synth_id
        self.synth_label = synth_label or synth_id or "NVDA synth"
        self.cfg_prefix = "genericNvda_" + _safe_config_key(self.synth_id)
        self.synth = None
        self.voices: List[object] = []
        self.variants: List[object] = []
        self._populating = False

        self.synth = _get_synth_instance(self.synth_id)
        if self.synth is None:
            raise RuntimeError(f"{self.synth_label} could not be initialized.")
        if self.synth_id.lower() == "worldvoice" and not hasattr(self.synth, "_voiceManager"):
            raise RuntimeError(
                "WorldVoice did not initialize its voice manager. The NVDA log shows missing "
                "WorldVoice-workspace engine DLLs, so SoundWave cannot render it until WorldVoice itself loads cleanly."
            )

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.voiceChoice = wx.Choice(panel)
        grid.Add(self.voiceChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Variant:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.variantChoice = wx.Choice(panel)
        grid.Add(self.variantChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Rate:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.rateSpin = wx.SpinCtrl(panel, min=0, max=100, initial=int(_cfg_get(self.cfg_prefix + "_rate", _safe_getattr(self.synth, "rate", 50) or 50)))
        grid.Add(self.rateSpin, 0, wx.EXPAND)

        root.Add(grid, 1, wx.ALL | wx.EXPAND, 10)
        self.autoSpeakCB = _add_autospeak_checkbox(panel, root, self.cfg_prefix + "_autoTest", default=True)

        buttons = wx.StdDialogButtonSizer()
        self.testBtn = wx.Button(panel, label="Test")
        ok = wx.Button(panel, wx.ID_OK)
        cancel = wx.Button(panel, wx.ID_CANCEL)
        buttons.AddButton(self.testBtn)
        buttons.AddButton(ok)
        buttons.AddButton(cancel)
        buttons.Realize()
        root.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize((460, self.GetSize().height))

        self._populate_voices()
        self._populate_variants()

        self.voiceChoice.Bind(wx.EVT_CHOICE, self._on_voice_changed)
        self.variantChoice.Bind(wx.EVT_CHOICE, self._maybe_auto_test)
        self.rateSpin.Bind(wx.EVT_SPINCTRL, self._maybe_auto_test)
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)

    def Destroy(self):
        try:
            if self.synth is not None:
                self.synth.terminate()
        except Exception:
            pass
        return super().Destroy()

    def _append_choice(self, ctrl, label: str, value: str):
        try:
            ctrl.Append(label, value)
        except Exception:
            ctrl.Append(label)

    def _choice_value(self, ctrl) -> str:
        idx = ctrl.GetSelection()
        if idx < 0:
            return ""
        try:
            return str(ctrl.GetClientData(idx) or "")
        except Exception:
            return ""

    def _populate_voices(self):
        self._populating = True
        try:
            self.voiceChoice.Clear()
            self.voices = _normalise_voice_infos(_safe_getattr(self.synth, "availableVoices", None))
            saved = str(_cfg_get(self.cfg_prefix + "_voice", _safe_getattr(self.synth, "voice", "") or "") or "")
            selected = 0
            if not self.voices:
                self._append_choice(self.voiceChoice, "Default", "")
            for i, vi in enumerate(self.voices):
                vid = _voice_info_text(vi, "id", "identifier", "name") or str(i)
                label = _voice_choice_label(vi, fallback=f"Voice {i + 1}")
                self._append_choice(self.voiceChoice, label, vid)
                if saved and vid == saved:
                    selected = i
            self.voiceChoice.SetSelection(selected if self.voiceChoice.GetCount() else wx.NOT_FOUND)
        finally:
            self._populating = False

    def _populate_variants(self):
        self._populating = True
        try:
            voice = self._choice_value(self.voiceChoice)
            if voice and hasattr(self.synth, "voice"):
                try:
                    self.synth.voice = voice
                except Exception:
                    pass
            self.variantChoice.Clear()
            self.variants = _normalise_voice_infos(_safe_getattr(self.synth, "availableVariants", None))
            saved = str(_cfg_get(self.cfg_prefix + "_variant", _safe_getattr(self.synth, "variant", "") or "") or "")
            selected = 0
            if not self.variants:
                self._append_choice(self.variantChoice, "Default", "")
            for i, vi in enumerate(self.variants):
                vid = _voice_info_text(vi, "id", "identifier", "name") or str(i)
                label = _voice_choice_label(vi, fallback=f"Variant {i + 1}")
                self._append_choice(self.variantChoice, label, vid)
                if saved and vid == saved:
                    selected = i
            self.variantChoice.SetSelection(selected if self.variantChoice.GetCount() else wx.NOT_FOUND)
        finally:
            self._populating = False

    def _on_voice_changed(self, evt=None):
        self._populate_variants()
        self._maybe_auto_test(evt)

    def _maybe_auto_test(self, evt=None):
        if self._populating:
            return
        try:
            if self.autoSpeakCB.GetValue():
                self._on_test(None)
        except Exception:
            pass

    def _on_test(self, evt=None):
        tmp_dir = tempfile.mkdtemp(prefix="soundWave_generic_test_")
        tmp_wav = os.path.join(tmp_dir, "test.wav")
        try:
            _render_with_nvda_generic_capture(
                self.SAMPLE_TEXT,
                tmp_wav,
                self.synth_id,
                cancel_evt=threading.Event(),
                progress={},
                opts=self.get_options(persist=False),
            )
            _play_wav(tmp_wav)
            _defer_delete_dir(tmp_dir, tmp_wav)
        except Exception as e:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            _error(str(e))

    def get_options(self, persist: bool = True) -> Dict[str, Any]:
        opts = {
            "voice": self._choice_value(self.voiceChoice),
            "variant": self._choice_value(self.variantChoice),
            "rate": int(self.rateSpin.GetValue()),
            "autoTest": bool(self.autoSpeakCB.GetValue()),
        }
        if persist:
            _cfg_set(self.cfg_prefix + "_voice", opts["voice"])
            _cfg_set(self.cfg_prefix + "_variant", opts["variant"])
            _cfg_set(self.cfg_prefix + "_rate", int(opts["rate"]))
            _cfg_set(self.cfg_prefix + "_autoTest", bool(opts["autoTest"]))
        return opts



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
    """IBM ECI options: DLL path + voice id + speed."""
    SAMPLE_TEXT = "This is a voice and speed test."

    def __init__(self, parent, initial=None):
        super().__init__(parent, title="soundWave - IBM ECI options")
        self.initial = initial or {}
        if not self.initial.get("dllPath"):
            found_dll = _find_ibmeci_dll()
            if found_dll:
                self.initial["dllPath"] = found_dll

        pnl = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        # Primary controls: Voice + Speed
        grid = wx.FlexGridSizer(rows=2, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        # Voice (friendly list from .SYN when available)
        grid.Add(wx.StaticText(pnl, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._voiceItems = _eci_enumerate_voices_from_syn(self.initial.get("dllPath", "") or "")
        self.voiceChoice = wx.Choice(pnl, choices=[lbl for (_vid, lbl) in self._voiceItems])
        grid.Add(self.voiceChoice, 1, wx.EXPAND)

        # Speed
        grid.Add(wx.StaticText(pnl, label="Speed:"), 0, wx.ALIGN_CENTER_VERTICAL)
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

        # Advanced: DLL path (kept out of the way; rarely changed)
        adv = wx.StaticBoxSizer(wx.StaticBox(pnl, label="Advanced"), wx.VERTICAL)
        dllRow = wx.BoxSizer(wx.HORIZONTAL)
        dllRow.Add(wx.StaticText(pnl, label="ECI DLL path:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.dllText = wx.TextCtrl(pnl, value=str(self.initial.get("dllPath", "") or ""))
        dllRow.Add(self.dllText, 1, wx.EXPAND | wx.RIGHT, 8)
        self.browseBtn = wx.Button(pnl, label="Browse…")
        dllRow.Add(self.browseBtn, 0)
        adv.Add(dllRow, 0, wx.EXPAND | wx.ALL, 8)
        root.Add(adv, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.autoSpeakCB = _add_autospeak_checkbox(pnl, root, "autoTestOnChangeIbmEci", default=True)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(pnl, label="Test (&T)")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
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

        self.browseBtn.Bind(wx.EVT_BUTTON, self._on_browse)
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
        self.dllText.Bind(wx.EVT_TEXT, _maybe_auto)
        self.voiceChoice.Bind(wx.EVT_CHOICE, _maybe_auto)
        self.speedSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)


    def _on_browse(self, evt):
        fd = wx.FileDialog(
            self,
            message="Select IBM ECI DLL (eci.dll)…",
            wildcard="DLL files (*.dll)|*.dll|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if _show_modal(fd) == wx.ID_OK:
                self.dllText.SetValue(fd.GetPath())
        finally:
            try:
                fd.Destroy()
            except Exception:
                pass

    def _on_test(self, evt):
        dll_path = (self.dllText.GetValue() or "").strip()
        if not dll_path or not os.path.isfile(dll_path):
            _error("Please set a valid ECI DLL path before testing.")
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
            "dllPath": (self.dllText.GetValue() or "").strip(),
            "voiceId": int(self._voiceItems[self.voiceChoice.GetSelection()][0]) if self._voiceItems and self.voiceChoice.GetSelection() >= 0 else 0,
            "speed": int(self.speedSpin.GetValue()),
            "autoTest": bool(self.autoSpeakCB.GetValue()) if hasattr(self, "autoSpeakCB") else True,
        }

# ----------------------------
# Sonata options / discovery
# ----------------------------
def _ensure_grpc_experimental_shim():
    """Some bundled grpc builds try to import grpc.experimental."""
    if "grpc.experimental" not in sys.modules:
        sys.modules["grpc.experimental"] = types.ModuleType("grpc.experimental")
    if "grpc.experimental.aio" not in sys.modules:
        sys.modules["grpc.experimental.aio"] = types.ModuleType("grpc.experimental.aio")

def _list_sonata_voice_configs() -> List[Tuple[str, str, List[str]]]:
    """
    Returns a list of (label, config_path, speaker_keys).
    Uses Sonata's voice discovery to locate voices in NVDA config.
    """
    try:
        from synthDrivers.sonata_neural_voices.tts_system import SonataTextToSpeechSystem
    except Exception as e:
        raise RuntimeError("Sonata addon not installed (synthDrivers.sonata_neural_voices not found).") from e

    voices = SonataTextToSpeechSystem.load_piper_voices_from_nvda_config_dir()
    results: List[Tuple[str, str, List[str]]] = []

    for v in voices:
        key = getattr(v, "key", "") or ""
        loc = getattr(v, "location", "") or ""
        label = key or os.path.basename(os.fspath(loc)) or "voice"

        try:
            cfg = next(pathlib.Path(loc).glob("*.json"))
            cfg_path = os.fspath(cfg)
        except Exception:
            continue

        speakers: List[str] = ["0"]
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                j = json.load(f)
            m = j.get("speaker_id_map")
            if isinstance(m, dict) and m:
                speakers = sorted([str(k) for k in m.keys()])
            else:
                n = j.get("num_speakers")
                if isinstance(n, int) and n > 1:
                    speakers = [str(i) for i in range(n)]
        except Exception:
            pass

        results.append((label, cfg_path, speakers))

    if not results:
        raise RuntimeError("No Sonata Piper voice configs were found.")
    return results



def _render_with_sonata_offline(
    text: str,
    out_wav: str,
    cancel_evt: threading.Event,
    voice_config_path: str,
    speaker: str,
    speed_percent: int,
) -> str:
    """
    Renders via Sonata's own gRPC server. This does not touch NVDA's live speech output.
    Returns the RPC mode used.
    """
    from synthDrivers.sonata_neural_voices.grpc_client import start_grpc_server
    from synthDrivers.sonata_neural_voices.helpers import import_bundled_library
    import globalVars as gv

    if not start_grpc_server():
        raise RuntimeError("Failed to start Sonata gRPC server.")
    port = getattr(gv, "SONATA_GRPC_SERVER_PORT", None)
    if not port:
        raise RuntimeError("Sonata gRPC server did not provide a port.")

    _ensure_grpc_experimental_shim()

    with import_bundled_library():
        from synthDrivers.sonata_neural_voices.lib import grpc as grpc  # bundled
        from synthDrivers.sonata_neural_voices.grpc_client.grpc_protos import sonata_grpc_pb2 as msgs
        from synthDrivers.sonata_neural_voices.grpc_client.grpc_protos import sonata_grpc_pb2_grpc as stubs

        channel = grpc.insecure_channel(f"localhost:{port}")
        service = stubs.sonata_grpcStub(channel)

        # Load voice
        voice_info = service.LoadVoice(msgs.VoicePath(config_path=str(voice_config_path)))
        voice_id = getattr(voice_info, "voice_id", None)
        if not voice_id:
            raise RuntimeError("Sonata LoadVoice returned no voice_id.")

        # Apply speaker + speed
        length_scale = 0.0
        try:
            rate = float(speed_percent) / 100.0
            if rate > 0:
                length_scale = float(1.0 / rate)
        except Exception:
            length_scale = 0.0

        try:
            opts = msgs.SynthesisOptions(speaker=str(speaker or ""), length_scale=float(length_scale))
            service.SetSynthesisOptions(msgs.VoiceSynthesisOptions(voice_id=str(voice_id), synthesis_options=opts))
        except Exception as e:
            log.warning("soundWave: SetSynthesisOptions failed: %s" % e, exc_info=True)

        # Audio format
        sr = 22050
        ch = 1
        sw = 2
        try:
            ai = getattr(voice_info, "audio", None)
            if ai:
                sr = int(getattr(ai, "sample_rate", sr) or sr)
                ch = int(getattr(ai, "num_channels", ch) or ch)
                sw = int(getattr(ai, "sample_width", sw) or sw)
        except Exception:
            pass

        wf = wave.open(out_wav, "wb")
        wf.setnchannels(ch)
        wf.setsampwidth(sw)
        wf.setframerate(sr)

        mode = "SynthesizeUtterance"
        chunks = 0
        total = 0
        first = True
        try:
            utter = msgs.Utterance(voice_id=str(voice_id), text=text or "")
            try:
                stream = service.SynthesizeUtterance(utter)
            except Exception:
                mode = "SynthesizeUtteranceRealtime"
                stream = service.SynthesizeUtteranceRealtime(utter)

            for msg in stream:
                if cancel_evt.is_set():
                    break
                b = getattr(msg, "wav_samples", None)
                if not b:
                    continue
                wf.writeframes(b)
                chunks += 1
                total += len(b)
                if first:
                    head = bytes(b[:16]).hex()
                    log.info(f"soundWave: sonata first chunk bytes={len(b)} head={head}")
                    first = False

            log.info(f"soundWave: sonata mode={mode} chunks={chunks} totalBytes={total}")
        finally:
            try:
                wf.close()
            except Exception:
                pass
            try:
                channel.close()
            except Exception:
                pass

        return mode

class SonataOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent):
        super().__init__(parent, title="soundWave - Sonata options")
        self.voices = _list_sonata_voice_configs()

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Voice
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(self, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voiceChoice = wx.Choice(self, choices=[v[0] for v in self.voices] if self.voices else ["(no voices found)"])
        row1.Add(self.voiceChoice, 1, wx.EXPAND)
        sizer.Add(row1, 0, wx.EXPAND | wx.ALL, 10)

        # Speaker
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(self, label="Speaker:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.speakerChoice = wx.Choice(self, choices=["0"])
        row2.Add(self.speakerChoice, 1, wx.EXPAND)
        sizer.Add(row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Speed
        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(self, label="Speed (%):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.speedSpin = wx.SpinCtrl(self, min=50, max=400, initial=140)
        row3.Add(self.speedSpin, 0)
        sizer.Add(row3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Auto-test
        self.autoTest = wx.CheckBox(self, label="Auto-speak when changing voice, speaker, or speed")
        self.autoTest.SetValue(bool(_get_cfg_bool("autoTestOnChangeSonata", True)))
        sizer.Add(self.autoTest, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Buttons
        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(self, label="Test (&T)")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
        btnRow.AddStretchSpacer(1)

        self.okBtn = wx.Button(self, wx.ID_OK)
        self.cancelBtn = wx.Button(self, wx.ID_CANCEL)
        try:
            self.okBtn.SetDefault()
        except Exception:
            pass
        btnRow.Add(self.okBtn, 0, wx.RIGHT, 8)
        btnRow.Add(self.cancelBtn, 0)
        sizer.Add(btnRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizer(sizer)
        self.Fit()
        self.SetMinSize((560, self.GetSize().height))

        # Restore defaults
        if self.voices:
            saved_cfg = _cfg_get("sonataVoiceConfigPath", None)
            idx = 0
            if saved_cfg:
                for i, (_, cfg, _) in enumerate(self.voices):
                    if cfg == saved_cfg:
                        idx = i
                        break
            self.voiceChoice.SetSelection(idx)
        else:
            self.voiceChoice.SetSelection(0)

        self._rebuild_speakers()

        try:
            self.speedSpin.SetValue(int(_cfg_get("sonataSpeedPercent", 140) or 140))
        except Exception:
            self.speedSpin.SetValue(140)

        try:
            self.speakerChoice.SetSelection(int(_cfg_get("sonataSpeakerIndex", 0) or 0))
        except Exception:
            self.speakerChoice.SetSelection(0)

        # Events
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)
        self.voiceChoice.Bind(wx.EVT_CHOICE, self._on_change)
        self.speakerChoice.Bind(wx.EVT_CHOICE, self._on_change)
        self.speedSpin.Bind(wx.EVT_SPINCTRL, self._on_change)

        # Focus voice list first
        try:
            self.voiceChoice.SetFocus()
        except Exception:
            pass

    def _rebuild_speakers(self):
        if not self.voices:
            self.speakerChoice.SetItems(["0"])
            self.speakerChoice.SetSelection(0)
            return
        i = self.voiceChoice.GetSelection()
        if i < 0:
            i = 0
        speakers = self.voices[i][2] or ["0"]
        self.speakerChoice.Clear()
        for s in speakers:
            self.speakerChoice.Append(str(s))
        self.speakerChoice.SetSelection(0)

    def _on_change(self, evt):
        # If voice changed, rebuild speakers list
        if evt and evt.GetEventObject() is self.voiceChoice:
            self._rebuild_speakers()
        if self.autoTest.IsChecked():
            self._on_test(None)
        evt.Skip()

    def _on_test(self, evt):
        try:
            opts = self.get_options(persist=False)
            tmp = os.path.join(tempfile.gettempdir(), "soundWave_sonata_test.wav")
            cancel_evt = threading.Event()
            _render_with_sonata_offline(
                self.SAMPLE_TEXT,
                tmp,
                cancel_evt=cancel_evt,
                voice_config_path=str(opts.get("voice_config_path", "")),
                speaker=str(opts.get("speaker", "0")),
                speed_percent=int(opts.get("speed_percent", 140)),
            )
            _play_wav(tmp)
        except Exception as e:
            _error("Test failed:\n" + str(e))

    def get_options(self, persist: bool = True) -> Dict[str, object]:
        if not self.voices:
            opts = {
                "voice_label": "",
                "voice_config_path": "",
                "speaker": "0",
                "speed_percent": int(self.speedSpin.GetValue()),
            }
            if persist:
                _cfg_set("sonataSpeedPercent", opts["speed_percent"])
                _cfg_set("autoTestOnChangeSonata", bool(self.autoTest.IsChecked()))
            return opts

        i = self.voiceChoice.GetSelection()
        if i < 0:
            i = 0
        label, cfg_path, speakers = self.voices[i]

        sp_idx = self.speakerChoice.GetSelection()
        if sp_idx < 0:
            sp_idx = 0
        speaker = speakers[sp_idx] if sp_idx < len(speakers) else "0"

        speed = int(self.speedSpin.GetValue())

        if persist:
            _cfg_set("sonataVoiceConfigPath", cfg_path)
            _cfg_set("sonataSpeakerIndex", sp_idx)
            _cfg_set("sonataSpeedPercent", speed)
            _cfg_set("autoTestOnChangeSonata", bool(self.autoTest.IsChecked()))

        return {
            "voice_label": label,
            "voice_config_path": cfg_path,
            "speaker": str(speaker),
            "speed_percent": speed,
        }



class OrpheusOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent, synth, initial: Optional[dict] = None):
        super().__init__(parent, title="soundWave - Orpheus options")
        self.synth = synth
        self.initial = initial or {}

        pnl = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=4, cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(pnl, label="Language:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.langChoice = wx.Choice(pnl, choices=[])
        grid.Add(self.langChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(pnl, label="Voice:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.voiceChoice = wx.Choice(pnl, choices=[])
        grid.Add(self.voiceChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(pnl, label="Speed (%):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.speedSpin = wx.SpinCtrl(pnl, min=20, max=400, initial=int(self.initial.get("speed", 100) or 100))
        grid.Add(self.speedSpin, 0, wx.ALIGN_LEFT)

        self.autoTestChk = wx.CheckBox(pnl, label="Auto-speak when changing options")
        self.autoTestChk.SetValue(bool(self.initial.get("autoTest", False)))
        grid.Add(self.autoTestChk, 0, wx.TOP, 6)
        grid.AddSpacer(0)

        root.Add(grid, 0, wx.EXPAND | wx.ALL, 12)

        self.autoSpeakCB = _add_autospeak_checkbox(pnl, root, "orpheusAutoSpeak", default=True)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(pnl, label="Test")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
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

        self.Bind(wx.EVT_CHOICE, self._on_lang_change, self.langChoice)
        self.Bind(wx.EVT_CHOICE, self._on_voice_change, self.voiceChoice)
        self.Bind(wx.EVT_SPINCTRL, self._on_speed_change, self.speedSpin)

    def _maybe_auto_test(self):
        try:
            if self.autoTestChk.GetValue():
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

    def _populate_languages(self):
        self.langChoice.Clear()
        self._langs = []
        voices = []
        try:
            voices = _normalise_voice_infos(getattr(self.synth, "availableVoices", None))
        except Exception:
            voices = []
        if not voices:
            self.langChoice.Append("Default", clientData="")
            self.langChoice.SetSelection(0)
            return
        for i, vi in enumerate(voices):
            label = _orpheus_friendly_label(vi, fallback=f"Language {i+1}")
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
            variants = _normalise_voice_infos(getattr(self.synth, "availableVariants", None))
        except Exception:
            variants = []
        self._variants = variants or []

        if not variants:
            self.voiceChoice.Append("Default", clientData="")
            self.voiceChoice.SetSelection(0)
            return

        for i, vi in enumerate(variants):
            label = _orpheus_friendly_label(vi, fallback=f"Voice {i+1}")
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

    def get_options(self, persist: bool = True) -> Dict[str, object]:
        opts = {
            "languageId": self.get_language_id(),
            "variantId": self.get_variant_id(),
            "speed": self.get_speed(),
            "autoTest": bool(self.autoTestChk.GetValue()),
        }
        if persist:
            _cfg_set("orpheusLanguageId", opts["languageId"])
            _cfg_set("orpheusVariantId", opts["variantId"])
            _cfg_set("orpheusSpeed", int(opts["speed"]))
            _set_cfg_bool("autoTestOnChangeOrpheus", bool(opts["autoTest"]))
        return opts

# ----------------------------
# Orpheus capture renderer
# ----------------------------

    def _maybe_auto_test(self, evt=None):
        try:
            if hasattr(self, 'autoTestChk') and self.autoTestChk.GetValue():
                _set_cfg_bool('autoTestOnChangeSAPI', True)
                if hasattr(self, '_on_test'):
                    self._on_test(None)
            else:
                _set_cfg_bool('autoTestOnChangeSAPI', False)
        except Exception:
            pass


    def _on_show_focus(self, evt):
        try:
            if evt.IsShown() and hasattr(self, 'voiceChoice'):
                wx.CallAfter(self.voiceChoice.SetFocus)
        except Exception:
            pass
        try:
            evt.Skip()
        except Exception:
            pass

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
        raise RuntimeError("Orpheus wrapper capture is not available in this driver.")

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
            raise RuntimeError("Orpheus render timed out waiting for end-of-string marker.")
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
        raise RuntimeError("Orpheus render failed: no audio was captured.")
    return "Orpheus wrapper capture"


def _render_with_orpheus_dll_capture(text: str, out_wav: str, synth) -> str:
    if ctypes is None or WINFUNCTYPE is None:
        raise RuntimeError("ctypes not available; cannot render with Orpheus.")

    if not getattr(synth, "lib", None):
        raise RuntimeError("Orpheus DLL not loaded.")
    if not hasattr(synth.lib, "TTS_SetAudioMethod"):
        raise RuntimeError("Orpheus DLL missing TTS_SetAudioMethod; cannot capture audio.")
    if not hasattr(synth, "speak"):
        raise RuntimeError("Orpheus driver missing speak(); cannot render.")

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
            raise RuntimeError("Orpheus render timed out waiting for end-of-string marker.")
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
        raise RuntimeError("Orpheus render failed: no audio was captured.")
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
        if hasattr(synth, "_on_audio"):
            return _render_with_orpheus_wrapper_capture(text, out_wav, synth)
    except Exception:
        log.error("soundWave: Orpheus wrapper capture failed", exc_info=True)
        if not getattr(synth, "lib", None):
            raise

    return _render_with_orpheus_dll_capture(text, out_wav, synth)


# ----------------------------
# IBM ECI renderer (DLL)
# ----------------------------
_ECIMessage_eciWaveformBuffer = 0
_ECIMessage_eciIndexReply = 2
_END_STRING_MARK = 0xFFFF

def _find_ibmeci_dll() -> str:
    """Find the bundled IBM ECI DLL from the installed IBMTTS add-on."""
    candidates = []
    try:
        base = os.path.join(os.path.expandvars("%APPDATA%"), "nvda", "addons", "IBMTTS")
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
        for path in glob.glob(os.path.join(addons_dir, "IBMTTS", "**", "ECI.DLL"), recursive=True):
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
        raise RuntimeError("IBM ECI DLL path not found.")

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
        raise RuntimeError("IBM ECI DLL path not found.")
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

def _convert_with_ffmpeg(in_wav: str, out_path: str):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH.")
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", in_wav, "-codec:a", "libmp3lame", "-q:a", "2", out_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed: {err or 'unknown error'}")


# ----------------------------
# Rendering
# ----------------------------
@dataclass
class RenderResult:
    ok: bool = False
    err: Optional[Exception] = None
    progress: Optional[dict] = None  # renderer progress info (best-effort)
    wall_s: float = 0.0
    audio_s: Optional[float] = None
    ftr_ratio: Optional[float] = None
    mode: str = ""
    synth_label: str = ""
    chunks: int = 1
    parts: int = 1
    output_paths: Optional[List[str]] = None


def _split_text_for_render(text: str, max_chars: int = RENDER_CHUNK_CHARS) -> List[str]:
    """Split long text into renderer-friendly chunks without breaking most sentences."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        max_chars = max(1000, int(max_chars))
    except Exception:
        max_chars = RENDER_CHUNK_CHARS
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    def _emit():
        nonlocal current, current_len
        joined = "\n\n".join(p for p in current if p).strip()
        if joined:
            chunks.append(joined)
        current = []
        current_len = 0

    def _add_piece(piece: str):
        nonlocal current_len
        piece = (piece or "").strip()
        if not piece:
            return
        extra = len(piece) + (2 if current else 0)
        if current and current_len + extra > max_chars:
            _emit()
        current.append(piece)
        current_len += len(piece) + (2 if current_len else 0)

    def _split_oversized(piece: str):
        sentences = re.split(r"(?<=[.!?])\s+", piece.strip())
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                _add_piece(sentence)
                continue
            words = sentence.split()
            buf: List[str] = []
            buf_len = 0
            for word in words:
                if len(word) > max_chars:
                    if buf:
                        _add_piece(" ".join(buf))
                        buf = []
                        buf_len = 0
                    for i in range(0, len(word), max_chars):
                        _add_piece(word[i:i + max_chars])
                    continue
                extra = len(word) + (1 if buf else 0)
                if buf and buf_len + extra > max_chars:
                    _add_piece(" ".join(buf))
                    buf = [word]
                    buf_len = len(word)
                else:
                    buf.append(word)
                    buf_len += extra
            if buf:
                _add_piece(" ".join(buf))

    for paragraph in re.split(r"\n\s*\n+", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            _add_piece(paragraph)
        else:
            _split_oversized(paragraph)
    _emit()
    return chunks or [text]


def _open_combined_wav(path: str, source_wav: str):
    with wave.open(source_wav, "rb") as src:
        params = src.getparams()
    out = wave.open(path, "wb")
    out.setnchannels(params.nchannels)
    out.setsampwidth(params.sampwidth)
    out.setframerate(params.framerate)
    out.setcomptype(params.comptype, params.compname)
    return out, params


def _append_wav_to_open_writer(source_wav: str, writer, expected_params) -> int:
    with wave.open(source_wav, "rb") as src:
        params = src.getparams()
        comparable = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
        expected = (expected_params.nchannels, expected_params.sampwidth, expected_params.framerate, expected_params.comptype)
        if comparable != expected:
            raise RuntimeError(
                "Renderer produced inconsistent WAV format between chunks "
                f"({comparable!r} vs {expected!r})."
            )
        frames = src.getnframes()
        while True:
            data = src.readframes(16384)
            if not data:
                break
            writer.writeframesraw(data)
        return int(frames) * int(params.nchannels) * int(params.sampwidth)


def _wav_duration_seconds(path: str) -> Optional[float]:
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        if rate:
            return frames / float(rate)
    except Exception:
        return None
    return None


def _atomic_replace(src_path: str, dest_path: str):
    tmp_dest = dest_path + ".tmp"
    try:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)
    except Exception:
        pass
    shutil.copy2(src_path, tmp_dest)
    os.replace(tmp_dest, dest_path)
    try:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)
    except Exception:
        pass


def _do_render_impl():
    parent = getattr(gui, "mainFrame", None) or wx.GetApp().GetTopWindow()

    # 1) Choose synth + base record dir
    sd = SynthSelectDialog(parent)
    try:
        if _show_modal(sd) != wx.ID_OK:
            return
        synth_meta = sd.get_choice()
    finally:
        try:
            sd.Destroy()
        except Exception:
            pass

    kind = synth_meta.get("kind", "")
    synth_label = synth_meta.get("label", "")

    if kind in ("sep", ""):
        return

    nvda_name = ""
    if kind == "nvda":
        nvda_name = synth_meta.get("nvdaName", "")
        synth_label = synth_meta.get("label", "") or nvda_name or "NVDA synth"

    # 2) Input text
    text, base = _pick_input_text(parent)
    if not text:
        return

    # 3) Per-synth options
    sonata_opts = None
    sapi_opts = None
    sapi32_opts = None
    orpheus_opts = None
    dectalk_opts = None
    nvda_opts = None
    if kind == "sonata":
        try:
            od = SonataOptionsDialog(parent)
        except Exception as e:
            _error(str(e))
            return
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            sonata_opts = od.get_options(persist=True)
        finally:
            try:
                od.Destroy()
            except Exception:
                pass
        synth_label = "Sonata"
    elif kind == "sapi5":
        od = Sapi5OptionsDialog(parent)
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            sapi_opts = od.get_options(persist=True)
        finally:
            try:
                od.Destroy()
            except Exception:
                pass
        synth_label = "SAPI5"

    elif kind == "sapi5_32":
        od = Sapi5OptionsDialog(
            parent,
            title="soundWave - SAPI5 32-bit options",
            voice_list_fn=_list_sapi5_32_voices,
            render_fn=_render_with_sapi5_32,
            cfg_prefix="sapi532",
        )
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            sapi32_opts = od.get_options(persist=True)
        finally:
            try:
                od.Destroy()
            except Exception:
                pass
        synth_label = "SAPI5 32-bit"

    elif kind == "dectalk":
        od = DectalkOptionsDialog(parent)
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            dectalk_opts = od.get_options(persist=True)
        finally:
            try:
                od.Destroy()
            except Exception:
                pass
        synth_label = "DecTalk"
    
    
    elif kind == "bestspeech":
        od = BestSpeechOptionsDialog(parent)
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            bs_opts = od.get_options(persist=True)
        finally:
            try:
                od.Destroy()
            except Exception:
                pass
        synth_label = "Keynote Gold"
    elif kind == "orpheus":
        synth_label = "Orpheus"
        # Orpheus offline capture reuses the live Orpheus instance, so Orpheus must be the current NVDA synth.
        try:
            live = synthDriverHandler.getSynth()
        except Exception:
            live = None
        if (live is None) or ((getattr(live, "name", "") or "").lower() != "orpheus"):
            _error("Orpheus offline capture requires Orpheus to be your current NVDA synthesizer.\n\n" "Please switch NVDA to Orpheus, then open soundWave again.")
            return
    
        init = {
            "languageId": _cfg_get("orpheusLanguageId", "") or "",
            "variantId": _cfg_get("orpheusVariantId", "") or "",
            "speed": int(_cfg_get("orpheusSpeed", 100) or 100),
            "autoTest": _get_cfg_bool("autoTestOnChangeOrpheus", True),
        }
        od = OrpheusOptionsDialog(parent, live, initial=init)
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            orpheus_opts = od.get_options()
            _cfg_set("orpheusLanguageId", str(orpheus_opts.get("languageId", "") or ""))
            _cfg_set("orpheusVariantId", str(orpheus_opts.get("variantId", "") or ""))
            _cfg_set("orpheusSpeed", int(orpheus_opts.get("speed", 100) or 100))
            _set_cfg_bool("autoTestOnChangeOrpheus", bool(orpheus_opts.get("autoTest", True)))
        finally:
            try:
                od.Destroy()
            except Exception:
                pass
    
    elif kind == "ibmeci":
        synth_label = "IBM ECI"
        auto_eci_dll = _find_ibmeci_dll()
        init = {
            "dllPath": (_cfg_get("ibmeciDllPath", "") or "").strip() or (os.environ.get("SOUNDWAVE_IBMECI_DLL", "") or "").strip() or auto_eci_dll,
            "voiceId": int(_cfg_get("ibmeciVoiceId", 0) or 0),
            "speed": int(_cfg_get("ibmeciSpeed", 110) or 110),
            "autoTest": _get_cfg_bool("autoTestOnChangeIbmEci", True),
        }
        od = IbmEciOptionsDialog(parent, initial=init)
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            eci = od.get_options()
            _cfg_set("ibmeciDllPath", eci.get("dllPath", "") or "")
            _cfg_set("ibmeciVoiceId", int(eci.get("voiceId", 0) or 0))
            _cfg_set("ibmeciSpeed", int(eci.get("speed", 110) or 110))
            _set_cfg_bool("autoTestOnChangeIbmEci", bool(eci.get("autoTest", True)))
        finally:
            try:
                od.Destroy()
            except Exception:
                pass

    elif kind == "nvda":
        try:
            od = GenericNvdaOptionsDialog(parent, nvda_name, synth_label)
        except Exception as e:
            try:
                log.error("soundWave: generic NVDA options failed for %s (%s)" % (synth_label, nvda_name), exc_info=True)
            except Exception:
                pass
            msg = str(e).strip() or f"{synth_label or nvda_name or 'This synth'} could not open its options dialog."
            _error(msg)
            return
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            nvda_opts = od.get_options(persist=True)
        finally:
            try:
                od.Destroy()
            except Exception:
                pass

    # 4) Output path (defaults into record base dir / synth subdir)
    suggestion_base = _build_suggested_output_base(base or "output", synth_label)
    out_path, fmt = _pick_output_path(parent, suggestion_base=suggestion_base, synth_label=synth_label)
    if not out_path:
        return

    cancel_evt = threading.Event()
    result = RenderResult(synth_label=synth_label)

    prog = _RenderProgressDialog(parent)
    try:
        prog._cancel_evt = cancel_evt
    except Exception:
        pass
    try:
        prog.Show()
    except Exception:
        prog = None
        ui.message("Rendering started.")

    tmp_dir = tempfile.mkdtemp(prefix="soundWave_")
    tmp_wav = os.path.join(tmp_dir, "render.wav")

    # Orpheus must not remain the live NVDA synth during capture rendering, or NVDA can hang/crash.
    # We temporarily switch the live synth to a safe fallback (default: eSpeak) while rendering,
    # while keeping the Orpheus instance alive for offline capture.
    _orpheus_live = None
    _orpheus_switched = False
    _orpheus_patched = False
    _orpheus_orig_terminate = None
    _orpheus_orig_cancel = None

    if kind == "orpheus":
        try:
            _orpheus_live = synthDriverHandler.getSynth()
        except Exception:
            _orpheus_live = None

        if (_orpheus_live is None) or ((getattr(_orpheus_live, "name", "") or "").lower() != "orpheus"):
            # We already gate this earlier, but keep it defensive.
            _error(
                "Orpheus offline capture requires Orpheus to be your current NVDA synthesizer.\n\n"
                "Please switch NVDA to Orpheus, then open soundWave again."
            )
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            return

        # Patch terminate/cancel to prevent NVDA unloading Orpheus while we switch away.
        try:
            _orpheus_orig_terminate = getattr(_orpheus_live, "terminate", None)
            _orpheus_orig_cancel = getattr(_orpheus_live, "cancel", None)

            def _noop(*args, **kwargs):
                return None

            if callable(_orpheus_orig_terminate):
                _orpheus_live.terminate = _noop
            if callable(_orpheus_orig_cancel):
                _orpheus_live.cancel = _noop
            _orpheus_patched = True
        except Exception:
            _orpheus_patched = False

        # Switch live synth away from Orpheus.
        fallback_synth = _pick_fallback_synth_name() or ORPHEUS_FALLBACK_SYNTH
        if not _safe_set_synth(fallback_synth):
            # Restore patched methods before bailing.
            try:
                if _orpheus_patched and _orpheus_live is not None:
                    if _orpheus_orig_terminate is not None:
                        _orpheus_live.terminate = _orpheus_orig_terminate
                    if _orpheus_orig_cancel is not None:
                        _orpheus_live.cancel = _orpheus_orig_cancel
            except Exception:
                pass
            _error(
                "Orpheus rendering requires switching NVDA temporarily to a fallback synthesizer, "
                f"but switching to '{fallback_synth}' failed.\n\n"
                "Please ensure at least one non-Orpheus NVDA synthesizer is available, then try again."
            )
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            return
        try:
            log.info("soundWave: switched live synth to fallback '%s' for Orpheus capture" % fallback_synth)
        except Exception:
            pass

        _orpheus_switched = True

    def worker():
        nonlocal tmp_wav
        wall0 = time.time()
        try:
            try:
                log.info('soundWave: render worker started (kind=%s, synth=%s, chars=%d)' % (kind, synth_label, len(text or "")))
            except Exception:
                pass

            def _render_one(chunk_text: str, chunk_wav: str) -> str:
                if kind == "sonata":
                    return _render_with_sonata_offline(
                        text=chunk_text,
                        out_wav=chunk_wav,
                        cancel_evt=cancel_evt,
                        voice_config_path=str(sonata_opts["voice_config_path"]),
                        speaker=str(sonata_opts["speaker"]),
                        speed_percent=int(sonata_opts["speed_percent"]),
                    )

                if kind == "dectalk":
                    _render_with_dectalk_offline(
                        text=chunk_text,
                        out_wav=chunk_wav,
                        voice=str((dectalk_opts or {}).get("voice", "Paul")),
                        rate=int((dectalk_opts or {}).get("rate", 180) or 180),
                        cancel_evt=cancel_evt,
                        progress=result.progress,
                    )
                    return "DECtalk"

                if kind == "bestspeech":
                    _render_with_bestspeech_offline(
                        text=chunk_text,
                        out_wav=chunk_wav,
                        voice=str((bs_opts or {}).get("voice", "fred")),
                        rate=int((bs_opts or {}).get("rate", 90) or 90),
                        rate_boost=bool((bs_opts or {}).get("rateBoost", False)),
                        cancel_evt=cancel_evt,
                        progress=result.progress,
                    )
                    return "Keynote Gold"

                if kind == "orpheus":
                    if _orpheus_live is None:
                        raise RuntimeError("Orpheus capture couldn't access the live Orpheus instance.")
                    return _render_with_orpheus_capture(chunk_text, chunk_wav, _orpheus_live, opts=orpheus_opts)

                if kind == "ibmeci":
                    dll_path = (_cfg_get("ibmeciDllPath", "") or "").strip() or (os.environ.get("SOUNDWAVE_IBMECI_DLL", "") or "").strip() or _find_ibmeci_dll()
                    if not dll_path or not os.path.isfile(dll_path):
                        raise RuntimeError(
                            "IBM ECI needs the ECI DLL path.\n\n"
                            "Set the IBM ECI DLL path in the soundWave synth dialog (or set the SOUNDWAVE_IBMECI_DLL environment variable) "
                            "(e.g. C:\\IBM\\eci.dll), then try again."
                        )
                    _render_with_ibmeci_dll(
                        chunk_text,
                        chunk_wav,
                        dll_path,
                        voice_id=int(_cfg_get('ibmeciVoiceId', 0) or 0),
                        sample_rate_param=2,
                        speed=int(_cfg_get('ibmeciSpeed', 110) or 110),
                        progress=result.progress,
                        cancel_evt=cancel_evt,
                    )
                    return "IBMECI"

                if kind == "nvda":
                    joined = f"{nvda_name} {synth_label}".lower()
                    if "speech api version 5" in joined or "_sapi5" in joined:
                        return _render_with_sapi5_32(
                            chunk_text,
                            chunk_wav,
                            voice_name=str(_cfg_get("sapi532VoiceName", "") or "") or None,
                            rate=int(_cfg_get("sapi532Rate", 0) or 0),
                        )
                    return _render_with_nvda_generic_capture(
                        chunk_text,
                        chunk_wav,
                        nvda_name,
                        cancel_evt=cancel_evt,
                        progress=result.progress,
                        opts=nvda_opts,
                    )

                if kind == "sapi5_32":
                    voice_name = str((sapi32_opts or {}).get("voiceName", "") or "") or None
                    rate = int((sapi32_opts or {}).get("rate", 0) or 0)
                    return _render_with_sapi5_32(chunk_text, chunk_wav, voice_name=voice_name, rate=rate)

                voice_name = str(_cfg_get('sapi5VoiceName', '') or '') or None
                rate = int(_cfg_get('sapi5Rate', 0) or 0)
                _render_with_sapi5(chunk_text, chunk_wav, voice_name=voice_name, rate=rate)
                return "SAPI5"

            text_chunks = _split_text_for_render(text, RENDER_CHUNK_CHARS) if len(text or "") >= CHUNK_RENDER_MIN_CHARS else [text]
            result.chunks = max(1, len(text_chunks))
            result.progress = {
                "chunksTotal": result.chunks,
                "chunksDone": 0,
                "bytes": 0,
                "started_ts": time.time(),
            }
            part_paths: List[str] = []
            current_writer = None
            current_params = None
            current_part_bytes = 0
            current_part_index = 1

            def _close_current_part():
                nonlocal current_writer, current_params, current_part_bytes
                if current_writer is not None:
                    try:
                        current_writer.close()
                    finally:
                        current_writer = None
                    part_paths.append(os.path.join(tmp_dir, "render_part%03d.wav" % current_part_index))
                    current_params = None
                    current_part_bytes = 0

            for idx, chunk_text in enumerate(text_chunks, start=1):
                if cancel_evt.is_set():
                    raise RuntimeError("Cancelled.")
                result.progress["chunksCurrent"] = idx
                result.progress["chunksTotal"] = result.chunks
                chunk_wav = os.path.join(tmp_dir, "chunk%05d.wav" % idx)
                mode = _render_one(chunk_text, chunk_wav)
                if mode:
                    result.mode = mode
                if not os.path.isfile(chunk_wav):
                    raise FileNotFoundError("Chunk render output was not created: %s" % (chunk_wav,))
                with wave.open(chunk_wav, "rb") as chunk_reader:
                    params = chunk_reader.getparams()
                    chunk_bytes = int(chunk_reader.getnframes()) * int(params.nchannels) * int(params.sampwidth)
                if current_writer is not None and current_part_bytes > 0 and current_part_bytes + chunk_bytes > MAX_WAV_DATA_BYTES:
                    _close_current_part()
                    current_part_index += 1
                if current_writer is None:
                    part_wav = os.path.join(tmp_dir, "render_part%03d.wav" % current_part_index)
                    current_writer, current_params = _open_combined_wav(part_wav, chunk_wav)
                current_part_bytes += _append_wav_to_open_writer(chunk_wav, current_writer, current_params)
                result.progress["chunksDone"] = idx
                result.progress["bytes"] = int(result.progress.get("bytes", 0) or 0) + chunk_bytes
                result.progress["last_audio_ts"] = time.time()
                try:
                    os.remove(chunk_wav)
                except Exception:
                    pass

            _close_current_part()
            if not part_paths:
                raise RuntimeError("Render failed: no audio parts were produced.")
            result.parts = len(part_paths)
            tmp_wav = part_paths[0]

            result.wall_s = max(0.001, time.time() - wall0)

            durations = [_wav_duration_seconds(p) for p in part_paths]
            result.audio_s = sum(d for d in durations if d is not None) if any(d is not None for d in durations) else None

            if cancel_evt.is_set():
                raise RuntimeError("Cancelled.")

            # write output
            output_paths: List[str] = []
            if len(part_paths) == 1:
                if fmt == "wav":
                    _atomic_replace(part_paths[0], out_path)
                else:
                    _convert_with_ffmpeg(part_paths[0], out_path)
                output_paths.append(out_path)
            else:
                base_out, ext_out = os.path.splitext(out_path)
                ext = ".mp3" if fmt == "mp3" else ".wav"
                for idx, part_wav in enumerate(part_paths, start=1):
                    part_out = "%s.part%03d%s" % (base_out, idx, ext)
                    if fmt == "wav":
                        _atomic_replace(part_wav, part_out)
                    else:
                        _convert_with_ffmpeg(part_wav, part_out)
                    output_paths.append(part_out)
            result.output_paths = output_paths

            if result.audio_s and result.audio_s > 0:
                result.ftr_ratio = result.wall_s / result.audio_s

            result.ok = True

        except Exception as e:
            result.err = e
            log.error("soundWave: render failed: %s" % e, exc_info=True)
        finally:
            try:
                log.info('soundWave: render worker finished (ok=%s, err=%s)' % (result.ok, bool(result.err)))
            except Exception:
                pass
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    started_at = time.time()
    _last_ui = {'ts': 0.0}
    _poll_state = {'cancel_ts': None, 'forced': False}
    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def finish():
        # Always restore Orpheus live synth and methods if we switched away.
        if kind == "orpheus":
            try:
                # Restore original Orpheus methods first.
                if _orpheus_patched and _orpheus_live is not None:
                    if _orpheus_orig_terminate is not None:
                        _orpheus_live.terminate = _orpheus_orig_terminate
                    if _orpheus_orig_cancel is not None:
                        _orpheus_live.cancel = _orpheus_orig_cancel
            except Exception:
                pass

            if _orpheus_switched:
                # Attempt 1: restore using instance, then verify.
                try:
                    _safe_set_synth(_orpheus_live)
                except Exception:
                    pass
                if _get_current_synth_name() != "orpheus":
                    # Attempt 2: close captured engine (best-effort), then restore by name.
                    try:
                        if callable(_orpheus_orig_terminate):
                            _orpheus_orig_terminate()
                    except Exception:
                        pass
                    try:
                        _safe_set_synth("orpheus")
                    except Exception:
                        pass

                if _get_current_synth_name() != "orpheus":
                    _info(
                        "Render finished, but soundWave couldn't restore Orpheus as your live synthesizer. "
                        "You may need to switch it back manually in NVDA's Synthesizer settings."
                    )

        if prog is not None:
            try:
                prog.Destroy()
            except Exception:
                pass

        if cancel_evt.is_set() and not result.ok:
            _info("Cancelled.")
            return

        if result.ok:
            saved = out_path
            if result.output_paths:
                if len(result.output_paths) == 1:
                    saved = result.output_paths[0]
                else:
                    saved = "%d files, starting with: %s" % (len(result.output_paths), result.output_paths[0])
            msg = [
                "Render complete.",
                f"Synth: {result.synth_label}",
                f"Saved to: {saved}",
                f"Backend: {result.mode or 'unknown'}",
                f"Time taken: {result.wall_s:.3f} s",
            ]
            if result.chunks and result.chunks > 1:
                msg.append("Text chunks: %d" % result.chunks)
            if result.parts and result.parts > 1:
                msg.append("Audio files: %d" % result.parts)
            if result.audio_s:
                msg.append(f"Audio length: {result.audio_s:.3f} s")
            if result.ftr_ratio is not None:
                speed_x = None
                try:
                    if result.audio_s and result.wall_s:
                        speed_x = float(result.audio_s) / float(result.wall_s)
                except Exception:
                    speed_x = None
                if speed_x is not None:
                    msg.append('Speed: %.2fx realtime' % (speed_x,))
                else:
                    msg.append('Speed: (unknown)')
            _info("\n".join(msg))
        else:
            _error(str(result.err or "Render failed."))


    def poll():
        # Stop polling if the progress dialog is closing/closed.
        try:
            if prog is not None and getattr(prog, '_closing', False):
                return
        except Exception:
            return
        # Mirror UI cancel state into the shared cancel event.
        if prog is not None and prog.cancelled:
            cancel_evt.set()

        # If cancelled (or timed out) and the worker thread doesn't exit promptly,
        # force-close the dialog so the user isn't trapped.
        now = time.time()
        if cancel_evt.is_set() and not _poll_state.get('forced'):
            if _poll_state.get('cancel_ts') is None:
                _poll_state['cancel_ts'] = now
            elif (now - float(_poll_state.get('cancel_ts') or now)) >= 2.0:
                _poll_state['forced'] = True
                try:
                    if result.err is None and not result.ok:
                        result.err = RuntimeError('Cancelled.')
                except Exception:
                    pass
                try:
                    log.warning('soundWave: forcing render dialog closed after cancel; worker still alive')
                except Exception:
                    pass
                wx.CallAfter(finish)
                return

        # Hard watchdog: don't allow the progress UI to hang forever.
        allowed_seconds = float(TIMEOUT_SECONDS or 300) * float(max(1, int(getattr(result, "chunks", 1) or 1)))
        if (now - started_at) > allowed_seconds:
            try:
                if not result.ok and result.err is None:
                    result.err = RuntimeError('Render timed out.')
            except Exception:
                pass
            try:
                log.error('soundWave: render timed out (UI watchdog); forcing dialog closed')
            except Exception:
                pass
            wx.CallAfter(finish)
            return

        if t.is_alive():
            # Update UI at ~5Hz for responsiveness, but only refresh the details text ~1Hz.
            if prog is not None:
                now = time.time()
                elapsed = max(0.0, now - started_at)
                label = result.mode or synth_label or 'Rendering'

                # Human-friendly summary
                prog.set_summary('%s… Elapsed %.0f s' % (label, elapsed,))

                # Details (once per second)
                if (now - float(_last_ui.get('ts', 0.0) or 0.0)) >= 1.0:
                    _last_ui['ts'] = now
                    details = []
                    details.append('Synth: %s' % (label,))
                    details.append('Elapsed time: %.1f s' % (elapsed,))

                    # Renderer-provided telemetry (optional)
                    try:
                        p = result.progress if isinstance(result.progress, dict) else None
                    except Exception:
                        p = None

                    if p:
                        try:
                            total_chunks = int(p.get('chunksTotal', 0) or 0)
                            done_chunks = int(p.get('chunksDone', 0) or 0)
                            current_chunk = int(p.get('chunksCurrent', done_chunks) or done_chunks)
                            if total_chunks > 1:
                                details.append('Text chunk: %d of %d (%d complete)' % (current_chunk, total_chunks, done_chunks))
                        except Exception:
                            pass

                        # Rendered audio seconds (if bytes + rate available)
                        rendered_s = None
                        try:
                            b = int(p.get('bytes', 0) or 0)
                            sr = int(p.get('pcm_rate', 0) or 0)
                            ch = int(p.get('channels', 1) or 1)
                            sw = int(p.get('sampwidth', 2) or 2)
                            if b > 0 and sr > 0 and ch > 0 and sw > 0:
                                rendered_s = b / float(sr * ch * sw)
                        except Exception:
                            rendered_s = None

                        if rendered_s is not None:
                            details.append('Synth rendered: %.1f s audio' % (rendered_s,))
                            if elapsed > 0.05:
                                details.append('Render speed: %.2fx realtime' % (rendered_s / elapsed,))

                        try:
                            buf = int(p.get('buffers', 0) or 0)
                            details.append('Audio chunks: %d' % (buf,))
                        except Exception:
                            pass

                        try:
                            last = p.get('last_audio_ts', None)
                            if last:
                                age = max(0.0, now - float(last))
                                details.append('Last audio: %.1f s ago' % (age,))
                        except Exception:
                            pass

                    prog.set_details_lines(details)

            wx.CallLater(200, poll)
        else:
            wx.CallAfter(finish)


    # Kick off UI polling
    wx.CallLater(50, poll)
class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "soundWave"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._updater = None
        if GitHubReleaseUpdater:
            self._updater = GitHubReleaseUpdater("soundWave", "soundWave", "OnjLouis", "soundWave")
            self._updater.start()

    def terminate(self):
        if self._updater:
            self._updater.stop()
        return super().terminate()

    def script_renderFTR(self, gesture):
        # Never show dialogs in the script callback: schedule onto wx loop.
        wx.CallAfter(_do_render_impl)

    @script(description=_("Check for soundWave updates"))
    def script_checkForSoundWaveUpdate(self, gesture):
        if self._updater:
            wx.CallAfter(self._updater.checkNow, True)
        else:
            ui.message(_("Updater is not available"))

    __gestures = {
        "kb:NVDA+control+=": "renderFTR",
    }
