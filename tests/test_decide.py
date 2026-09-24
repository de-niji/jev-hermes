import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

from helpers import OFFLINE_URL, SCRIPTS, choice, noul

import jev_decide as jd


class DecideErrors(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_network_error_raises_jev_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(jd.JevError):
                jd.decide("x", jd.PRESETS["intent"], "m")

    def test_timeout_raises_jev_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(jd.JevError):
                jd.decide("x", jd.PRESETS["intent"], "m")

    def test_http_error_includes_status(self):
        err = urllib.error.HTTPError(jd.DECISIONS_URL, 400, "bad", {}, io.BytesIO(b"bad criteria"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(jd.JevError, "HTTP 400: bad criteria"):
                jd.decide("x", jd.PRESETS["intent"], "m")

    def test_invalid_json_raises_jev_error(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"not json"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(jd.JevError):
                jd.decide("x", jd.PRESETS["intent"], "m")


class LoadKey(unittest.TestCase):
    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": " k1 "}):
            self.assertEqual(jd._load_key(), "k1")

    def test_env_file_fallback(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text('# comment\nOPENROUTER_API_KEY="k2"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(jd, "ENV_FILES", (env,)):
                self.assertEqual(jd._load_key(), "k2")

    def test_missing_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(jd, "ENV_FILES", ()):
            with self.assertRaises(jd.JevError):
                jd._load_key()


class Formatting(unittest.TestCase):
    def test_brief(self):
        out = {"answers": {"route": choice("calendar", 1.0), "needs_tools": noul(0.91)},
               "usage": {"cost": 2e-05}}
        self.assertEqual(jd._brief(out), "route=calendar(1.0) needs_tools=0.91 cost=2e-05")

    def test_min_confidence(self):
        out = {"answers": {"a": choice("x", 0.9), "b": choice("y", 0.4), "c": noul(0.1)}}
        self.assertEqual(jd._min_confidence(out), 0.4)
        self.assertIsNone(jd._min_confidence({"answers": {"c": noul(0.1)}}))


class Cli(unittest.TestCase):
    def test_api_error_exits_2(self):
        env = {**os.environ, "OPENROUTER_API_KEY": "x", "JEV_DECISIONS_URL": OFFLINE_URL}
        p = subprocess.run([sys.executable, str(SCRIPTS / "jev_decide.py"), "--state", "hi",
                            "--preset", "intent"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("request failed", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_presets_are_valid_shapes(self):
        # choice criteria must be a record, score criteria a list (HTTP 400 otherwise).
        for preset in jd.PRESETS.values():
            for q in preset.values():
                if q["type"] == "choice":
                    self.assertIsInstance(q["criteria"], dict)
                elif q["type"] == "score":
                    self.assertIsInstance(q["criteria"], list)
                else:
                    self.assertEqual(q["type"], "noul")
                    self.assertIn("true", q)
                    self.assertIn("false", q)


if __name__ == "__main__":
    unittest.main()
