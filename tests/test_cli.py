import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliTest(unittest.TestCase):
    def test_global_config_preserves_personal_text_and_verification(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            folder = Path(temporary)
            config = {'vault': str(folder / 'vault'), 'runtime_dir': str(folder / 'runtime'),
                      'backup_dir': str(folder / 'backup'), 'temp_dir': str(folder / 'tmp'),
                      'first_date': '2026-10-12', 'projects': [], 'sources': {}}
            config_path = folder / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            text = '原样保留：测试中文、$符号和`反引号。'
            text_path = folder / 'input.txt'
            text_path.write_text(text, encoding='utf-8')

            def run(arguments):
                result = subprocess.run([sys.executable, str(root / 'daily_ai_summary.py'),
                    '--config', str(config_path)] + arguments,
                    capture_output=True, text=True, encoding='utf-8', env=dict(os.environ, PYTHONUTF8='1'))
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)

            for _ in range(2):
                run(['personal', '--date', '2026-10-18', '--text-file', str(text_path)])
            diary = (folder / 'vault/01-每日记录/2026-10-18.md').read_text(encoding='utf-8')
            self.assertEqual(1, diary.count(text))
            topic = folder / 'vault/02-学习/测试/示例.md'
            topic.parent.mkdir(parents=True)
            topic.write_text('# 示例\n', encoding='utf-8')
            run(['verification', '--topic', '02-学习/测试/示例.md', '--text-file', str(text_path)])
            self.assertIn(text, topic.read_text(encoding='utf-8'))
            run([])
            run(['status'])
