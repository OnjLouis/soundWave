"""Dependent synth selectors without a live NVDA configuration or audio device."""
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1] / "source/soundWave_lib"


class Choice:
    def __init__(self):
        self.items = []
        self.selection = -1

    def Clear(self):
        self.items.clear()
        self.selection = -1

    def Append(self, label, value):
        self.items.append((label, value))

    def SetSelection(self, index):
        self.selection = index

    def GetSelection(self):
        return self.selection

    def GetCount(self):
        return len(self.items)

    def GetClientData(self, index):
        return self.items[index][1]

    def Show(self, show=True):
        pass


def info(value):
    return types.SimpleNamespace(id=value, displayName=value)


class Synth:
    supportedSettings = [info(name) for name in ("model", "voiceLanguage", "voice")]
    availableModels = {name: info(name) for name in ("phonon", "lfm3-audio")}

    def __init__(self):
        self.calls = []
        self.model = "phonon"

    def __setattr__(self, key, value):
        if key != "calls":
            self.calls.append((key, value))
        object.__setattr__(self, key, value)
        if key == "model":
            self.voiceLanguage = "all"
            self.voice = "Zoey" if value == "phonon" else "female-french"
        elif key == "voiceLanguage":
            self.voice = "Zoey" if self.model == "phonon" else "female-french"

    @property
    def availableVoiceLanguages(self):
        return {name: info(name) for name in ("all", "en_US" if self.model == "phonon" else "fr")}

    @property
    def availableVoices(self):
        return {self.voice: info(self.voice)}


class ModelOptionsTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.ns = dict(Dict=dict, Any=object, Optional=types.SimpleNamespace(__getitem__=lambda *_: object),
            wx=types.SimpleNamespace(Dialog=object, NOT_FOUND=-1), List=list, _=lambda text: text,
            _safe_getattr=lambda obj, name, default=None: getattr(obj, name, default),
            _cfg_get=lambda key, default=None: self.saved.get(key, default),
            _cfg_set=lambda key, value: self.saved.__setitem__(key, value))
        voice_ns = {}
        exec(compile((ROOT / "voice_utils.py").read_text(encoding="utf-8"), "voice_utils.py", "exec"), voice_ns)
        self.ns["voice_utils"] = types.SimpleNamespace(**voice_ns)
        names = {"_supported_setting_ids", "_supports_setting", "_discover_language_setting",
            "_discover_model_setting", "_snapshot_synth_settings", "_restore_synth_settings",
            "_apply_selector_options", "GenericNvdaOptionsDialog"}
        tree = ast.parse((ROOT / "synths/nvda_capture.py").read_text(encoding="utf-8-sig"))
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes,
            type_ignores=[])), "nvda_capture.py", "exec"), self.ns)

    def dialog(self):
        dialog = object.__new__(self.ns["GenericNvdaOptionsDialog"])
        dialog.synth = Synth()
        dialog.cfg_prefix = "fixture"
        dialog.models = []
        dialog.languages = []
        dialog.modelChoice, dialog.languageChoice, dialog.voiceChoice, dialog.variantChoice = (Choice() for _ in range(4))
        dialog.modelLabel, dialog.languageLabel = Choice(), Choice()
        dialog.modelSettingId = dialog.languageSettingId = ""
        dialog.is_google_tts = False
        dialog.Layout = Mock()
        dialog._maybe_auto_test = Mock()
        return dialog

    def test_custom_language_setting_preserves_camel_case(self):
        setting, values = self.ns["_discover_language_setting"](Synth())
        self.assertEqual(setting, "voiceLanguage")
        self.assertEqual([item.id for item in values], ["all", "en_US"])

    def test_model_discovery_requires_an_advertised_selector(self):
        self.assertEqual(self.ns["_discover_model_setting"](Synth())[0], "model")
        synth = Synth()
        synth.supportedSettings = [info("voice")]
        self.assertEqual(self.ns["_discover_model_setting"](synth), ("", []))

    def test_locale_selector_is_recognised_without_replacing_voice_language_metadata(self):
        synth = types.SimpleNamespace(supportedSettings=[info("locale")],
            availableLocales={"en_GB": info("en_GB")}, language="en_GB")
        self.assertEqual(self.ns["_discover_language_setting"](synth)[0], "locale")

    def test_removed_model_is_reported_instead_of_rendering_a_different_model(self):
        synth = Synth()
        with self.assertRaisesRegex(RuntimeError, "no longer available"):
            self.ns["_apply_selector_options"](synth, {"modelSettingId": "model", "model": "removed-model"})
        self.assertEqual(synth.model, "phonon")

    def test_old_options_without_model_remain_compatible(self):
        synth = Synth()
        self.ns["_apply_selector_options"](synth, {"voice": "Zoey"})
        self.assertEqual((synth.model, synth.voice), ("phonon", "Zoey"))

    def test_model_change_refreshes_language_and_voice_without_reusing_old_saved_ids(self):
        dialog = self.dialog()
        self.saved.update(fixture_voice="Zoey", fixture_language="en_US")
        dialog._populate_models()
        dialog._populate_languages()
        dialog._populate_voices()
        dialog._populate_variants()
        dialog.modelChoice.SetSelection(1)
        dialog._on_model_changed()
        self.assertEqual(dialog.synth.model, "lfm3-audio")
        self.assertEqual([value for _, value in dialog.languageChoice.items], ["all", "fr"])
        self.assertEqual(dialog._choice_value(dialog.voiceChoice), "female-french")
        dialog._maybe_auto_test.assert_called_once()

    def test_render_applies_model_then_language_then_voice(self):
        synth = Synth()
        synth.calls.clear()
        self.ns["_apply_selector_options"](synth, {
            "modelSettingId": "model", "model": "lfm3-audio",
            "languageSettingId": "voiceLanguage", "language": "fr", "voice": "female-french"})
        explicit = [(key, value) for key, value in synth.calls if
            (key, value) in [("model", "lfm3-audio"), ("voiceLanguage", "fr"), ("voice", "female-french")]]
        self.assertLess(explicit.index(("model", "lfm3-audio")), explicit.index(("voiceLanguage", "fr")))
        self.assertEqual((synth.model, synth.voiceLanguage, synth.voice), ("lfm3-audio", "fr", "female-french"))

    def test_snapshot_restores_model_and_language_before_voice(self):
        synth = Synth()
        synth.voiceLanguage = "en_US"
        snapshot = self.ns["_snapshot_synth_settings"](synth)
        synth.model = "lfm3-audio"
        self.ns["_restore_synth_settings"](synth, snapshot)
        self.assertEqual((synth.model, synth.voiceLanguage, synth.voice), ("phonon", "en_US", "Zoey"))


if __name__ == "__main__":
    unittest.main()
