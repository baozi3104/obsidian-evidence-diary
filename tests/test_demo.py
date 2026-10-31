import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DemoTests(unittest.TestCase):
    def test_demo_handles_console_without_chinese_encoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run(
                [sys.executable, str(root / 'examples/demo.py'),
                 '--output', str(Path(temporary) / 'demo')],
                capture_output=True, env=dict(os.environ, PYTHONIOENCODING='cp1252'))
            self.assertEqual(0, result.returncode, result.stderr.decode('ascii', errors='replace'))
            self.assertIn(b'Offline demo passed', result.stdout)
