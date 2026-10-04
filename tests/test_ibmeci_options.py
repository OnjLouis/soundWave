import ast
import ctypes
import importlib.util
from pathlib import Path
import types
import unittest
from unittest import mock


HOST = Path(__file__).resolve().parents[1] / "source/soundWave_lib/synths/ibmeci_host.py"


class FakeEngine:
    def __init__(self):
        self.params = {0: 0, 1: 0, 5: 1}
        self.voice = {i: 50 for i in range(8)}
        self.calls = []

    def eciCopyVoice(self, handle, preset, target):
        self.calls.append(("variant", preset))
        self.voice = {i: 75 for i in range(8)}
        return 1

    def eciSetParam(self, handle, key, value):
        old = self.params.get(key, 0)
        self.params[key] = value
        return old

    def eciGetParam(self, handle, key):
        return self.params[key]

    def eciSetVoiceParam(self, handle, target, key, value):
        self.calls.append(("parameter", key, value))
        old = self.voice[key]
        self.voice[key] = value
        return old

    def eciGetVoiceParam(self, handle, target, key):
        return self.voice[key]


class EciOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("eci_host_test", HOST)
        cls.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.host)

    def test_variant_is_copied_before_explicit_parameters(self):
        engine = FakeEngine()
        options = {"variant": 2, "sampleRate": 0, "speed": 110, "pitch": 0, "volume": 0}
        rate = self.host.configure_engine(engine, 1, options)
        self.assertEqual(8000, rate)
        self.assertEqual(("variant", 2), engine.calls[0])
        self.assertEqual(0, engine.voice[2])
        self.assertEqual(0, engine.voice[7])
        self.assertEqual(110, engine.voice[6])

    def test_all_sample_rates_use_the_engine_readback(self):
        for index, expected in enumerate((8000, 11025, 22050)):
            self.assertEqual(expected, self.host.configure_engine(FakeEngine(), 1, {"sampleRate": index}))

    def test_rejected_sample_rate_is_not_mislabeled(self):
        engine = FakeEngine()
        original = engine.eciSetParam
        engine.eciSetParam = lambda handle, key, value: original(handle, key, 1 if key == 5 else value)
        with self.assertRaisesRegex(RuntimeError, "sample rate"):
            self.host.configure_engine(engine, 1, {"sampleRate": 0})

    def test_preset_defaults_are_not_overwritten_without_an_override(self):
        engine = FakeEngine()
        self.host.configure_engine(engine, 1, {"variant": 3})
        self.assertEqual(75, engine.voice[2])

    def test_unknown_values_are_rejected(self):
        for options in ({"sampleRate": 99}, {"variant": 99}, {"volume": -1}, {"speed": 251}):
            with self.assertRaises(ValueError):
                self.host.configure_engine(FakeEngine(), 1, options)

    def test_every_supported_voice_parameter_is_applied(self):
        engine = FakeEngine()
        options = {key: 25 for key in self.host.VOICE_PARAMETERS}
        self.host.configure_engine(engine, 1, options)
        for name, (parameter, maximum) in self.host.VOICE_PARAMETERS.items():
            self.assertEqual(25, engine.voice[parameter], name)

    def test_numbered_syn_files_are_not_needed_for_language_names(self):
        self.assertEqual("British English", self.host.language_label(65537))
        self.assertEqual("American English", self.host.language_label(65536))

    def test_east_asian_encoding_uses_selected_language(self):
        self.assertEqual("cp932", self.host.language_encoding(524288))
        self.assertEqual("cp936", self.host.language_encoding(393216))
        self.assertEqual("cp1252", self.host.language_encoding(65537))
        self.assertEqual("utf-16-le", self.host.language_encoding(524288 | 0x0800))

    def test_probe_uses_fresh_defaults_for_each_sample_rate(self):
        engine = FakeEngine()
        engine.eciGetAvailableLanguages = lambda array, count: (
            setattr(ctypes.cast(count, ctypes.POINTER(ctypes.c_int)).contents, "value", 1),
            array.__setitem__(0, 65537) if array is not None else None,
        )
        def new_handle(language):
            engine.voice = {i: 50 for i in range(8)}
            return 1
        engine.eciNewEx = new_handle
        engine.eciDelete = lambda handle: None
        with mock.patch.object(self.host, "product_name", return_value="IBMECI"):
            metadata = self.host.probe_engine(engine, "fixture")
        profiles = metadata["languages"][0]["profiles"]
        self.assertEqual({"0", "1", "2"}, set(profiles))
        for profile in profiles.values():
            self.assertEqual(50, profile[0]["defaults"]["pitch"])


class Control:
    def __init__(self, value=0):
        self.value = value
        self.items = []
        self.enabled = True

    def GetSelection(self):
        return self.value

    def SetSelection(self, value):
        self.value = value

    def SetItems(self, items):
        self.items = items

    GetValue = GetSelection
    SetValue = SetSelection

    def Enable(self, value):
        self.enabled = value

    def IsEnabled(self):
        return self.enabled


class Accessible:
    def __init__(self, window):
        self.window = window


class DialogContractTests(unittest.TestCase):
    def setUp(self):
        self.source = HOST.with_name("ibmeci.py")
        tree = ast.parse(self.source.read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        controls = next(node for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "_ECI_CONTROLS" for target in node.targets))
        spec = importlib.util.spec_from_file_location("eci_contract_host", HOST)
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        namespace = {"wx": types.SimpleNamespace(Dialog=object, Accessible=Accessible, ACC_OK=0, ACC_NOT_IMPLEMENTED=1),
                     "_": lambda value: value,
                     "_eci_label": lambda value: value, "_eci_host": host,
                     "log": mock.Mock(), "_error": mock.Mock(), "ui": mock.Mock(),
                     "_eci_process": mock.Mock(), "globalVars": types.SimpleNamespace(appDir="nvda")}
        namespace["wx"].CallAfter = mock.Mock()
        self.namespace = namespace
        exec(compile(ast.Module(body=[controls] + nodes, type_ignores=[]), str(self.source), "exec"), namespace)
        dialog_class = namespace["IbmEciOptionsDialog"]
        self.dialog = dialog_class.__new__(dialog_class)
        self.accessible_class = namespace["_EciControlAccessible"]
        self.controls = namespace["_ECI_CONTROLS"]
        self.dialog._loading = True
        self.dialog._closed = False
        self.dialog._cancel = mock.Mock()
        self.dialog._cancel.is_set.return_value = False
        self.dialog.dllPath = "fixture"
        self.dialog._spins = {name: Control() for name, label, maximum in self.controls}
        self.dialog.voiceChoice = Control()
        self.dialog.variantChoice = Control()
        self.dialog.sampleRateChoice = Control()
        self.dialog.autoSpeakCB = Control(True)
        defaults = {name: 50 for name, label, maximum in self.controls}
        profile = [{"id": 0, "label": "Engine default", "defaults": defaults},
                   {"id": 2, "label": "Shelley", "defaults": {**defaults, "pitch": 81}}]
        self.dialog._languages = [{"id": 65537, "label": "British English", "sampleRates": [0, 1],
                                   "profiles": {"0": profile[:1], "1": profile}}]

    def test_probe_failure_is_logged_with_traceback_and_forwarded_to_dialog(self):
        self.namespace["_eci_process"].run_job.side_effect = RuntimeError("probe failed")
        self.dialog._load()
        self.namespace["log"].error.assert_called_once_with(
            "SoundWave IBM ECI voice settings probe failed for %s", "fixture", exc_info=True)
        self.namespace["wx"].CallAfter.assert_called_once_with(self.dialog._loaded, None, "probe failed")

    def test_cancelled_probe_is_not_reported_as_failure(self):
        self.dialog._cancel.is_set.return_value = True
        self.namespace["_eci_process"].run_job.side_effect = RuntimeError("cancelled")
        self.dialog._load()
        self.namespace["log"].error.assert_not_called()
        self.namespace["wx"].CallAfter.assert_not_called()

    def test_probe_failure_opens_accessible_error_without_enabling_render(self):
        self.dialog.status = mock.Mock()
        self.dialog.Fit = mock.Mock()
        self.dialog._loaded(None, "probe failed")
        message = "Could not load voice settings: probe failed"
        self.dialog.status.SetLabel.assert_called_once_with(message)
        self.namespace["_error"].assert_called_once_with(message)
        self.assertTrue(self.dialog._loading)
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            self.dialog.get_options()

    def test_closed_dialog_does_not_show_delayed_probe_error(self):
        self.dialog._closed = True
        self.dialog._loaded(None, "probe failed")
        self.namespace["_error"].assert_not_called()

    def test_current_settings_are_read_without_querying_the_live_host(self):
        tree = ast.parse(self.source.read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_eci_live_options")
        import os
        import sys
        driver = types.SimpleNamespace(ttsPath="fixture", dllName="ECI.DLL", params={9: 65537, 5: 0},
                                       vparams={2: 0, 6: 123, 7: 37})
        synth = types.SimpleNamespace(name="ibmeci", _variant="2")
        namespace = {"os": os, "sys": sys,
                     "synthDriverHandler": types.SimpleNamespace(getSynth=lambda: synth),
                     "_eci_host": types.SimpleNamespace(LANGUAGE_PARAMETER=9, SAMPLE_RATE_PARAMETER=5,
                         VOICE_PARAMETERS={"pitch": (2, 100), "speed": (6, 250), "volume": (7, 100)})}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(self.source), "exec"), namespace)
        with mock.patch.dict(sys.modules, {"synthDrivers._ibmeci": driver}):
            read = namespace["_eci_live_options"]
            self.assertEqual({"pitch": 0, "speed": 123, "volume": 37, "voiceId": 65537,
                              "sampleRate": 0, "variant": 2}, read(os.path.join("fixture", "ECI.DLL")))
            self.assertEqual({}, read(os.path.join("other", "ECI.DLL")))

    def test_sample_rate_rebuilds_only_available_presets(self):
        self.dialog._loading = False
        self.dialog._set_language(2, 1)
        self.assertEqual(81, self.dialog._spins["pitch"].GetValue())
        self.dialog._set_language(2, 0)
        self.dialog._loading = False
        self.assertEqual(0, self.dialog.get_options()["variant"])
        self.assertEqual(50, self.dialog.get_options()["pitch"])

    def test_get_options_preserves_zero_values_and_is_a_snapshot(self):
        self.dialog._loading = False
        self.dialog._set_language(2, 1)
        self.dialog._spins["pitch"].SetValue(0)
        self.dialog._spins["volume"].SetValue(0)
        options = self.dialog.get_options()
        self.dialog._spins["pitch"].SetValue(50)
        self.assertEqual(0, options["pitch"])
        self.assertEqual(0, options["volume"])
        self.assertEqual("British English - Shelley", options["voiceLabel"])

    def test_unsupported_voice_parameters_are_disabled_and_not_rendered(self):
        self.dialog._loading = False
        del self.dialog._languages[0]["profiles"]["1"][0]["defaults"]["breathiness"]
        self.dialog._set_language(0, 1)
        self.assertFalse(self.dialog._spins["breathiness"].IsEnabled())
        self.assertNotIn("breathiness", self.dialog.get_options())

    def test_delayed_preview_respects_auto_speak_being_disabled(self):
        self.dialog._closed = False
        self.dialog._loading = False
        self.dialog.autoSpeakCB.SetValue(False)
        self.dialog._on_test(None)
        # No worker state was created: the delayed call returned before starting.
        self.assertFalse(hasattr(self.dialog, "_busy"))

    def test_numeric_mnemonics_are_unique_in_dialog(self):
        labels = [label for name, label, maximum in self.controls]
        labels += ["&Voice:", "Varia&nt:", "Sa&mple rate:", "&Test", "&Auto speak"]
        keys = [label.split("&", 1)[1][0].casefold() for label in labels]
        self.assertEqual(len(keys), len(set(keys)))

    def test_accessible_name_and_shortcut_match_labels(self):
        for name, label, maximum in self.controls:
            accessible = self.accessible_class(Control(), label)
            self.assertEqual((0, label.replace("&", "").rstrip(":")), accessible.GetName(0))
            self.assertEqual((0, "Alt+" + label.split("&", 1)[1][0].upper()), accessible.GetKeyboardShortcut(0))
            self.assertEqual((0, "0"), accessible.GetValue(0))
            self.assertEqual(1, accessible.GetName(1)[0])

    def test_preview_and_main_render_pass_the_same_snapshot(self):
        main = self.source.parents[1] / "main.py"
        tree = ast.parse(main.read_text(encoding="utf-8-sig"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_render_with_ibmeci_dll"]
        self.assertEqual(1, len(calls))
        self.assertEqual("eci", next(item.value.id for item in calls[0].keywords if item.arg == "opts"))
        preview = ast.parse(self.source.read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(preview) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_render_with_ibmeci_dll"]
        self.assertEqual("options", next(item.value.id for item in calls[0].keywords if item.arg == "opts"))


if __name__ == "__main__":
    unittest.main()
