import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from simulator.openhands.config import validate_example


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class OpenHandsConfigTests(unittest.TestCase):
    def test_examples_pin_distinct_control_and_execution_images(self):
        paths = sorted(EXAMPLES.glob("openhands*.json"))
        self.assertTrue(paths)
        for path in paths:
            config = json.loads(path.read_text())
            self.assertEqual(validate_example(config, source=str(path)), [])
            self.assertNotEqual(config["image"], config["execution_image"])

    def test_command_is_static_and_rejects_shared_image(self):
        path = EXAMPLES / "openhands-single.json"
        config = json.loads(path.read_text())
        config["execution_image"] = config["image"]
        with tempfile.NamedTemporaryFile("w", suffix=".json") as fixture:
            json.dump(config, fixture)
            fixture.flush()
            result = subprocess.run(
                [sys.executable, "-m", "simulator.openhands.validate_config", fixture.name],
                cwd=ROOT, text=True, capture_output=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be different", result.stdout)

    def test_command_validates_examples_without_docker(self):
        result = subprocess.run(
            [sys.executable, "-m", "simulator.openhands.validate_config"],
            cwd=ROOT, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"static": true', result.stdout)


if __name__ == "__main__":
    unittest.main()
