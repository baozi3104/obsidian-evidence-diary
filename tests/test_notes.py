import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

try:
    from obsidian_sync.notes import VaultWriter, WriteConflict
except ImportError:
    VaultWriter = None
    WriteConflict = RuntimeError


class NotesTest(unittest.TestCase):
    def setUp(self):
        if VaultWriter is None:
            self.fail('VaultWriter feature is not implemented')
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / 'vault'
        self.vault.mkdir()
        self.writer = VaultWriter(self.vault, Path(self.temp.name) / 'backups')

    def test_daily_preserves_personal_and_is_idempotent(self):
        target = self.vault / '01-每日记录/2026-10-18.md'
        target.parent.mkdir()
        original = '# 今天\n\n我今天很累，明天再考虑。\n\n## AI\n<!-- auto-ai-summary:start -->\n旧摘要\n<!-- auto-ai-summary:end -->\n\n我后来又补了一句。\n'
        target.write_text(original, encoding='utf-8')
        self.writer.write_daily('2026-10-18', '真实工作记录')
        first = target.read_bytes()
        self.assertIn('我今天很累，明天再考虑。', first.decode())
        self.assertIn('我后来又补了一句。', first.decode())
        self.assertNotIn('旧摘要', first.decode())
        self.writer.write_daily('2026-10-18', '真实工作记录')
        self.assertEqual(first, target.read_bytes())
        self.assertTrue(any(self.writer.backup_dir.rglob('*.md')))

    def test_new_daily_has_personal_and_learning_sections(self):
        p = self.writer.write_daily('2026-10-18', '一条有依据的记录')
        text = p.read_text(encoding='utf-8')
        self.assertIn('个人记录', text)
        self.assertIn('auto-ai-summary:start', text)

    def test_broken_markers_refuse_without_modifying(self):
        p = self.vault / '01-每日记录/2026-10-18.md'
        p.parent.mkdir()
        p.write_text('手写内容\n<!-- auto-ai-summary:start -->\n损坏', encoding='utf-8')
        before = p.read_bytes()
        with self.assertRaises(ValueError):
            self.writer.write_daily('2026-10-18', '新摘要')
        self.assertEqual(before, p.read_bytes())

    def test_topic_preserves_manual_and_updates_knowledge_block(self):
        p = self.vault / '02-学习/ESP32/GPIO.md'
        p.parent.mkdir(parents=True)
        p.write_text('# GPIO\n\n我的实验：接灯后亮了。\n', encoding='utf-8')
        self.writer.write_topic('02-学习/ESP32/GPIO.md', '## 一句话理解\nGPIO是可控引脚。\n## 例子\n把引脚设为高电平。')
        self.writer.write_topic('02-学习/ESP32/GPIO.md', '## 一句话理解\nGPIO是可控引脚。\n## 例子\n设为高电平；注意接线电压。')
        text = p.read_text(encoding='utf-8')
        self.assertIn('我的实验：接灯后亮了。', text)
        self.assertEqual(1, text.count('auto-learning:start'))
        self.assertIn('注意接线电压', text)

    def test_personal_append_has_stable_dedup_and_does_not_touch_ai(self):
        self.writer.write_daily('2026-10-18', '已有AI摘要')
        p = self.writer.append_personal('2026-10-18', '我今天在线下跑步。')
        self.writer.append_personal('2026-10-18', '我今天在线下跑步。')
        text = p.read_text(encoding='utf-8')
        self.assertEqual(1, text.count('我今天在线下跑步。'))
        self.assertIn('已有AI摘要', text)

    def test_rejects_traversal_wrong_area_and_injected_markers(self):
        for path in ['../outside.md', '99-私密/a.md', '02-学习/../../bad.md', '02-学习/a:token.md']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.writer.write_topic(path, '内容')
        with self.assertRaises(ValueError):
            self.writer.write_daily('2026-10-18', '<!-- auto-ai-summary:end -->')

    def test_expected_hash_detects_external_change(self):
        p = self.writer.write_topic('02-学习/Python/异常.md', '旧知识')
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        p.write_text(p.read_text(encoding='utf-8') + '\n用户新写内容', encoding='utf-8')
        with self.assertRaises(WriteConflict):
            self.writer.write_topic('02-学习/Python/异常.md', '新版知识', expected_hash=digest)
        self.assertIn('用户新写内容', p.read_text(encoding='utf-8'))

    def test_validation_record_survives_later_knowledge_update(self):
        p = self.writer.write_topic('02-学习/ESP32/GPIO.md', '旧知识')
        self.writer.append_validation('02-学习/ESP32/GPIO.md', '我独立解释了输入和输出的区别；对话验证通过。')
        self.writer.write_topic('02-学习/ESP32/GPIO.md', '补全后的知识解释和例子')
        text = p.read_text(encoding='utf-8')
        self.assertIn('我独立解释了输入和输出的区别；对话验证通过。', text)
        self.assertIn('补全后的知识解释和例子', text)

    def test_refine_improves_knowledge_and_preserves_personal_sections(self):
        p = self.vault / '02-学习/Python/异常.md'
        p.parent.mkdir(parents=True)
        p.write_text('# 异常\n\n## 定义\n晦涩的旧解释。\n\n## 我的实践记录\n我独立运行后得到了ValueError。\n', encoding='utf-8')
        old_hash = hashlib.sha256(p.read_bytes()).hexdigest()
        self.writer.write_topic('02-学习/Python/异常.md', '## 通俗解释\n异常就是程序遇到了处理不了的情况。\n## 一个例子\nint("a")会报错。\n## 我的验证\n见原实践记录。', expected_hash=old_hash, edit_mode='refine')
        text = p.read_text(encoding='utf-8')
        active = text.split('<!-- auto-learning:start -->')[1].split('<!-- auto-learning:end -->')[0]
        self.assertNotIn('晦涩的旧解释。', active)
        self.assertIn('我独立运行后得到了ValueError。', text)
        self.assertIn('异常就是程序遇到了处理不了的情况。', text)
        self.assertIn('晦涩的旧解释。', text)  # Original is archived verbatim, not erased.

    def test_refine_preserves_personal_bullet_without_special_heading(self):
        p = self.vault / '02-学习/ESP32/输入.md'
        p.parent.mkdir(parents=True)
        p.write_text('# 输入\n\n## 定义\n旧解释。\n\n- 我实测接地后读取为0。\n', encoding='utf-8')
        old_hash = hashlib.sha256(p.read_bytes()).hexdigest()
        self.writer.write_topic('02-学习/ESP32/输入.md', '## 例子\n读取按键。\n## 我的验证\n待验证。', expected_hash=old_hash, edit_mode='refine')
        self.assertIn('- 我实测接地后读取为0。', p.read_text(encoding='utf-8'))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_junction_cannot_redirect_learning_write_to_private_area(self):
        learning = self.vault / '02-学习'
        private = self.vault / '99-私密'
        learning.mkdir()
        private.mkdir()
        link = learning / 'linked'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(private)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            self.skipTest('Cannot create test junction')
        self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        with self.assertRaises(ValueError):
            self.writer.write_topic('02-学习/linked/private.md', '不应写入')
        self.assertFalse((private / 'private.md').exists())

    def test_refine_preserves_implicit_personal_story_verbatim(self):
        p = self.vault / '02-学习/ESP32/GPIO.md'
        p.parent.mkdir(parents=True)
        original = '# GPIO\n\n## 用法\n旧知识。\n\n昨天接线时把输入口直接接到5V，板子损坏了；换板后才恢复。\n'
        p.write_text(original, encoding='utf-8')
        old = p.read_bytes()
        self.writer.write_topic('02-学习/ESP32/GPIO.md', '## 一个例子\n输入必须在允许电压范围。', expected_hash=hashlib.sha256(old).hexdigest(), edit_mode='refine')
        self.assertIn(original.strip(), p.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
