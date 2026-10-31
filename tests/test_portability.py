import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from obsidian_sync.engine import locate_codex


class PortabilityTests(unittest.TestCase):
    def test_executable_fallback_uses_current_user_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            binary = home / 'AppData/Local/OpenAI/Codex/bin/test-version/codex.exe'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.home', return_value=home):
                self.assertEqual(str(binary), locate_codex({}))
