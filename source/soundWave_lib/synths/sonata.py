# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import types
import wave
from typing import Dict, List, Tuple

import wx
from logHandler import log

from soundWave_lib import runtime as _runtime
_runtime.bind(globals())

def _has_sonata() -> bool:
    try:
        import synthDrivers.sonata_neural_voices  # noqa: F401
        return True
    except Exception:
        return False


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
                    log.debug(f"soundWave: sonata first chunk bytes={len(b)} head={head}")
                    first = False

            log.debug(f"soundWave: sonata mode={mode} chunks={chunks} totalBytes={total}")
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
        row1.Add(wx.StaticText(self, label="&Voice:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.voiceChoice = wx.Choice(self, choices=[v[0] for v in self.voices] if self.voices else ["(no voices found)"])
        row1.Add(self.voiceChoice, 1, wx.EXPAND)
        sizer.Add(row1, 0, wx.EXPAND | wx.ALL, 10)

        # Speaker
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(self, label="S&peaker:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.speakerChoice = wx.Choice(self, choices=["0"])
        row2.Add(self.speakerChoice, 1, wx.EXPAND)
        sizer.Add(row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Speed
        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(self, label="&Speed (%):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.speedSpin = wx.SpinCtrl(self, min=50, max=400, initial=140)
        row3.Add(self.speedSpin, 0)
        sizer.Add(row3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Auto-test
        self.autoTest = wx.CheckBox(self, label="&Auto-speak when changing voice, speaker, or speed")
        self.autoTest.SetValue(bool(_cfg_get_bool("autoTestOnChangeSonata", True)))
        sizer.Add(self.autoTest, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Buttons
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
        _bind_numeric_page_keys(self.speedSpin, 50, 400, page_step=10, callback=self._on_change)

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
        try:
            if evt is not None:
                evt.Skip()
        except Exception:
            pass

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
                "voiceLabel": "",
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
            "voiceLabel": label,
            "voice_config_path": cfg_path,
            "speaker": str(speaker),
            "speed_percent": speed,
        }
