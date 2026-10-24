"""MicMix's mixing math and the choices around it. From this folder: py -m unittest test_micmix"""
import unittest
from array import array
from types import SimpleNamespace
from unittest import mock

import micmix as mm  # micmix.pyw: Windows imports .pyw sources


class Mixing(unittest.TestCase):
    def test_slider(self):
        self.assertEqual(mm.gains(0), (1, 0))  # voice only
        self.assertEqual(mm.gains(0.25), (1, 0.25))
        self.assertEqual(mm.gains(0.5), (1, 1))  # both full
        self.assertEqual(mm.gains(1), (0, 1))  # music only

    def test_mix_clips_instead_of_wrapping(self):
        voice = array("h", [30000, -30000, 1000, 0]).tobytes()
        music = array("h", [30000, -30000, 0, -1000]).tobytes()
        self.assertEqual(array("h", mm.mix(voice, music, 1, 1)).tolist(), [32767, -32768, 1000, -1000])
        self.assertEqual(mm.mix(voice, music, 1, 0), voice)
        self.assertEqual(array("h", mm.mix(voice, music, 0.5, 0)).tolist(), [15000, -15000, 500, 0])


class Choices(unittest.TestCase):
    def test_app_pid_prefers_the_tree_with_sound(self):
        procs = {1: ("explorer.exe", 0), 10: ("App.exe", 1), 11: ("App.exe", 10),  # two separate App trees
                 20: ("App.exe", 1), 21: ("App.exe", 20)}
        with mock.patch.object(mm, "processes", return_value=procs):
            with mock.patch.object(mm, "session_pids", return_value={21}):
                self.assertEqual(mm.app_pid("APP.EXE"), 20)  # the tree that plays, whatever the case
            with mock.patch.object(mm, "session_pids", return_value=set()):
                self.assertEqual(mm.app_pid("app.exe"), 10)  # none plays: the first top process
        self.assertTrue(mm.is_app("WINWORD.EXE"))
        self.assertFalse(mm.is_app("Headphones (Headset)"))

    def test_voice_survives_the_music_failing(self):
        sent, reads = [], iter([bytes(mm.BLOCK)] * 3)

        class FakeMic:
            def __init__(self, *_):
                pass

            def read(self):
                return next(reads)  # three blocks, then StopIteration ends the engine

            def write(self, data):
                sent.append(data)

            def close(self):
                pass

        def no_music(source):
            raise OSError(f"Music from: {source} isn't running")

        with mock.patch.multiple(mm, Mic=FakeMic, Speaker=FakeMic, Loopback=no_music, devices=lambda io: ["Mic"]):
            engine = mm.Engine(SimpleNamespace(gain=(1.0, 0.0), peak=0), 0, 1, "Spotify.exe")
            engine.join(2)
        self.assertEqual(len(sent), 3)  # the voice kept going
        self.assertIn("your mic only", engine.warning)


if __name__ == "__main__":
    unittest.main()
