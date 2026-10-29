"""Capture ownership without touching live NVDA, configuration or audio devices."""
import ast
import ctypes
import gc
from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
import weakref


SOURCE = Path(__file__).resolve().parents[1] / 'source/soundWave_lib/synths/nvda_capture.py'


class PlaybackPlayer:
    _instances = {}

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.played = bytearray()
        self.closed = False

    def feed(self, data, *args, **kwargs):
        self.played.extend(data)

    def stop(self):
        pass

    def close(self):
        self.closed = True


class Notification:
    def __init__(self):
        self.callbacks = []

    def register(self, callback):
        self.callbacks.append(callback)

    def unregister(self, callback):
        self.callbacks.remove(callback)

    def notify(self, **kwargs):
        for callback in list(self.callbacks):
            callback(**kwargs)


class CaptureRoutingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=os.environ.get('SOUNDWAVE_TEST_TEMP'))
        self.addCleanup(self.folder.cleanup)
        self.output = str(Path(self.folder.name) / 'capture.wav')
        self.wave = types.SimpleNamespace(WavePlayer=PlaybackPlayer)
        self.notification = Notification()
        self.started = threading.Event()
        self.finish = threading.Event()
        self.constructing = threading.Event()
        self.construct = threading.Event()
        self.construct.set()
        self.cancel = threading.Event()
        self.errors = []
        self.capture = None
        self.replace_player = False
        case = self

        class Driver:
            supportedNotifications = {case.notification}

            def __init__(self, render=False):
                self.player = case.wave.WavePlayer(1, 24000, 16, outputDevice='fixture')
                self.worker = None
                self.terminated = False
                if render:
                    case.capture = self
                    case.constructing.set()
                    if not case.construct.wait(3):
                        raise RuntimeError('Fixture constructor timed out')

            def speak(self, sequence):
                self.worker = threading.Thread(target=self.generate)
                self.worker.start()

            def generate(self):
                if case.replace_player:
                    self.player = case.wave.WavePlayer(1, 24000, 16)
                self.player.feed(b'\x01\x00' * 20)
                case.started.set()
                if not case.finish.wait(3):
                    return
                self.player.feed(b'\x02\x00' * 20)
                case.notification.notify(synth=self)

            def cancel(self):
                case.finish.set()

            def terminate(self):
                if self.worker:
                    self.worker.join(2)
                self.player.close()
                self.terminated = True

        self.Driver = Driver
        self.namespace = dict(
            ctypes=ctypes, sys=sys, threading=threading, time=time, wave=wave,
            nvwave=self.wave, synthDriverHandler=types.SimpleNamespace(synthDoneSpeaking=self.notification),
            TIMEOUT_SECONDS=5, _=lambda text: text,
            _reject_separate_nvda_synth_host=lambda name: None,
            _get_synth_instance=lambda name: Driver(render=True),
            _snapshot_synth_settings=lambda driver: {}, _restore_synth_settings=lambda *args: None,
            _prepare_generic_capture_text=lambda text, name: text,
            google_tts=types.SimpleNamespace(is_google_tts_synth=lambda name: False),
            pocket_tts=types.SimpleNamespace(is_pocket_tts_synth=lambda name: False),
        )
        names = {'_GenericCaptureWavePlayer', '_make_capture_wave_player_factory',
                 '_render_with_nvda_generic_capture'}
        nodes = [node for node in ast.parse(SOURCE.read_text(encoding='utf-8-sig')).body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
            module='__future__', names=[ast.alias(name='annotations')], level=0)] + nodes, type_ignores=[]))
        exec(compile(module, str(SOURCE), 'exec'), self.namespace)
        self.render_thread = None
        self.addCleanup(self.finish_render)

    def render(self):
        try:
            self.namespace['_render_with_nvda_generic_capture'](
                'A render in progress.', self.output, 'fixture', cancel_evt=self.cancel)
        except Exception as error:
            self.errors.append(error)

    def start_render(self, constructing=False):
        self.render_thread = threading.Thread(target=self.render)
        self.render_thread.start()
        self.assertTrue((self.constructing if constructing else self.started).wait(3))

    def finish_render(self):
        self.construct.set()
        self.finish.set()
        if self.render_thread:
            self.render_thread.join(3)
            self.assertFalse(self.render_thread.is_alive())

    def assert_capture_complete(self):
        self.finish_render()
        self.assertEqual(self.errors, [])
        with wave.open(self.output) as output:
            self.assertEqual(output.readframes(output.getnframes()), b'\x01\x00' * 20 + b'\x02\x00' * 20)
        self.assertTrue(self.capture.terminated)
        self.assertEqual(self.notification.callbacks, [])

    def test_switching_live_synth_keeps_real_playback_during_and_after_render(self):
        self.start_render()
        live = self.wave.WavePlayer(1, 22050, 16, outputDevice='headphones')
        self.assertIsInstance(live, PlaybackPlayer)
        self.assertEqual(live.kwargs['outputDevice'], 'headphones')
        live.feed(b'live speech')
        self.assert_capture_complete()
        self.assertEqual(live.played, b'live speech')
        self.assertFalse(live.closed)
        self.assertIs(self.wave.WavePlayer, PlaybackPlayer)

    def test_switching_to_the_same_driver_class_is_not_capture(self):
        self.start_render()
        live = self.Driver()
        self.assertIsInstance(live.player, PlaybackPlayer)
        self.assert_capture_complete()
        self.assertFalse(live.terminated)

    def test_unrelated_background_sound_is_not_captured(self):
        self.start_render()
        players = []
        background = threading.Thread(target=lambda: players.append(self.wave.WavePlayer(2, 48000, 16)))
        background.start()
        background.join(2)
        self.assertFalse(background.is_alive())
        self.assertIsInstance(players[0], PlaybackPlayer)
        self.assert_capture_complete()

    def test_render_worker_can_replace_its_own_player(self):
        self.replace_player = True
        self.start_render()
        self.assert_capture_complete()

    def test_cancellation_does_not_close_or_redirect_new_live_player(self):
        self.start_render()
        live = self.Driver()
        self.assertIsInstance(live.player, PlaybackPlayer)
        self.cancel.set()
        self.finish_render()
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(str(self.errors[0]), 'Cancelled.')
        self.assertFalse(live.player.closed)
        self.assertIs(self.wave.WavePlayer, PlaybackPlayer)
        self.assertEqual(self.notification.callbacks, [])

    def test_simultaneous_live_constructor_is_not_hijacked(self):
        self.construct.clear()
        self.start_render(constructing=True)
        live = self.Driver()
        self.assertIsInstance(live.player, PlaybackPlayer)
        self.assert_capture_complete()

    def test_initialization_failure_restores_factory(self):
        def fail(name):
            raise RuntimeError('Fixture initialization failure')
        self.namespace['_get_synth_instance'] = fail
        self.render()
        self.assertEqual(str(self.errors[0]), 'Fixture initialization failure')
        self.assertIs(self.wave.WavePlayer, PlaybackPlayer)
        self.assertEqual(self.notification.callbacks, [])

    def test_later_audio_hook_is_not_overwritten_by_cleanup(self):
        self.start_render()
        later_hook = lambda *args, **kwargs: PlaybackPlayer(*args, **kwargs)
        self.wave.WavePlayer = later_hook
        self.assert_capture_complete()
        self.assertIs(self.wave.WavePlayer, later_hook)

    def test_factory_preserves_real_native_callback_registry(self):
        players = []
        factory = self.namespace['_make_capture_wave_player_factory'](players, PlaybackPlayer)
        self.assertIs(factory._instances, PlaybackPlayer._instances)

    def test_live_constructor_resolves_native_callback_through_module_class(self):
        case = self
        callback = object()

        class NativePlayer(PlaybackPlayer):
            _callback = callback

            def __init__(self, *args, **kwargs):
                # NVDA's real __init__ uses WavePlayer._callback, not self._callback.
                self.native_callback = case.wave.WavePlayer._callback
                super().__init__(*args, **kwargs)

        factory = self.namespace['_make_capture_wave_player_factory'](
            [], NativePlayer, {'initializing': False})
        self.wave.WavePlayer = factory
        live = factory(1, 22050, 16)
        self.assertIs(live.native_callback, callback)
        self.assertIsInstance(live, NativePlayer)

    def test_native_class_state_writes_survive_capture_cleanup(self):
        class NativePlayer(PlaybackPlayer):
            _silenceDevice = 'old device'
            audioDeviceError_static = False

        factory = self.namespace['_make_capture_wave_player_factory']([], NativePlayer)
        self.assertEqual(factory._silenceDevice, 'old device')
        factory._silenceDevice = 'new device'
        factory.audioDeviceError_static = True
        self.assertEqual(NativePlayer._silenceDevice, 'new device')
        self.assertTrue(NativePlayer.audioDeviceError_static)
        NativePlayer._silenceDevice = 'changed by native idle callback'
        self.assertEqual(factory._silenceDevice, NativePlayer._silenceDevice)

    def test_factory_keeps_live_player_type_checks_working(self):
        factory = self.namespace['_make_capture_wave_player_factory']([], PlaybackPlayer)
        self.assertIsInstance(PlaybackPlayer(), factory)
        self.assertTrue(issubclass(PlaybackPlayer, factory))

    def test_cached_factory_does_not_retain_the_finished_render_driver(self):
        self.start_render()
        cached_factory = self.wave.WavePlayer
        reference = weakref.ref(self.capture)
        self.assert_capture_complete()
        self.capture = None
        gc.collect()
        self.assertIsNone(reference())
        self.assertIsInstance(cached_factory(1, 22050, 16), PlaybackPlayer)


if __name__ == '__main__':
    unittest.main()
