import ast
import glob
import os
from pathlib import Path
import sys
import tempfile
import types
from typing import Optional
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "source" / "soundWave_lib" / "synths" / "ibmeci.py"


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("SOUNDWAVE_TEST_TEMP"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_root = self.root / "nvda"
        self.addons = []
        self.base = {}
        self.conf = {}
        self.config = types.ModuleType("config")
        self.config.conf = types.SimpleNamespace(profiles=[self.base])
        self.config.conf.get = self.conf.get
        self.handler = types.ModuleType("addonHandler")
        self.handler.getAvailableAddons = lambda: self.addons
        self.handler.getCodeAddon = lambda: None
        self.global_vars = types.ModuleType("globalVars")
        self.global_vars.appArgs = types.SimpleNamespace(configPath=str(self.config_root))
        modules = {"config": self.config, "addonHandler": self.handler, "globalVars": self.global_vars}
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"APPDATA": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {
            "_find_ibmeci_dll", "_configured_ibmeci_path",
        }]
        namespace = {"os": os, "glob": glob, "Optional": Optional}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.find = namespace["_find_ibmeci_dll"]

    def addon(self, name):
        root = self.config_root / "addons" / name
        root.mkdir(parents=True)
        self.addons.append(types.SimpleNamespace(name=name, path=str(root), isPendingInstall=False))
        return root

    def dll(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"discovery fixture; never load")
        return str(path)

    def configure(self, directory, filename="eci.dll", target=None):
        if target is None:
            target = self.base
        target["ibmeci"] = {"TTSPath": str(directory), "dllName": filename}

    def test_existing_bundled_engine(self):
        addon = self.addon("IBMTTS")
        expected = self.dll(addon / "synthDrivers" / "ibmtts" / "ECI.DLL")
        self.assertEqual(expected.casefold(), self.find("IBMTTS").casefold())

    def test_external_directory_and_custom_filename(self):
        self.addon("IBMTTS")
        directory = self.root / "Program Files (x86)" / "ViaVoiceTTS"
        expected = self.dll(directory / "ibmeci.dll")
        self.configure(directory, "ibmeci.dll")
        self.assertEqual(expected, self.find("IBMTTS"))

    def test_base_profile_takes_precedence(self):
        self.addon("IBMTTS")
        expected = self.dll(self.root / "general" / "ibmeci.dll")
        other = self.dll(self.root / "triggered" / "eci.dll")
        self.configure(Path(expected).parent, "ibmeci.dll")
        self.configure(Path(other).parent, target=self.conf)
        self.assertEqual(expected, self.find("IBMTTS"))

    def test_active_config_fallback_without_base_section(self):
        self.addon("IBMTTS")
        expected = self.dll(self.root / "external" / "ibmeci.dll")
        self.configure(Path(expected).parent, "ibmeci.dll", target=self.conf)
        self.assertEqual(expected, self.find("IBMTTS"))

    def test_relative_eci_libraries_location(self):
        self.addon("IBMTTS")
        expected = self.dll(self.config_root / "addons" / "eciLibraries" / "eci.dll")
        self.configure(os.path.join("..", "..", "eciLibraries"))
        self.assertEqual(expected, self.find("IBMTTS"))

    def test_portable_nvda_config_root(self):
        self.config_root = self.root / "portable" / "userConfig"
        self.global_vars.appArgs.configPath = str(self.config_root)
        addon = self.addon("ibmtts")
        expected = self.dll(addon / "synthDrivers" / "ibmtts" / "eci.dll")
        self.assertEqual(expected.casefold(), self.find("IBMTTS").casefold())

    def test_both_addons_keep_their_own_library(self):
        ibm = self.addon("IBMTTS")
        eloquence = self.addon("Eloquence")
        ibm_path = self.dll(ibm / "synthDrivers" / "ibmtts" / "ECI.DLL")
        eloquence_path = self.dll(eloquence / "synthDrivers" / "eloquence" / "ECI.DLL")
        self.assertEqual(ibm_path, self.find("IBMTTS"))
        self.assertEqual(eloquence_path, self.find("Eloquence"))

    def test_general_profile_options_fall_back_per_key(self):
        self.addon("IBMTTS")
        expected = self.dll(self.root / "general" / "ibmeci.dll")
        self.base["ibmeci"] = {"TTSPath": str(Path(expected).parent)}
        self.conf["ibmeci"] = {"TTSPath": str(self.root / "other"), "dllName": "ibmeci.dll"}
        self.assertEqual(expected, self.find("IBMTTS"))

    def test_config_alone_does_not_advertise_missing_driver(self):
        expected = self.dll(self.root / "external" / "ibmeci.dll")
        self.configure(Path(expected).parent, "ibmeci.dll")
        self.assertEqual("", self.find("IBMTTS"))

    def test_config_root_fallback_without_addon_inventory(self):
        self.config_root = self.root / "portable" / "userConfig"
        self.global_vars.appArgs.configPath = str(self.config_root)
        addon = self.addon("IBMTTS")
        expected = self.dll(addon / "synthDrivers" / "ibmtts" / "ECI.DLL")
        self.addons.clear()
        self.assertEqual(expected, self.find("IBMTTS"))

    def test_preferred_engine_does_not_use_other_addon(self):
        addon = self.addon("IBMTTS")
        self.dll(addon / "synthDrivers" / "ibmtts" / "eci.dll")
        self.assertEqual("", self.find("Eloquence"))

    def test_missing_configured_engine_does_not_use_stale_bundled_copy(self):
        addon = self.addon("IBMTTS")
        self.dll(addon / "synthDrivers" / "ibmtts" / "eci.dll")
        self.configure(self.root / "missing", "ibmeci.dll")
        self.assertEqual("", self.find("IBMTTS"))

    def test_generic_lookup_can_find_external_ibm_engine(self):
        self.addon("IBMTTS")
        expected = self.dll(self.root / "external" / "ibmeci.dll")
        self.configure(Path(expected).parent, "ibmeci.dll")
        self.assertEqual(expected, self.find())

    def test_pending_install_is_not_discovered(self):
        pending = self.addon("IBMTTS.pendingInstall")
        self.addons[-1].name = "IBMTTS"
        self.addons[-1].isPendingInstall = True
        self.dll(pending / "synthDrivers" / "ibmtts" / "eci.dll")
        self.assertEqual("", self.find("IBMTTS"))

    def test_picker_advertises_external_engine_only_as_ibmtts(self):
        self.addon("IBMTTS")
        expected = self.dll(self.root / "external" / "ibmeci.dll")
        self.configure(Path(expected).parent, "ibmeci.dll")
        tree = ast.parse((SOURCE.parents[1] / "main.py").read_text(encoding="utf-8-sig"))
        dialog = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SynthSelectDialog")
        init = next(node for node in dialog.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        loop = next(node for node in init.body if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple)
                    and [item.id for item in node.target.elts] == ["addon_name", "label"])
        dialog_state = types.SimpleNamespace(_choice_meta=[])
        choices = []
        exec(compile(ast.Module(body=[loop], type_ignores=[]), str(SOURCE), "exec"),
             {"self": dialog_state, "choices": choices, "_find_ibmeci_dll": self.find})
        self.assertEqual(["IBMTTS"], choices)
        self.assertEqual([{"kind": "ibmeci", "label": "IBMTTS", "eciDllPath": expected}], dialog_state._choice_meta)


if __name__ == "__main__":
    unittest.main()
