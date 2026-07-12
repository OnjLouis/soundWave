# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
import threading
import time
import wave
from typing import Any, Dict, List, Optional

import synthDriverHandler
import ui
import wx

try:
    import nvwave
except Exception:
    nvwave = None

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())

from soundWave_lib import voice_utils
from soundWave_lib.synths import google_tts
from soundWave_lib.synths import pocket_tts

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


def _get_bestspeech_voice_defaults(voice: str = "fred") -> Dict[str, int]:
    """Read Keynote Gold defaults after the selected voice applies its preset."""
    defaults = {"rate": 90, "pitch": 50, "volume": 80}
    try:
        import synthDrivers.bestspeech as bs
        drv = bs.SynthDriver()
        try:
            try:
                drv.voice = str(voice or "fred")
            except Exception:
                pass
            for key in ("rate", "pitch", "volume"):
                try:
                    value = int(getattr(drv, key))
                    defaults[key] = max(0, min(100, value))
                except Exception:
                    pass
        finally:
            try:
                drv.terminate()
            except Exception:
                pass
    except Exception:
        pass
    return defaults


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

    def waitDone(self):
        return

    def stop(self):
        return

    def pause(self, switch):
        return

    def setVolume(self, *args, **kwargs):
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
    is_google_tts = google_tts.is_google_tts_synth(synth_name)
    is_pocket_tts = pocket_tts.is_pocket_tts_synth(synth_name)

    def _on_synth_done(synth=None, **kwargs):
        nonlocal saw_done_notification
        if synth is None:
            return
        if synth is not None and synth is current_synth_holder.get("synth"):
            saw_done_notification = True
            done_evt.set()

    current_synth_holder = {"synth": None}
    try:
        capture_factory = _make_capture_wave_player_factory(players)
        if not is_google_tts and not is_pocket_tts:
            nvwave.WavePlayer = capture_factory
        synth = _get_synth_instance(synth_name)
        current_synth_holder["synth"] = synth
        if synth is None or not hasattr(synth, "speak"):
            raise RuntimeError("Couldn't create NVDA synth instance for %s." % synth_name)
        if (synth_name or "").lower() == "worldvoice" and not hasattr(synth, "_voiceManager"):
            raise RuntimeError(
                "WorldVoice did not initialize its voice manager. Its workspace engines appear to be missing "
                "or failing to load; see the NVDA log for the missing WorldVoice-workspace DLLs."
            )
        if is_google_tts:
            return google_tts.render_to_wav(text, out_wav, synth, opts=opts, progress=progress, cancel_evt=cancel_evt)
        if is_pocket_tts:
            return pocket_tts.render_to_wav(text, out_wav, synth, opts=opts, progress=progress, cancel_evt=cancel_evt)
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
                if "pitch" in opts and hasattr(synth, "pitch"):
                    synth.pitch = int(opts.get("pitch", 50) or 50)
            except Exception:
                pass
            try:
                if "volume" in opts and hasattr(synth, "volume"):
                    synth.volume = max(0, min(100, int(opts.get("volume", 100) or 100)))
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
            if total_bytes == 0 and done_evt.is_set():
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
    pitch: int = 50,
    volume: int = 100,
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
            drv.pitch = max(0, min(100, int(pitch)))
        except Exception:
            pass
        try:
            drv.volume = max(0, min(100, int(volume)))
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
        v_pitch = getattr(drv, "_pitch", preset.get("pitch", 130))

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


class GenericNvdaOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent, synth_id: str, synth_label: str):
        super().__init__(parent, title=f"soundWave - {synth_label} options", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.synth_id = synth_id
        self.synth_label = synth_label or synth_id or "NVDA synth"
        self.is_google_tts = google_tts.is_google_tts_synth("%s %s" % (self.synth_id, self.synth_label))
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

        grid.Add(wx.StaticText(panel, label="&Voice:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.voiceChoice = wx.Choice(panel)
        grid.Add(self.voiceChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Varia&nt:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.variantChoice = wx.Choice(panel)
        grid.Add(self.variantChoice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="&Rate:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.rateSpin = wx.SpinCtrl(panel, min=0, max=100, initial=int(_cfg_get(self.cfg_prefix + "_rate", _safe_getattr(self.synth, "rate", 50) or 50)))
        grid.Add(self.rateSpin, 0, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="&Pitch:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pitchSpin = wx.SpinCtrl(panel, min=0, max=100, initial=int(_cfg_get(self.cfg_prefix + "_pitch", _safe_getattr(self.synth, "pitch", 50) or 50)))
        grid.Add(self.pitchSpin, 0, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Vol&ume:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.volumeSpin = wx.SpinCtrl(panel, min=0, max=100, initial=int(_cfg_get(self.cfg_prefix + "_volume", _safe_getattr(self.synth, "volume", 100) or 100)))
        grid.Add(self.volumeSpin, 0, wx.EXPAND)

        self.eosSpin = None
        if hasattr(self.synth, "eosThreshold"):
            grid.Add(wx.StaticText(panel, label="&EOS sensitivity:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.eosSpin = wx.SpinCtrl(
                panel,
                min=0,
                max=100,
                initial=int(_cfg_get(self.cfg_prefix + "_eosThreshold", _safe_getattr(self.synth, "eosThreshold", 50) or 50)),
            )
            grid.Add(self.eosSpin, 0, wx.EXPAND)

        root.Add(grid, 1, wx.ALL | wx.EXPAND, 10)
        default_auto_test = google_tts.DEFAULT_AUTO_TEST if self.is_google_tts else True
        self.autoSpeakCB = _add_autospeak_checkbox(panel, root, self.cfg_prefix + "_autoTest", default=default_auto_test)

        buttons = wx.StdDialogButtonSizer()
        self.testBtn = wx.Button(panel, label="&Test")
        self.helpBtn = _create_help_button(panel)
        ok = wx.Button(panel, wx.ID_OK)
        cancel = wx.Button(panel, wx.ID_CANCEL)
        buttons.AddButton(self.testBtn)
        buttons.AddButton(self.helpBtn)
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
        self.pitchSpin.Bind(wx.EVT_SPINCTRL, self._maybe_auto_test)
        self.volumeSpin.Bind(wx.EVT_SPINCTRL, self._maybe_auto_test)
        if self.eosSpin is not None:
            self.eosSpin.Bind(wx.EVT_SPINCTRL, self._maybe_auto_test)
        self.testBtn.Bind(wx.EVT_BUTTON, self._on_test)
        _bind_numeric_page_keys(self.rateSpin, 0, 100, page_step=10, callback=self._maybe_auto_test)
        _bind_numeric_page_keys(self.pitchSpin, 0, 100, page_step=10, callback=self._maybe_auto_test)
        _bind_numeric_page_keys(self.volumeSpin, 0, 100, page_step=10, callback=self._maybe_auto_test)
        if self.eosSpin is not None:
            _bind_numeric_page_keys(self.eosSpin, 0, 100, page_step=10, callback=self._maybe_auto_test)

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
            self.voices = voice_utils.normalise_voice_infos(_safe_getattr(self.synth, "availableVoices", None))
            if self.is_google_tts:
                installed = google_tts.installed_voice_ids(self.synth)
                self.voices = [
                    vi for vi in self.voices
                    if (voice_utils.voice_info_text(vi, "id", "ID", "identifier", "name") or "") in installed
                ]
            saved = str(_cfg_get(self.cfg_prefix + "_voice", _safe_getattr(self.synth, "voice", "") or "") or "")
            selected = 0
            if not self.voices:
                label = "No installed Google TTS voices found" if self.is_google_tts else "Default"
                self._append_choice(self.voiceChoice, label, "")
            for i, vi in enumerate(self.voices):
                vid = voice_utils.voice_info_text(vi, "id", "ID", "identifier", "name") or str(i)
                label = voice_utils.voice_choice_label(vi, fallback=f"Voice {i + 1}")
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
            self.variants = voice_utils.normalise_voice_infos(_safe_getattr(self.synth, "availableVariants", None))
            saved = str(_cfg_get(self.cfg_prefix + "_variant", _safe_getattr(self.synth, "variant", "") or "") or "")
            selected = 0
            if not self.variants:
                self._append_choice(self.variantChoice, "Default", "")
            for i, vi in enumerate(self.variants):
                vid = voice_utils.voice_info_text(vi, "id", "ID", "identifier", "name") or str(i)
                label = voice_utils.voice_choice_label(vi, fallback=f"Variant {i + 1}")
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
            if self.is_google_tts and not google_tts.ALLOW_AUTO_TEST:
                return
        except Exception:
            return
        try:
            if self.autoSpeakCB.GetValue():
                self._on_test(None, quiet=True)
        except Exception:
            pass

    def _on_test(self, evt=None, quiet: bool = False):
        tmp_dir = tempfile.mkdtemp(prefix="soundWave_generic_test_")
        tmp_wav = os.path.join(tmp_dir, "test.wav")
        try:
            opts = self.get_options(persist=False)
            if self.is_google_tts:
                if not google_tts.ALLOW_MANUAL_TEST:
                    ui.message(_("Test is not available for this synthesizer."))
                    return
                google_tts.render_to_wav(
                    self.SAMPLE_TEXT,
                    tmp_wav,
                    self.synth,
                    opts=opts,
                    progress={},
                    cancel_evt=threading.Event(),
                )
            else:
                _render_with_nvda_generic_capture(
                    self.SAMPLE_TEXT,
                    tmp_wav,
                    self.synth_id,
                    cancel_evt=threading.Event(),
                    progress={},
                    opts=opts,
                )
            _play_wav(tmp_wav)
            _defer_delete_dir(tmp_dir, tmp_wav)
        except Exception as e:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            if not quiet:
                _error(str(e))

    def get_options(self, persist: bool = True) -> Dict[str, Any]:
        opts = {
            "voice": self._choice_value(self.voiceChoice),
            "voiceLabel": _choice_label(self.voiceChoice),
            "variant": self._choice_value(self.variantChoice),
            "variantLabel": _choice_label(self.variantChoice),
            "rate": int(self.rateSpin.GetValue()),
            "pitch": int(self.pitchSpin.GetValue()),
            "volume": max(0, min(100, int(self.volumeSpin.GetValue()))),
            "autoTest": bool(self.autoSpeakCB.GetValue()),
        }
        if self.eosSpin is not None:
            opts["eosThreshold"] = max(0, min(100, int(self.eosSpin.GetValue())))
        if persist:
            _cfg_set(self.cfg_prefix + "_voice", opts["voice"])
            _cfg_set(self.cfg_prefix + "_variant", opts["variant"])
            _cfg_set(self.cfg_prefix + "_rate", int(opts["rate"]))
            _cfg_set(self.cfg_prefix + "_pitch", int(opts["pitch"]))
            _cfg_set(self.cfg_prefix + "_volume", int(opts["volume"]))
            if "eosThreshold" in opts:
                _cfg_set(self.cfg_prefix + "_eosThreshold", int(opts["eosThreshold"]))
            _cfg_set(self.cfg_prefix + "_autoTest", bool(opts["autoTest"]))
        return opts

class BestSpeechOptionsDialog(wx.Dialog):
    SAMPLE_TEXT = "This is a soundWave test."

    def __init__(self, parent, initial: Optional[dict] = None):
        super().__init__(parent, title="soundWave - Keynote Gold options")
        initial = initial or {}

        self.voices = _list_bestspeech_voices()
        saved_voice = str(initial.get("voice", _cfg_get("bestspeechVoice", "") or "") or "")
        if not saved_voice and self.voices:
            saved_voice = self.voices[0]
        voice_defaults = _get_bestspeech_voice_defaults(saved_voice or "fred")
        tone_settings_current = bool(_cfg_get_bool("bestspeechToneSettingsCurrent", False))
        initial_rate = int(initial.get("rate", _cfg_get("bestspeechRate", voice_defaults.get("rate", 90)) or voice_defaults.get("rate", 90)) or voice_defaults.get("rate", 90))
        if tone_settings_current:
            initial_pitch = int(initial.get("pitch", _cfg_get("bestspeechPitch", voice_defaults.get("pitch", 50)) or voice_defaults.get("pitch", 50)) or voice_defaults.get("pitch", 50))
            initial_volume = int(initial.get("volume", _cfg_get("bestspeechVolume", voice_defaults.get("volume", 80)) or voice_defaults.get("volume", 80)) or voice_defaults.get("volume", 80))
        else:
            initial_pitch = int(initial.get("pitch", voice_defaults.get("pitch", 50)) or voice_defaults.get("pitch", 50))
            initial_volume = int(initial.get("volume", voice_defaults.get("volume", 80)) or voice_defaults.get("volume", 80))

        sizer = wx.BoxSizer(wx.VERTICAL)

        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(self, label="&Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voiceChoice = wx.Choice(self, choices=self.voices if self.voices else ["(no voices found)"])
        row1.Add(self.voiceChoice, 1, wx.EXPAND)
        sizer.Add(row1, 0, wx.EXPAND | wx.ALL, 10)

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(self, label="&Speed:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.rateSpin = wx.SpinCtrl(self, min=0, max=100, initial=max(0, min(100, initial_rate)))
        row2.Add(self.rateSpin, 0)
        sizer.Add(row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(self, label="&Pitch:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.pitchSpin = wx.SpinCtrl(self, min=0, max=100, initial=max(0, min(100, initial_pitch)))
        row3.Add(self.pitchSpin, 0)
        sizer.Add(row3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        row4 = wx.BoxSizer(wx.HORIZONTAL)
        row4.Add(wx.StaticText(self, label="Vol&ume:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.volumeSpin = wx.SpinCtrl(self, min=0, max=100, initial=max(0, min(100, initial_volume)))
        row4.Add(self.volumeSpin, 0)
        sizer.Add(row4, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.rateBoostChk = wx.CheckBox(self, label="Rate &boost (faster)")
        self.rateBoostChk.SetValue(bool(initial.get("rateBoost", _cfg_get_bool("bestspeechRateBoost", False))))
        sizer.Add(self.rateBoostChk, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.autoSpeakCB = _add_autospeak_checkbox(self, sizer, "bestspeechAutoSpeak", default=True)

        btnRow = wx.BoxSizer(wx.HORIZONTAL)
        self.testBtn = wx.Button(self, label="&Test")
        btnRow.Add(self.testBtn, 0, wx.RIGHT, 8)
        self.helpBtn = _create_help_button(self)
        btnRow.Add(self.helpBtn, 0, wx.RIGHT, 8)
        btnRow.AddStretchSpacer(1)
        okCancel = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        btnRow.Add(okCancel, 0, wx.EXPAND)
        sizer.Add(btnRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizerAndFit(sizer)
        self.SetMinSize((520, -1))

        # Load persisted
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
        self._auto_test = _debounced_call(lambda: self._on_test(None), delay_ms=350)

        def _maybe_auto(evt):
            try:
                if self.autoSpeakCB.GetValue():
                    self._auto_test()
            except Exception:
                pass
            try:
                if evt is not None:
                    evt.Skip()
            except Exception:
                pass

        self.voiceChoice.Bind(wx.EVT_CHOICE, _maybe_auto)
        self.rateSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        self.pitchSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        self.volumeSpin.Bind(wx.EVT_SPINCTRL, _maybe_auto)
        self.rateBoostChk.Bind(wx.EVT_CHECKBOX, _maybe_auto)
        _bind_numeric_page_keys(self.rateSpin, 0, 100, page_step=10, callback=_maybe_auto)
        _bind_numeric_page_keys(self.pitchSpin, 0, 100, page_step=10, callback=_maybe_auto)
        _bind_numeric_page_keys(self.volumeSpin, 0, 100, page_step=10, callback=_maybe_auto)

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
                pitch=int(self.pitchSpin.GetValue()),
                volume=int(self.volumeSpin.GetValue()),
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
            "voiceLabel": self._get_voice(),
            "rate": int(self.rateSpin.GetValue()),
            "pitch": max(0, min(100, int(self.pitchSpin.GetValue()))),
            "volume": max(0, min(100, int(self.volumeSpin.GetValue()))),
            "rateBoost": bool(self.rateBoostChk.GetValue()),
            "autoSpeak": bool(self.autoSpeakCB.GetValue()),
        }
        if persist:
            _cfg_set("bestspeechVoice", str(opts["voice"] or ""))
            _cfg_set("bestspeechRate", int(opts["rate"]))
            _cfg_set("bestspeechPitch", int(opts["pitch"]))
            _cfg_set("bestspeechVolume", int(opts["volume"]))
            _cfg_set("bestspeechToneSettingsCurrent", True)
            _cfg_set("bestspeechRateBoost", bool(opts["rateBoost"]))
            _cfg_set("bestspeechAutoSpeak", bool(opts["autoSpeak"]))
        return opts

# ----------------------------
# Synth discovery + selection
# ----------------------------
