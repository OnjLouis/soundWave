# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

import wx

try:
    import comtypes
    import comtypes.client
except Exception:
    comtypes = None

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())

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


def _sapi5_xml_for_pitch(text: str, pitch: int) -> Tuple[str, int]:
    """Return text and Speak flags for SAPI pitch support."""
    try:
        pitch = max(-10, min(10, int(pitch)))
    except Exception:
        pitch = 0
    if pitch == 0:
        return text or "", 0
    escaped = html.escape(text or "", quote=False)
    return f'<pitch absmiddle="{pitch}">{escaped}</pitch>', 8


def _sapi5_stream_format_type(voice) -> int:
    try:
        return int(voice.AudioOutputStream.Format.Type)
    except Exception:
        return 22


def _render_with_sapi5(
    text: str,
    out_wav: str,
    voice_name: Optional[str] = None,
    rate: int = 0,
    volume: int = 100,
    pitch: int = 0,
):
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
        try:
            voice.Volume = max(0, min(100, int(volume)))
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
        stream.Format.Type = _sapi5_stream_format_type(voice)
        stream.Open(out_wav, 3, False)
        voice.AudioOutputStream = stream
        speak_text, flags = _sapi5_xml_for_pitch(text or "", pitch)
        voice.Speak(speak_text, flags)
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


def _render_with_sapi5_32(
    text: str,
    out_wav: str,
    voice_name: Optional[str] = None,
    rate: int = 0,
    volume: int = 100,
    pitch: int = 0,
):
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
    [int]$Rate,
    [int]$Volume,
    [int]$Pitch
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
$voice.Volume = [Math]::Max(0, [Math]::Min(100, $Volume))
$stream = New-Object -ComObject SAPI.SpFileStream
try {
    $stream.Format.Type = $voice.AudioOutputStream.Format.Type
} catch {
    $stream.Format.Type = 22
}
if (Test-Path -LiteralPath $OutPath) {
    Remove-Item -LiteralPath $OutPath -Force
}
$stream.Open($OutPath, 3, $false)
$voice.AudioOutputStream = $stream
$safePitch = [Math]::Max(-10, [Math]::Min(10, $Pitch))
if ($safePitch -ne 0) {
    $escaped = [System.Security.SecurityElement]::Escape($text)
    $text = "<pitch absmiddle=`"$safePitch`">$escaped</pitch>"
    [void]$voice.Speak($text, 8)
} else {
    [void]$voice.Speak($text, 0)
}
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
                str(max(0, min(100, int(volume or 100)))),
                str(max(-10, min(10, int(pitch or 0)))),
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
        row1.Add(wx.StaticText(self, label="&Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voiceChoice = wx.Choice(self, choices=(self.voices if self.voices else ["(no voices found)"]))
        row1.Add(self.voiceChoice, 1, wx.EXPAND)
        sizer.Add(row1, 0, wx.EXPAND | wx.ALL, 10)

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(self, label="&Rate:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.rateSpin = wx.SpinCtrl(self, min=-10, max=10, initial=0)
        row2.Add(self.rateSpin, 0)
        sizer.Add(row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(self, label="&Pitch:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.pitchSpin = wx.SpinCtrl(self, min=-10, max=10, initial=0)
        row3.Add(self.pitchSpin, 0)
        sizer.Add(row3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        row4 = wx.BoxSizer(wx.HORIZONTAL)
        row4.Add(wx.StaticText(self, label="Vol&ume:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.volumeSpin = wx.SpinCtrl(self, min=0, max=100, initial=100)
        row4.Add(self.volumeSpin, 0)
        sizer.Add(row4, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.autoTest = wx.CheckBox(self, label="&Auto-speak when changing voice, rate, pitch, or volume")
        self.autoTest.SetValue(bool(_cfg_get_bool(f"autoTestOnChange{cfg_prefix}", True)))
        sizer.Add(self.autoTest, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(self, label="&Test")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
        self.helpBtn = _create_help_button(self)
        btnRow.Add(self.helpBtn, 0, wx.RIGHT, 8)
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
        pitch = int(_cfg_get(f"{cfg_prefix}Pitch", 0) or 0)
        volume = int(_cfg_get(f"{cfg_prefix}Volume", 100) or 100)

        if self.voices:
            if voice_name and voice_name in self.voices:
                self.voiceChoice.SetSelection(self.voices.index(voice_name))
            else:
                self.voiceChoice.SetSelection(0)
        else:
            self.voiceChoice.SetSelection(0)

        try:
            self.rateSpin.SetValue(max(-10, min(10, rate)))
        except Exception:
            pass
        try:
            self.pitchSpin.SetValue(max(-10, min(10, pitch)))
        except Exception:
            pass
        try:
            self.volumeSpin.SetValue(max(0, min(100, volume)))
        except Exception:
            pass
        # Events
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)
        self.voiceChoice.Bind(wx.EVT_CHOICE, self._on_change)
        self.rateSpin.Bind(wx.EVT_SPINCTRL, self._on_change)
        self.pitchSpin.Bind(wx.EVT_SPINCTRL, self._on_change)
        self.volumeSpin.Bind(wx.EVT_SPINCTRL, self._on_change)
        _bind_numeric_page_keys(self.rateSpin, -10, 10, page_step=5, callback=self._on_change)
        _bind_numeric_page_keys(self.pitchSpin, -10, 10, page_step=5, callback=self._on_change)
        _bind_numeric_page_keys(self.volumeSpin, 0, 100, page_step=10, callback=self._on_change)

    def _on_change(self, evt):
        if self.autoTest.IsChecked():
            self._on_test(None)

    def _on_test(self, evt):
        try:
            voice = self.get_voice_name()
            rate = self.get_rate()
            pitch = self.get_pitch()
            volume = self.get_volume()
            # quick temp file
            tmp = os.path.join(tempfile.gettempdir(), "soundWave_sapi_test.wav")
            self._render_fn(self.SAMPLE_TEXT, tmp, voice_name=voice, rate=rate, volume=volume, pitch=pitch)
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
            return max(-10, min(10, int(self.rateSpin.GetValue())))
        except Exception:
            return 0

    def get_pitch(self) -> int:
        try:
            return max(-10, min(10, int(self.pitchSpin.GetValue())))
        except Exception:
            return 0

    def get_volume(self) -> int:
        try:
            return max(0, min(100, int(self.volumeSpin.GetValue())))
        except Exception:
            return 100

    def get_options(self, persist: bool = True) -> dict:
        opts = {
            "voiceName": self.get_voice_name(),
            "rate": self.get_rate(),
            "pitch": self.get_pitch(),
            "volume": self.get_volume(),
            "autoTest": bool(self.autoTest.IsChecked()),
        }
        if persist:
            _cfg_set(f"{self._cfg_prefix}VoiceName", opts["voiceName"])
            _cfg_set(f"{self._cfg_prefix}Rate", int(opts["rate"]))
            _cfg_set(f"{self._cfg_prefix}Pitch", int(opts["pitch"]))
            _cfg_set(f"{self._cfg_prefix}Volume", int(opts["volume"]))
            _cfg_set(f"autoTestOnChange{self._cfg_prefix}", bool(opts["autoTest"]))
        return opts


# ----------------------------
