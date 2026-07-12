# -*- coding: utf-8 -*-
"""soundWave hub.

This module owns the NVDA global plugin, shared dialogs, render orchestration,
chunking, output naming, and progress UI. Specialist synth integrations live in
``soundWave_lib.synths``.
"""

from __future__ import annotations

import os
import importlib
import re
import sys
import subprocess
import time
import wave
import shutil
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import wx

from soundWave_lib import runtime as _runtime


def _ensure_dir(path: str) -> None:
    """Create directory if it doesn't exist (idempotent)."""
    if not path:
        return
    os.makedirs(path, exist_ok=True)

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
    cb = wx.CheckBox(parent, label="&Auto speak when changing settings")
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


def _bind_numeric_page_keys(ctrl, minimum: int, maximum: int, page_step: int = 10, callback=None):
    """Give wx.SpinCtrl predictable PageUp/PageDown/Home/End keyboard behavior."""
    def _on_char(evt):
        try:
            key = evt.GetKeyCode()
            if key not in (wx.WXK_PAGEUP, wx.WXK_PAGEDOWN, wx.WXK_HOME, wx.WXK_END):
                evt.Skip()
                return
            cur = int(ctrl.GetValue())
            if key == wx.WXK_PAGEUP:
                value = cur + int(page_step)
            elif key == wx.WXK_PAGEDOWN:
                value = cur - int(page_step)
            elif key == wx.WXK_HOME:
                value = int(minimum)
            else:
                value = int(maximum)
            value = max(int(minimum), min(int(maximum), int(value)))
            ctrl.SetValue(value)
            if callback is not None:
                wx.CallAfter(callback, None)
        except Exception:
            try:
                evt.Skip()
            except Exception:
                pass
    try:
        ctrl.Bind(wx.EVT_CHAR_HOOK, _on_char)
    except Exception:
        pass



try:
    import nvwave
except Exception:
    nvwave = None


ADDON_NAME = "soundWave"
CFG_SECTION = "soundWave"
TIMEOUT_SECONDS = 300
ORPHEUS_FALLBACK_SYNTH = "espeak"
RENDER_CHUNK_CHARS = 12000
CHUNK_RENDER_MIN_CHARS = 24000
SUPERTONIC_RENDER_CHUNK_CHARS = 700
MAX_WAV_DATA_BYTES = 3600 * 1024 * 1024
DEFAULT_FILENAME_PATTERN = "%source% - %engine% - %voice%"
DEFAULT_SINGLE_FOLDER_PATTERN = "%engine% - %voice%"
DEFAULT_SINGLE_FILE_PATTERN = "%source%"
DEFAULT_BATCH_FOLDER_PATTERN = "%engine% - %voice%"
DEFAULT_BATCH_FILE_PATTERN = "%number% - %source%"
_AUTO_OPENED_FOLDERS = set()


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
        if key == wx.WXK_F1:
            _open_manual()
            return
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


def _manual_path() -> str:
    try:
        addon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = [
            os.path.join(addon_root, "doc", "en", "readme.html"),
            os.path.join(addon_root, "doc", "readme.html"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
    except Exception:
        pass
    return ""


def _open_manual():
    path = _manual_path()
    if not path:
        _error("The soundWave manual could not be found.")
        return
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception as e:
        _error("The soundWave manual could not be opened:\n%s" % e)


class _HelpButtonAccessible(getattr(wx, "Accessible", object)):
    def __init__(self, window):
        try:
            super().__init__(window)
        except TypeError:
            try:
                super().__init__()
            except Exception:
                pass
        self.window = window

    def GetName(self, childId):
        return (getattr(wx, "ACC_OK", 0), "Help")

    def GetRole(self, childId):
        return (getattr(wx, "ACC_OK", 0), getattr(wx, "ROLE_SYSTEM_PUSHBUTTON", 0x2B))

    def GetKeyboardShortcut(self, childId):
        return (getattr(wx, "ACC_OK", 0), "Alt+H")


def _create_help_button(parent) -> wx.Button:
    btn = wx.Button(parent, label="&Help")
    btn.Bind(wx.EVT_BUTTON, lambda evt: _open_manual())
    try:
        btn.SetName("Help")
        btn.SetToolTip("Open the soundWave manual. Shortcut: Alt+H.")
    except Exception:
        pass
    try:
        if hasattr(wx, "Accessible") and hasattr(btn, "SetAccessible"):
            acc = _HelpButtonAccessible(btn)
            btn.SetAccessible(acc)
            btn._soundWaveAccessible = acc
    except Exception:
        pass
    return btn


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


def _open_folder_for_path(path: str) -> bool:
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if folder and os.path.isdir(folder):
            os.startfile(folder)  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    return False


def _auto_open_folder_for_path(path: str) -> bool:
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        folder_key = os.path.normcase(os.path.abspath(folder or ""))
        if not folder_key or folder_key in _AUTO_OPENED_FOLDERS:
            return False
        if _open_folder_for_path(folder):
            _AUTO_OPENED_FOLDERS.add(folder_key)
            return True
    except Exception:
        pass
    return False


def _play_output_file(path: str) -> bool:
    try:
        if not path or not os.path.isfile(path):
            return False
        os.startfile(path)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _play_output_files(paths: List[str]) -> bool:
    files = [p for p in (paths or []) if p and os.path.isfile(p)]
    if not files:
        return False
    if len(files) == 1:
        return _play_output_file(files[0])
    try:
        playlist = os.path.join(tempfile.gettempdir(), "soundWave-last-render.m3u")
        with open(playlist, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for path in files:
                f.write("#EXTINF:-1,%s\n" % os.path.basename(path))
                f.write(path + "\n")
        os.startfile(playlist)  # type: ignore[attr-defined]
        return True
    except Exception:
        ok = False
        for path in files:
            ok = _play_output_file(path) or ok
        return ok


def _show_render_complete(parent, message: str, output_paths: List[str]) -> None:
    first_path = output_paths[0] if output_paths else ""
    if _cfg_get_bool("autoPlayAfterRender", False) and first_path:
        _play_output_files(output_paths)
    if _cfg_get_bool("autoOpenFolderAfterRender", False) and first_path:
        _auto_open_folder_for_path(first_path)
    if not _cfg_get_bool("showCompletionSummary", True):
        return

    dlg = wx.Dialog(parent, title=ADDON_NAME, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    sizer = wx.BoxSizer(wx.VERTICAL)
    text = wx.TextCtrl(dlg, value=message, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP)
    sizer.Add(text, 1, wx.ALL | wx.EXPAND, 10)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    buttons.AddStretchSpacer(1)
    open_btn = wx.Button(dlg, label="&Open folder")
    ok_btn = wx.Button(dlg, wx.ID_OK)
    buttons.Add(open_btn, 0, wx.RIGHT, 8)
    buttons.Add(ok_btn, 0)
    sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

    def _on_open(evt):
        if first_path:
            _open_folder_for_path(first_path)

    open_btn.Bind(wx.EVT_BUTTON, _on_open)
    dlg.SetSizer(sizer)
    dlg.SetMinSize((560, 260))
    dlg.Fit()
    try:
        ok_btn.SetDefault()
    except Exception:
        pass
    try:
        text.SetFocus()
    except Exception:
        pass
    try:
        _show_modal(dlg)
    finally:
        try:
            dlg.Destroy()
        except Exception:
            pass


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


def _clean_text_for_render(text: str) -> str:
    """Remove blank-only lines before text reaches any synth renderer."""
    lines = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines).strip()



TEXT_INPUT_EXTENSIONS = {".txt", ".text", ".md", ".markdown", ".log", ".srt"}


def _read_text_file(path: str) -> Optional[str]:
    last_err = None
    for enc in ("utf-8-sig", "utf-16", "mbcs"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except Exception as e:
            last_err = e
    raise RuntimeError(str(last_err or "unknown read error"))


def _pick_input_items(parent) -> List[Dict[str, str]]:
    """Pick input text for rendering.

    Returns item dictionaries with text/base/path. Folder input returns one item per text-like file.
    """
    input_source = 3
    while True:
        try:
            source_dlg = wx.SingleChoiceDialog(
                parent,
                "Choose input source:",
                ADDON_NAME,
                ["Use clipboard text", "Open text file", "Open text folder", "Type or paste text"],
            )
            try:
                res = source_dlg.ShowModal()
                if res != wx.ID_OK:
                    return []
                input_source = source_dlg.GetSelection()
            finally:
                try:
                    source_dlg.Destroy()
                except Exception:
                    pass
        except Exception:
            input_source = 3

        if input_source != 0:
            break

        clip = _get_clipboard_text().strip()
        if clip:
            return [{"text": clip, "base": "Clipboard", "path": ""}]
        try:
            gui.messageBox("No text on clipboard.", ADDON_NAME, wx.OK | wx.ICON_INFORMATION)
        except Exception:
            pass

    if input_source == 1:
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
                return []
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
        try:
            txt = _read_text_file(in_path)
        except Exception as e:
            _error(f"Couldn't read input file:\n{e}")
            return []
        txt = txt.strip()
        if not txt:
            _error("The selected text file is empty.")
            return []
        return [{"text": txt, "base": os.path.splitext(os.path.basename(in_path))[0] or "file", "path": in_path}]

    if input_source == 2:
        last_input_dir = str(_cfg_get("lastInputDir", "") or "")
        if not last_input_dir or not os.path.isdir(os.path.expandvars(os.path.expanduser(last_input_dir))):
            last_input_dir = os.path.expanduser("~")
        dlg = wx.DirDialog(
            parent,
            "Open text folder",
            defaultPath=os.path.expandvars(os.path.expanduser(last_input_dir)),
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        try:
            if _show_modal(dlg) != wx.ID_OK:
                return []
            folder = dlg.GetPath()
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass
        try:
            _cfg_set("lastInputDir", folder)
        except Exception:
            pass
        items: List[Dict[str, str]] = []
        for name in sorted(os.listdir(folder), key=lambda x: x.lower()):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext and ext not in TEXT_INPUT_EXTENSIONS:
                continue
            try:
                txt = (_read_text_file(path) or "").strip()
            except Exception:
                continue
            if not txt:
                continue
            items.append({"text": txt, "base": os.path.splitext(name)[0] or "file", "path": path})
        if not items:
            _error("The selected folder does not contain any readable text files.")
        return items

    # Ask for manual input (multi-line).
    clip = _get_clipboard_text().strip()
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
                return []
            txt = (dlg.GetValue() or "").strip()
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass
    except Exception as e:
        _error(f"Couldn't open input dialog: {e}")
        return []

    if not txt:
        return []
    return [{"text": txt, "base": "Typed", "path": ""}]


def _pick_input_text(parent) -> tuple[str, str]:
    items = _pick_input_items(parent)
    if not items:
        return "", ""
    return items[0].get("text", ""), items[0].get("base", "Typed")

class _RenderProgressDialog(wx.Dialog):
    """Common render progress dialog used for all synths.

    Provides a pulsing gauge plus an optional Details list that updates once per second.
    """
    def __init__(self, parent, title: str = "soundWave"):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._cancelled = False
        self._detailsShown = bool(_cfg_get_bool("renderDetailsShown", False))

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

        # Details (hidden by default). A list lets screen reader users move line by line.
        self.details = wx.ListBox(
            self,
            choices=["Rendering…"],
            size=(-1, 120),
        )
        self.details.Hide()
        self.details.Disable()
        sizer.Add(self.details, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        btnSizer = wx.BoxSizer(wx.HORIZONTAL)

        self.detailsBtn = wx.Button(self, label="Show &details")
        self.detailsBtn.Bind(wx.EVT_BUTTON, self._on_toggle_details)
        btnSizer.Add(self.detailsBtn, 0, wx.ALL, 10)

        self.helpBtn = _create_help_button(self)
        btnSizer.Add(self.helpBtn, 0, wx.ALL, 10)

        btnSizer.AddStretchSpacer(1)

        self.cancelBtn = wx.Button(self, label="&Cancel")
        self.cancelBtn.Bind(wx.EVT_BUTTON, self._on_cancel)
        btnSizer.Add(self.cancelBtn, 0, wx.ALL, 10)

        sizer.Add(btnSizer, 0, wx.EXPAND)

        self.SetSizer(sizer)
        self.SetMinSize((520, 200))
        if self._detailsShown:
            self._apply_details_visibility(set_focus=False)
        self.Fit()

        # Allow ESC to cancel
        try:
            self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        except Exception:
            pass

        # Tab order: Show/Hide details -> Cancel (and when details are shown: Details -> Show/Hide details -> Cancel)
        try:
            self.helpBtn.MoveAfterInTabOrder(self.detailsBtn)
            self.cancelBtn.MoveAfterInTabOrder(self.helpBtn)
        except Exception:
            pass

    @property
    def cancelled(self) -> bool:
        return bool(self._cancelled)

    def _on_char_hook(self, evt):
        try:
            key = evt.GetKeyCode()
            if key == wx.WXK_F1:
                _open_manual()
                return
            if key == wx.WXK_ESCAPE:
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
        _cfg_set("renderDetailsShown", self._detailsShown)
        self._apply_details_visibility(set_focus=True)

    def _apply_details_visibility(self, set_focus: bool = False):
        if self._detailsShown:
            self.details.Enable()
            # Populate immediately so the first focus landing isn't an empty list.
            if self.details.GetCount() <= 0:
                self.set_details_lines([str(self.summary.GetLabel() or "Rendering…")])
            self.details.Show()
            self.detailsBtn.SetLabel("Hide &details")
            try:
                self.detailsBtn.MoveAfterInTabOrder(self.details)
                self.helpBtn.MoveAfterInTabOrder(self.detailsBtn)
                self.cancelBtn.MoveAfterInTabOrder(self.helpBtn)
            except Exception:
                pass
            if set_focus:
                try:
                    self.details.SetFocus()
                except Exception:
                    pass
        else:
            self.details.Hide()
            self.details.Disable()
            self.detailsBtn.SetLabel("Show &details")
            try:
                self.helpBtn.MoveAfterInTabOrder(self.detailsBtn)
                self.cancelBtn.MoveAfterInTabOrder(self.helpBtn)
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
            cleaned = [str(x) for x in (lines or []) if str(x).strip()]
            if not cleaned:
                cleaned = ["Rendering…"]
            selection = self.details.GetSelection()
            self.details.Set(cleaned)
            if cleaned:
                if 0 <= selection < len(cleaned):
                    self.details.SetSelection(selection)
                else:
                    self.details.SetSelection(0)
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


def _get_record_base_dir() -> str:
    base = _cfg_get("recordBaseDir", "") or ""
    return os.path.expandvars(os.path.expanduser(base))


def _get_default_record_base_dir() -> str:
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    if not os.path.isdir(docs):
        docs = os.path.expanduser("~")
    default_dir = os.path.join(docs, "SoundWave")
    _ensure_dir(default_dir)
    return default_dir


def _get_output_base_dir() -> str:
    base = _get_record_base_dir()
    if base:
        return base
    return _get_default_record_base_dir()


def _safe_filename_piece(value: str, fallback: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    return value[:80]


def _format_bytes(byte_count: int) -> str:
    try:
        size = float(max(0, int(byte_count)))
    except Exception:
        size = 0.0
    units = ("B", "KB", "MB", "GB")
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return "%d B" % int(size)
    return "%.1f %s" % (size, unit)


def _choice_label(ctrl) -> str:
    try:
        idx = ctrl.GetSelection()
        if idx == wx.NOT_FOUND or idx < 0:
            return ""
        return str(ctrl.GetString(idx) or "")
    except Exception:
        return ""


def _first_useful_label(*labels: str) -> str:
    ignored = {"", "default", "(no voices found)", "(no variants found)"}
    for label in labels:
        text = str(label or "").strip()
        if text.lower() not in ignored:
            return text
    return ""


def _clean_filename_pattern_result(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r"(\s*-\s*){2,}", " - ", value).strip(" -")
    return _safe_filename_piece(value, "output")


def _render_name_template(pattern: str, input_base: str, synth_label: str, voice_label: str = "", number_label: str = "", fallback: str = "output") -> str:
    source_part = _safe_filename_piece(input_base or "output", "output")
    engine_part = _safe_filename_piece(synth_label, "Synth")
    voice_part = _safe_filename_piece(voice_label, "")
    number_part = _safe_filename_piece(number_label, "")
    try:
        rendered = (
            pattern.replace("%source%", source_part)
            .replace("%engine%", engine_part)
            .replace("%voice%", voice_part)
            .replace("%number%", number_part)
        )
    except Exception:
        rendered = ""
    return _clean_filename_pattern_result(rendered) or _safe_filename_piece(fallback, "output")


def _configured_pattern(key: str, default: str) -> str:
    value = str(_cfg_get(key, "") or "").strip()
    if value:
        return value
    if key == "singleFilePattern":
        old = str(_cfg_get("filenamePattern", "") or "").strip()
        if old and old != DEFAULT_FILENAME_PATTERN:
            return old
    return default


def _build_template_output_dir(pattern_key: str, default_pattern: str, input_base: str, synth_label: str, voice_label: str = "", number_label: str = "") -> str:
    folder_name = _render_name_template(
        _configured_pattern(pattern_key, default_pattern),
        input_base=input_base,
        synth_label=synth_label,
        voice_label=voice_label,
        number_label=number_label,
        fallback="SoundWave",
    )
    out_dir = os.path.join(_get_output_base_dir(), folder_name)
    _ensure_dir(out_dir)
    return out_dir


def _build_template_output_base(pattern_key: str, default_pattern: str, input_base: str, synth_label: str, voice_label: str = "", number_label: str = "") -> str:
    return _render_name_template(
        _configured_pattern(pattern_key, default_pattern),
        input_base=input_base,
        synth_label=synth_label,
        voice_label=voice_label,
        number_label=number_label,
        fallback=input_base or "output",
    )


def _available_output_formats() -> List[str]:
    formats = ["wav"]
    if shutil.which("ffmpeg"):
        formats.extend(["mp3", "flac", "m4a"])
    return formats


def _configured_output_format(key: str = "defaultOutputFormat") -> str:
    fmt = str(_cfg_get(key, "wav") or "wav").strip().lower()
    if fmt not in _available_output_formats():
        return "wav"
    return fmt


def _output_ext(fmt: str) -> str:
    fmt = str(fmt or "wav").lower()
    if fmt not in ("wav", "mp3", "flac", "m4a"):
        fmt = "wav"
    return ".%s" % fmt


def _pick_output_path(parent, suggestion_base: str, synth_label: str, voice_label: str = ""):
    available_formats = _available_output_formats()
    has_ffmpeg = len(available_formats) > 1
    wildcard = "WAV audio (*.wav)|*.wav"
    if has_ffmpeg:
        wildcard += "|MP3 audio (*.mp3)|*.mp3|FLAC audio (*.flac)|*.flac|M4A audio (*.m4a)|*.m4a"

    default_dir = _build_template_output_dir(
        "singleFolderPattern",
        DEFAULT_SINGLE_FOLDER_PATTERN,
        input_base=suggestion_base,
        synth_label=synth_label,
        voice_label=voice_label,
    )
    if _cfg_get_bool("skipSingleSaveDialog", False):
        fmt = _configured_output_format()
        out_path = _unique_output_path(os.path.join(default_dir, suggestion_base + _output_ext(fmt)))
        _cfg_set("lastOutputFormat", fmt)
        return (out_path, fmt)

    default_fmt = str(_cfg_get("lastOutputFormat", _configured_output_format()) or "wav").lower()
    if default_fmt not in available_formats:
        default_fmt = "wav"
    default_file = f"{suggestion_base}{_output_ext(default_fmt)}"

    fd = wx.FileDialog(
        parent,
        message="Save audio as…",
        defaultDir=default_dir,
        defaultFile=default_file,
        wildcard=wildcard,
        style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
    )
    try:
        filter_map = {"wav": 0, "mp3": 1, "flac": 2, "m4a": 3}
        try:
            fd.SetFilterIndex(filter_map.get(default_fmt, 0))
        except Exception:
            pass
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
    if ext in (".wav", ".mp3", ".flac", ".m4a"):
        fmt = ext[1:]
    else:
        if has_ffmpeg and filt == 1:
            fmt = "mp3"
            out_path += ".mp3"
        elif has_ffmpeg and filt == 2:
            fmt = "flac"
            out_path += ".flac"
        elif has_ffmpeg and filt == 3:
            fmt = "m4a"
            out_path += ".m4a"
        else:
            fmt = "wav"
            out_path += ".wav"

    if fmt in ("mp3", "flac", "m4a") and not has_ffmpeg:
        _error("%s requires ffmpeg on PATH. Please choose WAV." % fmt.upper())
        return (None, None)

    _cfg_set("lastOutputFormat", fmt)
    return (out_path, fmt)


def _pick_batch_output_format(parent) -> Optional[str]:
    available_formats = _available_output_formats()
    if _cfg_get_bool("skipBatchFormatDialog", False):
        fmt = _configured_output_format()
        _cfg_set("lastBatchOutputFormat", fmt)
        return fmt

    choices = [fmt.upper() for fmt in available_formats]
    dlg = wx.SingleChoiceDialog(parent, "Choose output format for all files:", ADDON_NAME, choices)
    try:
        last = str(_cfg_get("lastBatchOutputFormat", "wav") or "wav").upper()
        if last in choices:
            try:
                dlg.SetSelection(choices.index(last))
            except Exception:
                pass
        if dlg.ShowModal() != wx.ID_OK:
            return None
        selection = dlg.GetStringSelection() or "WAV"
        _cfg_set("lastBatchOutputFormat", selection.lower())
    finally:
        try:
            dlg.Destroy()
        except Exception:
            pass
    return selection.lower()


def _unique_output_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    folder, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    for i in range(2, 10000):
        candidate = os.path.join(folder, "%s (%d)%s" % (stem, i, ext))
        if not os.path.exists(candidate):
            return candidate
    return path


# ----------------------------
# SAPI5 offline rendering + options
_runtime.publish(globals())
from soundWave_lib.synths.sapi5 import (
    _list_sapi5_32_voices,
    _has_sapi5_32,
    _render_with_sapi5,
    _render_with_sapi5_32,
    Sapi5OptionsDialog,
)

# DECtalk offline rendering + options
_runtime.publish(globals())
from soundWave_lib.synths.dectalk import (
    _has_dectalk,
    DectalkOptionsDialog,
    _render_with_dectalk_offline,
)

# Generic NVDA capture and BestSpeech support
_runtime.publish(globals())
from soundWave_lib.synths.nvda_capture import (
    _has_bestspeech,
    _get_bestspeech_voice_defaults,
    _render_with_nvda_generic_capture,
    _render_with_bestspeech_offline,
    BestSpeechOptionsDialog,
    GenericNvdaOptionsDialog,
)

# Sonata availability probe
_runtime.publish(globals())
from soundWave_lib.synths.sonata import _has_sonata

def _list_nvda_synths() -> List[Tuple[str, str]]:
    """Return available NVDA synth drivers as (id, displayName)."""
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

        sizer.Add(wx.StaticText(self, label="Choose a &synthesizer to render with:"), 0, wx.ALL, 10)

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

        for addon_name, label in (("Eloquence", "Eloquence"), ("IBMTTS", "IBMTTS")):
            dll_path = _find_ibmeci_dll(addon_name)
            if dll_path:
                choices.append(label)
                self._choice_meta.append({"kind": "ibmeci", "label": label, "eciDllPath": dll_path})
        if not any(meta.get("kind") == "ibmeci" for meta in self._choice_meta):
            dll_path = (os.environ.get("SOUNDWAVE_IBMECI_DLL", "") or "").strip() or _find_ibmeci_dll()
            if dll_path:
                choices.append("IBM ECI")
                self._choice_meta.append({"kind": "ibmeci", "label": "IBM ECI", "eciDllPath": dll_path})
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
            and "eloquence" not in f"{n} {d}".lower()
            and "ibmtts" not in f"{n} {d}".lower()
            and "ibm tts" not in f"{n} {d}".lower()
            and "ibm eci" not in f"{n} {d}".lower()
        ]
        if generic_other:
            for n, d in generic_other:
                choices.append(d)
                self._choice_meta.append({"kind": "nvda", "label": d, "nvdaName": n})

        self.choice = wx.Choice(self, choices=choices)
        sizer.Add(self.choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # record base dir
        baseRow = wx.BoxSizer(wx.HORIZONTAL)
        baseRow.Add(wx.StaticText(self, label="Record base &folder:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.baseDir = wx.TextCtrl(self, value=str(_cfg_get("recordBaseDir", "") or ""))
        baseRow.Add(self.baseDir, 1, wx.EXPAND | wx.RIGHT, 8)
        self.browseBtn = wx.Button(self, label="&Browse…")
        self.browseBtn.Bind(wx.EVT_BUTTON, self._on_browse)
        baseRow.Add(self.browseBtn, 0)
        sizer.Add(baseRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticText(
            self,
            label="Tip: this is the default save folder for renders. Output folders are named by your SoundWave templates.\n"
                  "Leave blank to use Documents\\SoundWave. You can also set this in NVDA Settings, soundWave."
        ), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        
        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.helpBtn = _create_help_button(self)
        btnRow.Add(self.helpBtn, 0, wx.RIGHT, 8)
        btnRow.AddStretchSpacer(1)
        btns = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        btnRow.Add(btns, 0, wx.EXPAND)
        sizer.Add(btnRow, 0, wx.EXPAND | wx.ALL, 10)

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
        current = self.baseDir.GetValue()
        if not current or not os.path.isdir(os.path.expandvars(os.path.expanduser(current))):
            current = str(_cfg_get("recordBaseDir", "") or "") or _get_default_record_base_dir()
        dlg = wx.DirDialog(
            self,
            "Choose record base folder",
            defaultPath=os.path.expandvars(os.path.expanduser(current)),
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
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




# IBM ECI options
_runtime.publish(globals())
from soundWave_lib.synths.ibmeci import IbmEciOptionsDialog

# Sonata options / discovery
_runtime.publish(globals())
from soundWave_lib.synths.sonata import (
    _render_with_sonata_offline,
    SonataOptionsDialog,
)

# Orpheus options and capture renderer
_runtime.publish(globals())
from soundWave_lib.synths.orpheus import (
    OrpheusOptionsDialog,
    _render_with_orpheus_capture,
)

# IBM ECI renderer
_runtime.publish(globals())
from soundWave_lib.synths.ibmeci import (
    _find_ibmeci_dll,
    _render_with_ibmeci_dll,
)

_runtime.publish(globals())

def _convert_with_ffmpeg(in_wav: str, out_path: str, fmt: str = "mp3"):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH.")
    if fmt == "flac":
        args = [ffmpeg, "-y", "-i", in_wav, "-codec:a", "flac", "-compression_level", "8", out_path]
    elif fmt == "m4a":
        args = [ffmpeg, "-y", "-i", in_wav, "-codec:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    else:
        args = [ffmpeg, "-y", "-i", in_wav, "-codec:a", "libmp3lame", "-q:a", "2", out_path]
    proc = subprocess.run(
        args,
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
        max_chars = max(100, int(max_chars))
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


def _chunk_size_for_render(kind: str, nvda_name: str = "", synth_label: str = "") -> int:
    joined = f"{kind} {nvda_name} {synth_label}".lower()
    if "supertonic" in joined:
        return SUPERTONIC_RENDER_CHUNK_CHARS
    return RENDER_CHUNK_CHARS


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


_runtime.publish(globals())

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
    input_items = _pick_input_items(parent)
    cleaned_items: List[Dict[str, str]] = []
    for item in input_items:
        cleaned_text = _clean_text_for_render(item.get("text", ""))
        if cleaned_text:
            cleaned = dict(item)
            cleaned["text"] = cleaned_text
            cleaned_items.append(cleaned)
    if not cleaned_items:
        return
    is_batch = len(cleaned_items) > 1
    text = cleaned_items[0]["text"]
    base = cleaned_items[0].get("base", "output") or "output"

    # 3) Per-synth options
    sonata_opts = None
    sapi_opts = None
    sapi32_opts = None
    orpheus_opts = None
    dectalk_opts = None
    nvda_opts = None
    voice_label = ""
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
            voice_label = str(sonata_opts.get("voiceLabel", "") or sonata_opts.get("voice_label", "") or "")
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
            voice_label = str(sapi_opts.get("voiceName", "") or "")
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
            voice_label = str(sapi32_opts.get("voiceName", "") or "")
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
            voice_label = str(dectalk_opts.get("voiceLabel", "") or dectalk_opts.get("voice", "") or "")
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
            voice_label = str(bs_opts.get("voiceLabel", "") or bs_opts.get("voice", "") or "")
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
            "pitch": int(_cfg_get("orpheusPitch", 50) or 50),
            "volume": int(_cfg_get("orpheusVolume", 100) or 100),
            "autoTest": _cfg_get_bool("autoTestOnChangeOrpheus", True),
        }
        od = OrpheusOptionsDialog(parent, live, initial=init)
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            orpheus_opts = od.get_options()
            voice_label = str(orpheus_opts.get("variantLabel", "") or orpheus_opts.get("voiceLabel", "") or "")
            _cfg_set("orpheusLanguageId", str(orpheus_opts.get("languageId", "") or ""))
            _cfg_set("orpheusVariantId", str(orpheus_opts.get("variantId", "") or ""))
            _cfg_set("orpheusSpeed", int(orpheus_opts.get("speed", 100) or 100))
            _cfg_set("orpheusPitch", int(orpheus_opts.get("pitch", 50) or 50))
            _cfg_set("orpheusVolume", int(orpheus_opts.get("volume", 100) or 100))
            _cfg_set("autoTestOnChangeOrpheus", bool(orpheus_opts.get("autoTest", True)))
        finally:
            try:
                od.Destroy()
            except Exception:
                pass
    
    elif kind == "ibmeci":
        synth_label = synth_meta.get("label", "") or "IBM ECI"
        detected_eci_dll = (synth_meta.get("eciDllPath", "") or "").strip()
        auto_eci_dll = detected_eci_dll or _find_ibmeci_dll(synth_label)
        init = {
            "dllPath": detected_eci_dll or (_cfg_get("ibmeciDllPath", "") or "").strip() or (os.environ.get("SOUNDWAVE_IBMECI_DLL", "") or "").strip() or auto_eci_dll,
            "voiceId": int(_cfg_get("ibmeciVoiceId", 0) or 0),
            "speed": int(_cfg_get("ibmeciSpeed", 110) or 110),
            "autoTest": _cfg_get_bool("autoTestOnChangeIbmEci", True),
        }
        od = IbmEciOptionsDialog(parent, initial=init)
        try:
            if _show_modal(od) != wx.ID_OK:
                return
            eci = od.get_options()
            voice_label = str(eci.get("voiceLabel", "") or eci.get("voiceId", "") or "")
            _cfg_set("ibmeciDllPath", eci.get("dllPath", "") or "")
            _cfg_set("ibmeciVoiceId", int(eci.get("voiceId", 0) or 0))
            _cfg_set("ibmeciSpeed", int(eci.get("speed", 110) or 110))
            _cfg_set("autoTestOnChangeIbmEci", bool(eci.get("autoTest", True)))
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
            voice_label = _first_useful_label(
                nvda_opts.get("voiceLabel", ""),
                nvda_opts.get("variantLabel", ""),
                nvda_opts.get("voice", ""),
            )
        finally:
            try:
                od.Destroy()
            except Exception:
                pass

    # 4) Output path (defaults into record base dir / synth subdir)
    render_jobs: List[Dict[str, str]] = []
    if is_batch:
        fmt = _pick_batch_output_format(parent)
        if not fmt:
            return
        default_dir = _build_template_output_dir(
            "batchFolderPattern",
            DEFAULT_BATCH_FOLDER_PATTERN,
            input_base=base or "output",
            synth_label=synth_label,
            voice_label=voice_label,
        )
        _ensure_dir(default_dir)
        width = max(2, len(str(len(cleaned_items))))
        ext = ".%s" % fmt
        for idx, item in enumerate(cleaned_items, start=1):
            number_label = str(idx).zfill(width)
            suggestion_base = _build_template_output_base(
                "batchFilePattern",
                DEFAULT_BATCH_FILE_PATTERN,
                input_base=item.get("base", "output") or "output",
                synth_label=synth_label,
                voice_label=voice_label,
                number_label=number_label,
            )
            out_path_item = _unique_output_path(os.path.join(default_dir, suggestion_base + ext))
            render_jobs.append({"text": item["text"], "base": item.get("base", "output") or "output", "outPath": out_path_item, "number": number_label})
        out_path = render_jobs[0]["outPath"]
    else:
        suggestion_base = _build_template_output_base(
            "singleFilePattern",
            DEFAULT_SINGLE_FILE_PATTERN,
            input_base=base or "output",
            synth_label=synth_label,
            voice_label=voice_label,
        )
        out_path, fmt = _pick_output_path(parent, suggestion_base=suggestion_base, synth_label=synth_label, voice_label=voice_label)
        if not out_path:
            return
        render_jobs.append({"text": text, "base": base, "outPath": out_path, "number": ""})

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
                total_chars = sum(len(job.get("text", "") or "") for job in render_jobs)
                log.info('soundWave: render worker started (kind=%s, synth=%s, jobs=%d, chars=%d)' % (kind, synth_label, len(render_jobs), total_chars))
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
                        pitch=int((bs_opts or {}).get("pitch", 50) or 50),
                        volume=int((bs_opts or {}).get("volume", 100) or 100),
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
                            "SoundWave could not find the installed Eloquence/IBMTTS speech engine.\n\n"
                            "Install or update the Eloquence or IBMTTS NVDA add-on, reload NVDA, then try again."
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
                            volume=int(_cfg_get("sapi532Volume", 100) or 100),
                            pitch=int(_cfg_get("sapi532Pitch", 0) or 0),
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
                    volume = int((sapi32_opts or {}).get("volume", 100) or 100)
                    pitch = int((sapi32_opts or {}).get("pitch", 0) or 0)
                    return _render_with_sapi5_32(chunk_text, chunk_wav, voice_name=voice_name, rate=rate, volume=volume, pitch=pitch)

                voice_name = str(_cfg_get('sapi5VoiceName', '') or '') or None
                rate = int(_cfg_get('sapi5Rate', 0) or 0)
                volume = int(_cfg_get('sapi5Volume', 100) or 100)
                pitch = int(_cfg_get('sapi5Pitch', 0) or 0)
                _render_with_sapi5(chunk_text, chunk_wav, voice_name=voice_name, rate=rate, volume=volume, pitch=pitch)
                return "SAPI5"

            render_chunk_chars = _chunk_size_for_render(kind, nvda_name, synth_label)
            prepared_jobs = []
            for job_index, job in enumerate(render_jobs, start=1):
                job_text = job.get("text", "")
                chunks = _split_text_for_render(job_text, render_chunk_chars) if len(job_text or "") > render_chunk_chars else [job_text]
                prepared_jobs.append((job_index, job, chunks))
            result.chunks = sum(max(1, len(chunks)) for _job_index, _job, chunks in prepared_jobs)
            result.progress = {
                "chunksTotal": result.chunks,
                "chunksDone": 0,
                "bytes": 0,
                "started_ts": time.time(),
                "jobsTotal": len(prepared_jobs),
                "jobsDone": 0,
            }
            all_output_paths: List[str] = []
            all_durations: List[float] = []
            total_parts = 0
            chunks_done = 0

            for job_index, job, text_chunks in prepared_jobs:
                part_paths: List[str] = []
                current_writer = None
                current_params = None
                current_part_bytes = 0
                current_part_index = 1
                job_out_path = job["outPath"]

                def _close_current_part():
                    nonlocal current_writer, current_params, current_part_bytes
                    if current_writer is not None:
                        try:
                            current_writer.close()
                        finally:
                            current_writer = None
                        part_paths.append(os.path.join(tmp_dir, "job%03d_render_part%03d.wav" % (job_index, current_part_index)))
                        current_params = None
                        current_part_bytes = 0

                for idx, chunk_text in enumerate(text_chunks, start=1):
                    if cancel_evt.is_set():
                        raise RuntimeError("Cancelled.")
                    result.progress["jobsCurrent"] = job_index
                    result.progress["chunksCurrent"] = chunks_done + idx
                    result.progress["chunksTotal"] = result.chunks
                    chunk_wav = os.path.join(tmp_dir, "job%03d_chunk%05d.wav" % (job_index, idx))
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
                        part_wav = os.path.join(tmp_dir, "job%03d_render_part%03d.wav" % (job_index, current_part_index))
                        current_writer, current_params = _open_combined_wav(part_wav, chunk_wav)
                    current_part_bytes += _append_wav_to_open_writer(chunk_wav, current_writer, current_params)
                    result.progress["chunksDone"] = chunks_done + idx
                    result.progress["bytes"] = int(result.progress.get("bytes", 0) or 0) + chunk_bytes
                    result.progress["last_audio_ts"] = time.time()
                    try:
                        os.remove(chunk_wav)
                    except Exception:
                        pass

                chunks_done += len(text_chunks)
                result.progress["jobsDone"] = job_index
                _close_current_part()
                if not part_paths:
                    raise RuntimeError("Render failed: no audio parts were produced.")
                total_parts += len(part_paths)
                if tmp_wav == os.path.join(tmp_dir, "render.wav"):
                    tmp_wav = part_paths[0]
                durations = [_wav_duration_seconds(p) for p in part_paths]
                all_durations.extend([d for d in durations if d is not None])

                if cancel_evt.is_set():
                    raise RuntimeError("Cancelled.")

                if len(part_paths) == 1:
                    if fmt == "wav":
                        _atomic_replace(part_paths[0], job_out_path)
                    else:
                        _convert_with_ffmpeg(part_paths[0], job_out_path, fmt=fmt)
                    all_output_paths.append(job_out_path)
                else:
                    base_out, ext_out = os.path.splitext(job_out_path)
                    ext = ".%s" % fmt if fmt != "wav" else ".wav"
                    for part_idx, part_wav in enumerate(part_paths, start=1):
                        part_out = "%s.part%03d%s" % (base_out, part_idx, ext)
                        if fmt == "wav":
                            _atomic_replace(part_wav, part_out)
                        else:
                            _convert_with_ffmpeg(part_wav, part_out, fmt=fmt)
                        all_output_paths.append(part_out)

            result.output_paths = all_output_paths
            result.parts = max(1, total_parts)
            result.wall_s = max(0.001, time.time() - wall0)
            result.audio_s = sum(all_durations) if all_durations else None

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
    _finish_state = {'done': False}
    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def finish():
        if _finish_state.get('done'):
            return
        _finish_state['done'] = True

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
            _show_render_complete(parent, "\n".join(msg), result.output_paths or [out_path])
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
                            total_jobs = int(p.get('jobsTotal', 0) or 0)
                            done_jobs = int(p.get('jobsDone', 0) or 0)
                            current_job = int(p.get('jobsCurrent', done_jobs) or done_jobs)
                            if total_jobs > 1:
                                details.append('File: %d of %d (%d complete)' % (current_job, total_jobs, done_jobs))
                        except Exception:
                            pass
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
                        byte_count = 0
                        try:
                            byte_count = int(p.get('bytes', 0) or 0)
                            sr = int(p.get('pcm_rate', 0) or 0)
                            ch = int(p.get('channels', 1) or 1)
                            sw = int(p.get('sampwidth', 2) or 2)
                            if byte_count > 0 and sr > 0 and ch > 0 and sw > 0:
                                rendered_s = byte_count / float(sr * ch * sw)
                        except Exception:
                            rendered_s = None

                        if rendered_s is not None:
                            details.append('Audio rendered: %.1f s' % (rendered_s,))
                            if elapsed > 0.05:
                                details.append('Conversion rate: %.2fx realtime' % (rendered_s / elapsed,))
                        elif byte_count > 0:
                            details.append('Audio written: %s' % (_format_bytes(byte_count),))
                            if elapsed > 0.05:
                                details.append('Write rate: %s/s' % (_format_bytes(int(byte_count / elapsed)),))
                        else:
                            wait_text = str(p.get('waitingText', '') or '').strip()
                            details.append(wait_text or 'Audio progress: waiting for renderer output')

                        try:
                            last = p.get('last_audio_ts', None)
                            if last:
                                age = max(0.0, now - float(last))
                                details.append('Last audio update: %.1f s ago' % (age,))
                        except Exception:
                            pass

                        prog.set_details_lines(details)

            wx.CallLater(200, poll)
        else:
            wx.CallAfter(finish)


    # Kick off UI polling
    wx.CallLater(50, poll)


class SoundWaveSettingsPanel(gui.settingsDialogs.SettingsPanel):
    title = "soundWave"

    def makeSettings(self, settingsSizer):
        helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)

        self.recordBaseDir = helper.addLabeledControl("Default save &folder:", wx.TextCtrl)
        self.recordBaseDir.SetValue(str(_cfg_get("recordBaseDir", "") or ""))
        helper.addItem(wx.StaticText(
            self,
            label=(
                "Leave blank to use Documents\\SoundWave. SoundWave creates a subfolder "
                "under this folder using your naming templates. You can also change it for a single "
                "render on the first SoundWave screen."
            ),
        ))
        self.browseRecordBaseBtn = helper.addItem(wx.Button(self, label="&Browse..."))

        self.singleFolderPattern = helper.addLabeledControl("Single render fol&der pattern:", wx.TextCtrl)
        self.singleFolderPattern.SetValue(_configured_pattern("singleFolderPattern", DEFAULT_SINGLE_FOLDER_PATTERN))
        self.singleFilePattern = helper.addLabeledControl("Single file &name pattern:", wx.TextCtrl)
        self.singleFilePattern.SetValue(_configured_pattern("singleFilePattern", DEFAULT_SINGLE_FILE_PATTERN))
        self.batchFolderPattern = helper.addLabeledControl("Batch folde&r pattern:", wx.TextCtrl)
        self.batchFolderPattern.SetValue(_configured_pattern("batchFolderPattern", DEFAULT_BATCH_FOLDER_PATTERN))
        self.batchFilePattern = helper.addLabeledControl("Batch file &pattern:", wx.TextCtrl)
        self.batchFilePattern.SetValue(_configured_pattern("batchFilePattern", DEFAULT_BATCH_FILE_PATTERN))
        helper.addItem(wx.StaticText(
            self,
            label=(
                "Available tokens: %source%, %engine%, %voice%, and %number%. "
                "The number token is mainly for batch file names."
            ),
        ))
        helper.addItem(wx.StaticText(
            self,
            label="Defaults: folders use %engine% - %voice%; single files use %source%; batch files use %number% - %source%."
        ))

        format_choices = [fmt.upper() for fmt in _available_output_formats()]
        self.defaultOutputFormat = helper.addLabeledControl("Default output forma&t:", wx.Choice, choices=format_choices)
        current_fmt = _configured_output_format().upper()
        try:
            self.defaultOutputFormat.SetSelection(format_choices.index(current_fmt))
        except Exception:
            self.defaultOutputFormat.SetSelection(0)
        helper.addItem(wx.StaticText(
            self,
            label="MP3, FLAC, and M4A are available when ffmpeg is installed and available on PATH."
        ))

        self.skipSingleSaveDialog = wx.CheckBox(self, label="Skip single-render Save &As dialog")
        self.skipSingleSaveDialog.SetValue(_cfg_get_bool("skipSingleSaveDialog", False))
        self.skipBatchFormatDialog = wx.CheckBox(self, label="Skip batch output format &choice")
        self.skipBatchFormatDialog.SetValue(_cfg_get_bool("skipBatchFormatDialog", False))
        helper.addItem(self.skipSingleSaveDialog)
        helper.addItem(self.skipBatchFormatDialog)

        self.autoOpenFolder = wx.CheckBox(self, label="&Open output folder when the whole render finishes")
        self.autoOpenFolder.SetValue(_cfg_get_bool("autoOpenFolderAfterRender", False))
        self.autoPlay = wx.CheckBox(self, label="Automatically pla&y rendered audio when rendering finishes")
        self.autoPlay.SetValue(_cfg_get_bool("autoPlayAfterRender", False))
        self.showSummary = wx.CheckBox(self, label="Show render &summary when rendering finishes")
        self.showSummary.SetValue(_cfg_get_bool("showCompletionSummary", True))
        helper.addItem(self.autoOpenFolder)
        helper.addItem(self.autoPlay)
        helper.addItem(self.showSummary)

        helpRow = wx.BoxSizer(wx.HORIZONTAL)
        helpRow.AddStretchSpacer(1)
        self.helpBtn = _create_help_button(self)
        helpRow.Add(self.helpBtn, 0)
        helper.addItem(helpRow)
        self.browseRecordBaseBtn.Bind(wx.EVT_BUTTON, self._on_browse_record_base)

    def _on_browse_record_base(self, evt):
        current = self.recordBaseDir.GetValue()
        if not current or not os.path.isdir(os.path.expandvars(os.path.expanduser(current))):
            current = str(_cfg_get("recordBaseDir", "") or "") or _get_default_record_base_dir()
        dlg = wx.DirDialog(
            self,
            "Choose default soundWave save folder",
            defaultPath=os.path.expandvars(os.path.expanduser(current)),
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        try:
            if _show_modal(dlg) == wx.ID_OK:
                self.recordBaseDir.SetValue(dlg.GetPath())
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass

    def onSave(self):
        _cfg_set("recordBaseDir", self.recordBaseDir.GetValue() or "")
        _cfg_set("singleFolderPattern", (self.singleFolderPattern.GetValue() or "").strip() or DEFAULT_SINGLE_FOLDER_PATTERN)
        _cfg_set("singleFilePattern", (self.singleFilePattern.GetValue() or "").strip() or DEFAULT_SINGLE_FILE_PATTERN)
        _cfg_set("batchFolderPattern", (self.batchFolderPattern.GetValue() or "").strip() or DEFAULT_BATCH_FOLDER_PATTERN)
        _cfg_set("batchFilePattern", (self.batchFilePattern.GetValue() or "").strip() or DEFAULT_BATCH_FILE_PATTERN)
        fmt = "wav"
        try:
            fmt = (self.defaultOutputFormat.GetStringSelection() or "WAV").lower()
        except Exception:
            pass
        _cfg_set("defaultOutputFormat", fmt if fmt in _available_output_formats() else "wav")
        _cfg_set("skipSingleSaveDialog", bool(self.skipSingleSaveDialog.GetValue()))
        _cfg_set("skipBatchFormatDialog", bool(self.skipBatchFormatDialog.GetValue()))
        _cfg_set("autoOpenFolderAfterRender", bool(self.autoOpenFolder.GetValue()))
        _cfg_set("autoPlayAfterRender", bool(self.autoPlay.GetValue()))
        _cfg_set("showCompletionSummary", bool(self.showSummary.GetValue()))


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = _("soundWave")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            cats = gui.settingsDialogs.NVDASettingsDialog.categoryClasses
            if SoundWaveSettingsPanel not in cats:
                cats.append(SoundWaveSettingsPanel)
        except Exception:
            pass

    def terminate(self):
        try:
            cats = gui.settingsDialogs.NVDASettingsDialog.categoryClasses
            if SoundWaveSettingsPanel in cats:
                cats.remove(SoundWaveSettingsPanel)
        except Exception:
            pass
        return super().terminate()

    @script(
        description=_("Open the soundWave text-to-audio render dialog."),
        gesture="kb:NVDA+control+=",
    )
    def script_renderFTR(self, gesture):
        # Never show dialogs in the script callback: schedule onto wx loop.
        wx.CallAfter(_do_render_impl)
