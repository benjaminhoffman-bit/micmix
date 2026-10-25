"""MicMix's math and the choices around it, without any device. From this folder: py -m unittest test_micmix"""
import tempfile
import time
import unittest
from array import array
from pathlib import Path
from unittest import mock

import audio
import micmix  # micmix.pyw: Windows imports .pyw sources


def block(*samples: int) -> bytes:
    return array("h", samples).tobytes()


class Mixing(unittest.TestCase):
    def test_slider(self):
        self.assertEqual(audio.gains(0), (1, 0))  # voice only
        self.assertEqual(audio.gains(0.25), (1, 0.25))
        self.assertEqual(audio.gains(0.5), (1, 1))  # both full
        self.assertEqual(audio.gains(1), (0, 1))  # music only

    def test_mix_clips_instead_of_wrapping(self):
        voice, music = block(30000, -30000, 1000, 0), block(30000, -30000, 0, -1000)
        self.assertEqual(array("h", audio.mix((voice, 1, 1), (music, 1, 1))).tolist(), [32767, -32768, 1000, -1000])
        self.assertEqual(audio.mix((voice, 1, 1), (music, 0, 0)), voice)
        self.assertEqual(array("h", audio.mix((voice, 0.5, 0.5))).tolist(), [15000, -15000, 500, 0])

    def test_mix_fades_across_the_block(self):
        flat = block(*[16384] * 8)  # 4 stereo frames: the gain steps once per frame
        self.assertEqual(array("h", audio.mix((flat, 1.0, 0.0))).tolist(),
                         [16384, 16384, 12288, 12288, 8192, 8192, 4096, 4096])

    def test_gate_holds_then_fades_out(self):
        gate, loud, quiet = audio.Gate(), 10000, 10  # about -10 and -70 dBFS
        self.assertEqual(gate.step(loud, -40), (1.0, 1.0))
        for _ in range(audio.GATE_HOLD):
            self.assertEqual(gate.step(quiet, -40), (1.0, 1.0))  # held open: word endings stay
        self.assertEqual(gate.step(quiet, -40), (1.0, 0.8))  # then a fade, not a cut
        for _ in range(4):
            gate.step(quiet, -40)
        self.assertEqual(gate.gain, 0.0)
        self.assertEqual(gate.step(quiet, None), (1.0, 1.0))  # off: always open

    def test_soundboard_plays_at_its_volume_then_stops(self):
        s, playing = audio.shared_state(), []
        s.board = 0.5
        clip = block(*[1000] * (audio.BLOCK // 2 + 2))  # one block and two samples
        s.clips.put(clip)
        self.assertEqual(array("h", audio.Engine.sounds(playing, s))[0], 500)
        self.assertEqual(array("h", audio.Engine.sounds(playing, s))[:3].tolist(), [500, 500, 0])  # the tail, padded
        self.assertIsNone(audio.Engine.sounds(playing, s))  # finished
        s.clips.put(clip)
        s.clips.put(audio.STOP)
        self.assertIsNone(audio.Engine.sounds(playing, s))  # Stop sounds silences everything
        s.clips.put(clip)
        audio.Engine.sounds(playing, s)
        s.clips.put(audio.STOP)  # mid-play: one block that fades to nothing, so it doesn't click
        faded = array("h", audio.Engine.sounds(playing, s))
        self.assertEqual((faded[0], faded[-1]), (500, 0))
        self.assertIsNone(audio.Engine.sounds(playing, s))


class Choices(unittest.TestCase):
    def test_app_pid_prefers_the_tree_with_sound(self):
        procs = {1: ("explorer.exe", 0), 10: ("App.exe", 1), 11: ("App.exe", 10),  # two separate App trees
                 20: ("App.exe", 1), 21: ("App.exe", 20)}
        with mock.patch.object(audio, "processes", return_value=procs):
            with mock.patch.object(audio, "session_pids", return_value={21}):
                self.assertEqual(audio.app_pid("APP.EXE"), 20)  # the tree that plays, whatever the case
            with mock.patch.object(audio, "session_pids", return_value=set()):
                self.assertEqual(audio.app_pid("app.exe"), 10)  # none plays: the first top process
        self.assertTrue(audio.is_app("WINWORD.EXE"))
        self.assertFalse(audio.is_app("Headphones (Headset)"))

    def test_voice_survives_the_music_failing(self):
        sent, reads, opened = [], iter([bytes(audio.BLOCK)] * 3), []

        class FakeDevice:
            def __init__(self, *_):
                pass

            def read(self):
                time.sleep(0.0005)  # time for the engine's look for the app, on its own thread
                return next(reads)  # three blocks, then StopIteration ends the engine

            def write(self, data):
                sent.append(data)

            def close(self):
                pass

        def no_music(source):
            raise OSError(f"Music from: {source} isn't running")

        def late_music(source, pid=None):  # not running at first, then started
            opened.append(pid)
            if len(opened) == 1:
                no_music(source)
            return mock.Mock(block=lambda: bytes(audio.BLOCK))

        with mock.patch.multiple(audio, Mic=FakeDevice, Speaker=FakeDevice, Loopback=no_music,
                                 devices=lambda io: ["Mic"]):
            engine = audio.Engine(audio.shared_state(), 0, 1, "Spotify.exe", None)
            engine.join(2)
            self.assertEqual(len(sent), 3)  # the voice kept going
            self.assertIn("your mic only", engine.warning)
            reads = iter([bytes(audio.BLOCK)] * 3)
            engine = audio.Engine(audio.shared_state(), 0, 1, 1, None)  # Music from = Send to: it'd loop
            engine.join(2)
            self.assertEqual(len(sent), 6)  # the voice still goes, without the music
            self.assertIn("loop", engine.warning)
            reads = iter([bytes(audio.BLOCK)] * 201)
            with mock.patch.object(audio, "Loopback", late_music), mock.patch.object(audio, "app_pid", lambda exe: 42):
                engine = audio.Engine(audio.shared_state(), 0, 1, "Spotify.exe", None)
                engine.join(5)
            self.assertEqual((opened, engine.warning), ([None, 42], ""))  # found a few s later; the status says so

    def test_bad_settings_are_kept_aside(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(micmix, "SETTINGS",
                                                                         Path(folder) / "micmix.json"):
            micmix.SETTINGS.write_text('{"fade": 0.4}', encoding="utf-8-sig")  # with a BOM, as Notepad saves
            self.assertEqual(micmix.load(), {"fade": 0.4})
            micmix.SETTINGS.write_text('{"fade": 0.4,}', encoding="utf-8")  # one comma too many
            self.assertEqual(micmix.load(), {})
            self.assertTrue((Path(folder) / "micmix.json.bad").is_file())  # not saved over

    def test_brand_assets_ship(self):
        for name in ("micmix.ico", "sd-28.png", "sd-40.png", "sd-56.png", "fonts/Oxanium.ttf", "fonts/OFL-Oxanium.txt",
                     "fonts/IBMPlexMono-400.ttf", "fonts/IBMPlexMono-700.ttf", "fonts/OFL-IBM-Plex.txt"):
            self.assertTrue((micmix.ASSETS / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
