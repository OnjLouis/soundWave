# -*- coding: utf-8 -*-
from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile
import threading
from typing import Optional

import globalVars
import ui
import wx

from soundWave_lib import runtime as _runtime
from soundWave_lib.synths import ibmeci_host as _eci_host
from soundWave_lib.synths import ibmeci_process as _eci_process

_runtime.bind(globals())

_ECI_LABELS = {
    "Engine default": _("Engine default"),
    "American English": _("American English"), "British English": _("British English"),
    "Castilian Spanish": _("Castilian Spanish"), "Latin American Spanish": _("Latin American Spanish"),
    "French": _("French"), "French Canadian": _("French Canadian"), "German": _("German"),
    "Italian": _("Italian"), "Mandarin Chinese": _("Mandarin Chinese"),
    "Taiwanese Mandarin": _("Taiwanese Mandarin"), "Brazilian Portuguese": _("Brazilian Portuguese"),
    "Japanese": _("Japanese"), "Finnish": _("Finnish"), "Korean": _("Korean"),
    "Cantonese": _("Cantonese"), "Hong Kong Cantonese": _("Hong Kong Cantonese"),
    "Dutch": _("Dutch"), "Norwegian": _("Norwegian"), "Swedish": _("Swedish"),
    "Danish": _("Danish"), "Thai": _("Thai"),
    "Adult Male 1": _("Adult Male 1"), "Adult Female 1": _("Adult Female 1"),
    "Child 1": _("Child 1"), "Adult Male 2": _("Adult Male 2"), "Adult Male 3": _("Adult Male 3"),
    "Adult Female 2": _("Adult Female 2"), "Elderly Female 1": _("Elderly Female 1"),
    "Elderly Male 1": _("Elderly Male 1"),
}


def _eci_label(label):
    return _ECI_LABELS.get(label, label)


def _eci_live_options(dll_path):
    """Read only the running driver's Python cache, never its shared host."""
    synth = synthDriverHandler.getSynth()
    driver = sys.modules.get("synthDrivers._ibmeci")
    if synth is None or getattr(synth, "name", "") != "ibmeci" or driver is None:
        return {}
    path = os.path.join(getattr(driver, "ttsPath", ""), getattr(driver, "dllName", ""))
    if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(dll_path)):
        return {}
    parameters = dict(getattr(driver, "params", {}))
    values = dict(getattr(driver, "vparams", {}))
    result = {name: int(values[parameter]) for name, (parameter, maximum) in _eci_host.VOICE_PARAMETERS.items()
              if parameter in values and 0 <= int(values[parameter]) <= maximum}
    if _eci_host.LANGUAGE_PARAMETER in parameters:
        result["voiceId"] = int(parameters[_eci_host.LANGUAGE_PARAMETER])
    if _eci_host.SAMPLE_RATE_PARAMETER in parameters:
        result["sampleRate"] = int(parameters[_eci_host.SAMPLE_RATE_PARAMETER])
    variant = vars(synth).get("_variant", 0)
    result["variant"] = int(variant)
    return result


_ECI_CONTROLS = (
    ("speed", _("&Speed:"), 250),
    ("pitch", _("&Pitch:"), 100),
    ("inflection", _("&Inflection:"), 100),
    ("volume", _("V&olume:"), 100),
    ("headSize", _("Head si&ze:"), 100),
    ("roughness", _("&Roughness:"), 100),
    ("breathiness", _("&Breathiness:"), 100),
)


class _EciControlAccessible(getattr(wx, "Accessible", object)):
    def __init__(self, window, label):
        super().__init__(window)
        self.window = window
        self.label = label.replace("&", "").rstrip(":")
        self.shortcut = "Alt+" + label.split("&", 1)[1][0].upper()

    def GetName(self, childId):
        return (wx.ACC_OK, self.label) if childId == 0 else (wx.ACC_NOT_IMPLEMENTED, "")

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, self.shortcut) if childId == 0 else (wx.ACC_NOT_IMPLEMENTED, "")

    def GetValue(self, childId):
        if childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, "")
        value = self.window.GetStringSelection() if hasattr(self.window, "GetStringSelection") else self.window.GetValue()
        return (wx.ACC_OK, str(value))


def _label_eci_control(control, label):
    if hasattr(wx, "Accessible"):
        accessible = _EciControlAccessible(control, label)
        control.SetAccessible(accessible)
        control._soundWaveAccessible = accessible


class IbmEciOptionsDialog(wx.Dialog):
    """Query and preview ECI outside NVDA's live synth process."""

    SAMPLE_TEXT = "This is a SoundWave voice test."

    def __init__(self, parent, initial=None):
        super().__init__(parent, title=_("soundWave - IBM ECI options"))
        self.initial = dict(initial or {})
        self.dllPath = str(self.initial.get("dllPath") or _find_ibmeci_dll())
        self._closed = False
        self._loading = True
        self._busy = False
        self._pending_test = False
        self._cancel = threading.Event()
        self._languages = []
        self._variants = []
        self._rates = []

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.StaticText(panel, label=_("Loading voice settings..."))
        root.Add(self.status, 0, wx.ALL, 12)
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)
        self.voiceChoice = self._choice(panel, grid, _("&Voice:"))
        self.variantChoice = self._choice(panel, grid, _("Varia&nt:"))
        self.sampleRateChoice = self._choice(panel, grid, _("Sa&mple rate:"))
        self._spins = {}
        for name, label, maximum in _ECI_CONTROLS:
            grid.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control = wx.SpinCtrl(panel, min=0, max=maximum, initial=0)
            _label_eci_control(control, label)
            control.Enable(False)
            grid.Add(control, 1, wx.EXPAND)
            self._spins[name] = control
            control.Bind(wx.EVT_SPINCTRL, self._changed)
            _bind_numeric_page_keys(control, 0, maximum, page_step=10, callback=self._changed)
        root.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.autoSpeakCB = _add_autospeak_checkbox(panel, root, "autoTestOnChangeIbmEci", default=True)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(panel, label=_("&Test"))
        self.testBtn.Enable(False)
        buttons.Add(self.testBtn, 0, wx.RIGHT, 8)
        buttons.Add(_create_help_button(panel), 0, wx.RIGHT, 8)
        buttons.AddStretchSpacer(1)
        self.okBtn = wx.Button(panel, wx.ID_OK)
        self.okBtn.SetDefault()
        self.okBtn.Enable(False)
        buttons.Add(self.okBtn, 0, wx.RIGHT, 8)
        buttons.Add(wx.Button(panel, wx.ID_CANCEL), 0)
        root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)
        self.voiceChoice.Bind(wx.EVT_CHOICE, self._voice_changed)
        self.variantChoice.Bind(wx.EVT_CHOICE, self._variant_changed)
        self.sampleRateChoice.Bind(wx.EVT_CHOICE, self._rate_changed)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self._auto_test = _debounced_call(lambda: self._on_test(None), delay_ms=250)
        threading.Thread(target=self._load, name="soundWave-ECI-options", daemon=True).start()

    @staticmethod
    def _choice(panel, grid, label):
        grid.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        control = wx.Choice(panel)
        _label_eci_control(control, label)
        control.Enable(False)
        grid.Add(control, 1, wx.EXPAND)
        return control

    def _load(self):
        try:
            metadata = _eci_process.run_job(self.dllPath, globalVars.appDir,
                                            timeout=_eci_process.PROBE_TIMEOUT_SECONDS, cancel_evt=self._cancel)
            wx.CallAfter(self._loaded, metadata, None)
        except Exception as error:
            wx.CallAfter(self._loaded, None, str(error))

    def _loaded(self, metadata, error):
        if self._closed:
            return
        if error:
            self.status.SetLabel(_("Could not load voice settings: {error}").format(error=error))
            self.status.Wrap(520)
            self.Fit()
            ui.message(self.status.GetLabel())
            return
        self._languages = sorted(metadata["languages"], key=lambda item: item["label"].casefold())
        self.voiceChoice.SetItems([_eci_label(entry["label"]) for entry in self._languages])
        requested = int(self.initial.get("voiceId", 0))
        selected = next((i for i, entry in enumerate(self._languages) if entry["id"] == requested), 0)
        self.voiceChoice.SetSelection(selected)
        self._set_language(int(self.initial.get("variant", 0)), self.initial.get("sampleRate", 1))
        defaults = self._variants[self.variantChoice.GetSelection()]["defaults"]
        for name, _label, maximum in _ECI_CONTROLS:
            if name in self.initial and name in defaults:
                self._spins[name].SetValue(max(0, min(maximum, int(self.initial[name]))))
            self._spins[name].Enable(name in defaults)
        for control in (self.voiceChoice, self.variantChoice, self.sampleRateChoice, self.testBtn, self.okBtn):
            control.Enable(True)
        self.autoSpeakCB.SetValue(bool(self.initial.get("autoTest", True)))
        self._loading = False
        self.status.SetLabel("")
        self.Layout()
        ui.message(_("Voice settings ready."))

    def _set_language(self, variant, rate):
        language = self._languages[self.voiceChoice.GetSelection()]
        self._rates = language["sampleRates"]
        self.sampleRateChoice.SetItems([f"{_eci_host.SAMPLE_RATES[value]} Hz" for value in self._rates])
        self.sampleRateChoice.SetSelection(self._rates.index(rate) if rate in self._rates else 0)
        self._set_variants(variant)

    def _set_variants(self, variant):
        language = self._languages[self.voiceChoice.GetSelection()]
        rate = self._rates[self.sampleRateChoice.GetSelection()]
        self._variants = language["profiles"][str(rate)]
        self.variantChoice.SetItems([_eci_label(entry["label"]) for entry in self._variants])
        selected = next((i for i, entry in enumerate(self._variants) if entry["id"] == variant), 0)
        self.variantChoice.SetSelection(selected)
        self._set_preset_defaults()

    def _set_preset_defaults(self):
        defaults = self._variants[self.variantChoice.GetSelection()]["defaults"]
        for name, _label, maximum in _ECI_CONTROLS:
            self._spins[name].Enable(name in defaults and not self._loading)
            if name not in defaults:
                continue
            if not self._loading and name in ("speed", "volume"):
                continue
            self._spins[name].SetValue(max(0, min(maximum, defaults[name])))

    def _voice_changed(self, event):
        variant = self._variants[self.variantChoice.GetSelection()]["id"]
        rate = self._rates[self.sampleRateChoice.GetSelection()]
        self._set_language(variant, rate)
        self._changed(event)

    def _variant_changed(self, event):
        self._set_preset_defaults()
        self._changed(event)

    def _rate_changed(self, event):
        variant = self._variants[self.variantChoice.GetSelection()]["id"]
        self._set_variants(variant)
        self._changed(event)

    def _changed(self, event):
        if not self._closed and not self._loading and self.autoSpeakCB.GetValue():
            self._auto_test()
        if event is not None:
            event.Skip()

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._closed = True
            self._cancel.set()
        event.Skip()

    def _on_test(self, event):
        if self._closed or self._loading:
            return
        if event is None and not self.autoSpeakCB.GetValue():
            return
        if self._busy:
            self._pending_test = True
            self._cancel.set()
            return
        self._busy = True
        self._cancel = threading.Event()
        options = self.get_options()
        cancel = self._cancel
        manual = event is not None

        def preview():
            directory = tempfile.mkdtemp(prefix="soundWave_test_eci_")
            output = os.path.join(directory, "test.wav")
            error = None
            try:
                _render_with_ibmeci_dll(self.SAMPLE_TEXT, output, options["dllPath"],
                                      opts=options, cancel_evt=cancel,
                                      timeout_seconds=_eci_process.PROBE_TIMEOUT_SECONDS)
            except Exception as exception:
                error = str(exception)
            wx.CallAfter(self._preview_done, directory, output, error, cancel, manual)

        threading.Thread(target=preview, name="soundWave-ECI-preview", daemon=True).start()

    def _preview_done(self, directory, output, error, cancel, manual):
        self._busy = False
        if self._closed or cancel.is_set() or error:
            shutil.rmtree(directory)
        else:
            try:
                _play_wav(output)
            finally:
                _defer_delete_dir(directory, output)
        if self._closed:
            return
        if error and not cancel.is_set():
            message = _("Test failed:\n{error}").format(error=error)
            if manual:
                _error(message)
            else:
                ui.message(message)
        if self._pending_test:
            self._pending_test = False
            self._on_test(None)

    def get_options(self):
        if self._loading:
            raise RuntimeError(_("Voice settings are not ready."))
        language = self._languages[self.voiceChoice.GetSelection()]
        variant = self._variants[self.variantChoice.GetSelection()]
        label = _eci_label(language["label"])
        if variant["id"]:
            label += " - " + _eci_label(variant["label"])
        return {
            "dllPath": self.dllPath, "voiceId": language["id"], "voiceLabel": label,
            "variant": variant["id"], "sampleRate": self._rates[self.sampleRateChoice.GetSelection()],
            "autoTest": self.autoSpeakCB.GetValue(),
            **{name: control.GetValue() for name, control in self._spins.items() if control.IsEnabled()},
        }


def _configured_ibmeci_path(addon_root: str) -> Optional[str]:
    """Resolve IBMTTS settings without importing or starting its speech driver."""
    try:
        import config
        active = config.conf.get("ibmeci", {})
        profiles = getattr(config.conf, "profiles", ())
        general = profiles[0].get("ibmeci", {}) if profiles else {}
    except (ImportError, AttributeError, KeyError):
        return None
    if not general and not active:
        return None
    # The driver's library options use the base profile, falling back per key.
    directory = general.get("TTSPath", active.get("TTSPath", "ibmtts"))
    filename = general.get("dllName", active.get("dllName", "eci.dll"))
    if not directory or not filename:
        return ""
    if not os.path.isabs(directory):
        directory = os.path.join(addon_root, "synthDrivers", directory)
    return os.path.abspath(os.path.join(directory, filename))


def _find_ibmeci_dll(preferred_addon: str = "") -> str:
    """Find the selected add-on's engine, including configured external libraries."""
    names = ("Eloquence", "IBMTTS")
    preferred = (preferred_addon or "").strip().casefold()
    if preferred in {name.casefold() for name in names}:
        names = tuple(name for name in names if name.casefold() == preferred)
    try:
        import addonHandler
        installed = [addon for addon in addonHandler.getAvailableAddons() if not addon.isPendingInstall]
    except Exception:
        installed = []
    try:
        import globalVars
        config_root = globalVars.appArgs.configPath
    except (ImportError, AttributeError):
        config_root = os.path.join(os.path.expandvars("%APPDATA%"), "nvda")
    for name in names:
        roots = [addon.path for addon in installed if addon.name.casefold() == name.casefold()]
        if not roots:
            roots = [os.path.join(config_root, "addons", name)]
        for root in roots:
            if not os.path.isdir(root):
                continue
            if name == "IBMTTS":
                configured = _configured_ibmeci_path(root)
                if configured is not None:
                    if configured and os.path.isfile(configured):
                        return configured
                    # Do not silently substitute a different engine for an explicit setting.
                    continue
            candidates = [
                os.path.join(root, "synthDrivers", "eloquence", "ECI.DLL"),
                os.path.join(root, "synthDrivers", "ibmtts", "ECI.DLL"),
                os.path.join(root, "synthDrivers", "ibmtts", "ibmeci", "ECI.DLL"),
            ]
            for path in candidates:
                if os.path.isfile(path):
                    return path
            for path in glob.glob(os.path.join(root, "**", "ECI.DLL"), recursive=True):
                if os.path.isfile(path):
                    return path
    return ""


def _render_with_ibmeci_dll(text, out_wav, dll_path, voice_id=0, sample_rate_param=1,
                          speed=110, progress=None, cancel_evt=None, *, opts=None,
                          timeout_seconds=_eci_process.RENDER_TIMEOUT_SECONDS):
    options = dict(opts) if opts is not None else {
        "voiceId": voice_id, "sampleRate": sample_rate_param, "speed": speed,
    }
    return _eci_process.run_job(dll_path, globalVars.appDir, options=options, text=text,
                               out_wav=out_wav, timeout=timeout_seconds,
                               progress=progress, cancel_evt=cancel_evt)
