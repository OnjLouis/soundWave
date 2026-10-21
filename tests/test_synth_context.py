import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'source/soundWave_lib/main.py'


class SynthContextTests(unittest.TestCase):
    def setUp(self):
        self.active = types.SimpleNamespace(name='apolloJuno', voice='uk', rate=75, pitch=50, volume=80)
        self.temporary = types.SimpleNamespace(name='apolloJuno', voice='apollo', rate=50, pitch=30, volume=40)
        self.current = self.active
        self.ring = types.SimpleNamespace(settings=[types.SimpleNamespace(synth=self.active)])
        self.ring.updateSupportedSettings = Mock(side_effect=self.bind_ring)
        self.global_vars = types.SimpleNamespace(settingsRing=self.ring)
        self.dictionaries = types.SimpleNamespace(loadVoiceDict=Mock())
        self.handler = types.SimpleNamespace(getSynth=lambda: self.current)
        self.pending = []
        self.wx = types.SimpleNamespace(IsMainThread=lambda: True,
                                        CallAfter=lambda callback: self.pending.append(callback))
        self.modules = patch.dict(sys.modules, {'globalVars': self.global_vars,
                                                'speechDictHandler': self.dictionaries})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.namespace = dict(synthDriverHandler=self.handler, wx=self.wx,
                              importlib=types.SimpleNamespace(import_module=Mock(side_effect=ImportError)),
                              _resolve_synth_id=lambda name: name, log=Mock())
        names = {'_get_synth_instance', '_create_synth_instance', '_restore_active_synth_context'}
        nodes = [node for node in ast.parse(SOURCE.read_text(encoding='utf-8-sig')).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.namespace)

    def bind_ring(self, synth):
        self.ring.settings = [types.SimpleNamespace(synth=synth)]

    def create(self, name):
        self.bind_ring(self.temporary)
        return self.temporary

    def get(self):
        return self.namespace['_get_synth_instance']('apolloJuno')

    def assert_active_controls(self):
        self.assertTrue(all(setting.synth is self.current for setting in self.ring.settings))
        self.assertEqual((self.active.voice, self.active.rate, self.active.pitch, self.active.volume), ('uk',75,50,80))

    def test_public_factory_cannot_steal_live_ring_or_voice_dictionary(self):
        self.handler.getSynthInstance = self.create
        self.assertIs(self.get(), self.temporary)
        self.assert_active_controls()
        self.dictionaries.loadVoiceDict.assert_called_once_with(self.active)

    def test_failed_factory_restores_context_even_when_all_fallbacks_fail(self):
        def failed(name):
            self.create(name)
            raise RuntimeError('Fixture initialization failure')
        self.handler.getSynthInstance = failed
        self.assertIsNone(self.get())
        self.assert_active_controls()

    def test_fallback_constructor_also_cannot_steal_live_ring(self):
        self.namespace['importlib'].import_module = Mock(return_value=types.SimpleNamespace(SynthDriver=type(
            'FixtureDriver', (), {'__new__': lambda cls: self.create('fixture')})))
        self.assertIs(self.get(), self.temporary)
        self.assert_active_controls()

    def test_worker_restores_on_gui_thread_and_uses_current_active_synth(self):
        self.handler.getSynthInstance = self.create
        self.wx.IsMainThread = lambda: False
        self.assertIs(self.get(), self.temporary)
        self.assertEqual(len(self.pending),1)
        self.ring.updateSupportedSettings.assert_not_called()
        replacement = types.SimpleNamespace(name='espeak', voice='en')
        self.current = replacement
        self.pending.pop()()
        self.assert_active_controls()
        self.dictionaries.loadVoiceDict.assert_called_once_with(replacement)

    def test_no_active_synth_and_already_correct_context_are_left_alone(self):
        self.handler.getSynthInstance = lambda name: self.temporary
        self.assertIs(self.get(),self.temporary)
        self.ring.updateSupportedSettings.assert_not_called()
        self.dictionaries.loadVoiceDict.assert_not_called()
        self.current = None
        self.handler.getSynthInstance = self.create
        self.assertIs(self.get(),self.temporary)
        self.ring.updateSupportedSettings.assert_not_called()

    def test_restoration_failure_does_not_hide_a_valid_render_driver(self):
        self.handler.getSynthInstance = self.create
        self.ring.updateSupportedSettings.side_effect = RuntimeError('Fixture restore failure')
        self.assertIs(self.get(),self.temporary)
        self.namespace['log'].warning.assert_called_once()


if __name__ == '__main__':
    unittest.main()
