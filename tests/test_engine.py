from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from obsidian_sync.engine import SyncEngine, validate_result, due_dates
    import obsidian_sync.engine as engine_module
except ImportError:
    SyncEngine = None


class EngineTest(unittest.TestCase):
    def setUp(self):
        if SyncEngine is None:
            self.fail('SyncEngine feature is not implemented')
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = {'vault': str(root / 'vault'), 'runtime_dir': str(root / 'runtime'),
                       'backup_dir': str(root / 'backups'), 'temp_dir': str(root / 'tmp'),
                       'first_date': '2026-10-12', 'projects': [], 'sources': {}}
        self.events = [{'id': 'e1', 'source': 'codex', 'session_id': 't1', 'project': 'test',
                        'timestamp': '2026-10-18T10:00:00+08:00', 'role': 'user',
                        'kind': 'message', 'text': '我想试一下', 'evidence': {}}]
        self.collect = lambda cfg, start, end: {'events': self.events, 'stats': {}, 'warnings': []}

    def result(self):
        return {'items': [{'section': 'work', 'project': 'test', 'status': 'attempted',
                           'text': '讨论并尝试一种方法，尚无验证结果。', 'evidence_ids': ['e1']}],
                'topics': []}

    def test_failure_does_not_create_diary_or_success_cursor(self):
        def fail(*args):
            raise RuntimeError('network unavailable')
        engine = SyncEngine(self.config, collector=self.collect, generator=fail)
        with self.assertRaises(RuntimeError):
            engine.sync_day(date(2026, 10, 18))
        self.assertFalse((Path(self.config['vault']) / '01-每日记录/2026-10-18.md').exists())
        state = json.loads(engine.state_path.read_text(encoding='utf-8'))
        self.assertEqual('failed', state['days']['2026-10-18']['status'])
        self.assertNotIn('fingerprint', state['days']['2026-10-18'])

    def test_success_is_idempotent_and_new_late_event_updates_original_day(self):
        calls = []
        def generate(*args):
            calls.append(1)
            return self.result()
        engine = SyncEngine(self.config, collector=self.collect, generator=generate)
        engine.sync_day(date(2026, 10, 18))
        p = Path(self.config['vault']) / '01-每日记录/2026-10-18.md'
        original = p.read_bytes()
        engine.sync_day(date(2026, 10, 18))
        self.assertEqual(1, len(calls))
        self.assertEqual(original, p.read_bytes())
        self.events.append(dict(self.events[0], id='e2', text='晚上新增证据'))
        engine.sync_day(date(2026, 10, 18))
        self.assertEqual(2, len(calls))
        self.assertTrue(p.exists())

    def test_successful_retry_clears_previous_error(self):
        engine = SyncEngine(self.config, collector=self.collect, generator=lambda *a: self.result())
        day = date(2026, 10, 18)
        engine.record(day, 'failed', error='previous connection failure')
        engine.sync_day(day)
        entry = json.loads(engine.state_path.read_text(encoding='utf-8'))['days'][str(day)]
        self.assertEqual('success', entry['status'])
        self.assertNotIn('error', entry)

    def test_invalid_native_result_is_not_reused_forever_from_cache(self):
        responses = [self.result(), self.result(), self.result()]
        responses[0]['items'][0]['status'] = 'verified'
        calls = []

        def native(command, **kwargs):
            calls.append(command)
            target = Path(command[command.index('--output-last-message') + 1])
            target.write_text(json.dumps(responses[len(calls) - 1]), encoding='utf-8')
            return type('Completed', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()

        with patch.object(engine_module, 'locate_codex', return_value='test-codex'), \
                patch.object(engine_module.subprocess, 'run', side_effect=native):
            with self.assertRaises(ValueError):
                engine_module.codex_generate(self.config, '2026-10-18', self.events, [])
            result = engine_module.codex_generate(self.config, '2026-10-18', self.events, [])
            self.assertEqual('attempted', result['items'][0]['status'])
            engine_module.codex_generate(self.config, '2026-10-18', self.events, [])
            self.assertEqual(2, len(calls))
            cache = next((Path(self.config['runtime_dir']) / 'model-cache').glob('*.json'))
            cache.write_text(json.dumps(responses[0]), encoding='utf-8')
            result = engine_module.codex_generate(self.config, '2026-10-18', self.events, [])
            self.assertEqual('attempted', result['items'][0]['status'])
            self.assertEqual(3, len(calls))

    def test_fake_evidence_and_unsupported_success_are_rejected(self):
        result = self.result()
        result['items'][0]['evidence_ids'] = ['invented']
        with self.assertRaises(ValueError):
            validate_result(result, self.events)
        result = self.result()
        result['items'][0]['status'] = 'verified'
        with self.assertRaises(ValueError):
            validate_result(result, self.events)

    def test_topic_outside_learning_is_rejected(self):
        result = self.result()
        result['topics'] = [{'path': '../bad.md', 'markdown': '危险路径', 'evidence_ids': ['e1']}]
        with self.assertRaises(ValueError):
            validate_result(result, self.events)

    def test_explicit_tool_error_beats_zero_exit_code(self):
        self.events[0].update(kind='tool_result', role='tool', evidence={'success': False, 'exit_code': 0})
        result = self.result()
        result['items'][0]['status'] = 'verified'
        with self.assertRaises(ValueError):
            validate_result(result, self.events)

    def test_broken_diary_does_not_modify_any_topic(self):
        root = Path(self.config['vault'])
        topic = root / '02-学习/Python/异常.md'
        topic.parent.mkdir(parents=True)
        topic.write_text('# 异常\n\n原来的知识。\n', encoding='utf-8')
        diary = root / '01-每日记录/2026-10-18.md'
        diary.parent.mkdir()
        diary.write_text('我的日记\n<!-- auto-ai-summary:start -->', encoding='utf-8')
        before_topic, before_diary = topic.read_bytes(), diary.read_bytes()
        result = self.result()
        result['topics'] = [{'path': '02-学习/Python/异常.md', 'markdown': '## 例子\ntry/except\n## 我的验证\n待验证', 'evidence_ids': ['e1']}]
        engine = SyncEngine(self.config, collector=self.collect, generator=lambda *a: result)
        with self.assertRaises(ValueError):
            engine.sync_day(date(2026, 10, 18))
        self.assertEqual(before_topic, topic.read_bytes())
        self.assertEqual(before_diary, diary.read_bytes())

    def test_ai_cannot_claim_personal_mastery_and_next_is_only_suggestion(self):
        result = self.result()
        result['topics'] = [{'path': '02-学习/Python/异常.md', 'markdown': '## 例子\ntry/except\n## 我的验证\n我已经熟练掌握。', 'evidence_ids': ['e1']}]
        with self.assertRaises(ValueError):
            validate_result(result, self.events)
        result = self.result()
        result['items'][0]['section'] = 'next'
        with self.assertRaises(ValueError):
            validate_result(result, self.events)

    def test_backfill_dates_include_yesterday_after_a_successful_run(self):
        days = due_dates({'days': {'2026-10-17': {'status': 'success'}}}, date(2026, 10, 18), date(2026, 10, 12))
        self.assertIn(date(2026, 10, 17), days)
        self.assertIn(date(2026, 10, 18), days)
        self.assertIn(date(2026, 10, 12), days)

    def test_preview_does_not_change_vault_or_state(self):
        engine = SyncEngine(self.config, collector=self.collect, generator=lambda *args: self.result())
        report = engine.sync_day(date(2026, 10, 18), preview=True)
        self.assertEqual('preview', report['status'])
        self.assertFalse((Path(self.config['vault']) / '01-每日记录/2026-10-18.md').exists())
        self.assertFalse(engine.state_path.exists())

    def test_only_malformed_records_is_failure_not_no_data(self):
        collector = lambda *args: {'events': [], 'stats': {'source_errors': 0, 'malformed_records': 2}, 'warnings': ['records malformed']}
        engine = SyncEngine(self.config, collector=collector, generator=lambda *args: self.result())
        with self.assertRaises(RuntimeError):
            engine.sync_day(date(2026, 10, 18))
        self.assertEqual('failed', engine.state['days']['2026-10-18']['status'])

    def test_verification_status_is_rendered_by_engine_not_required_from_model(self):
        result = self.result()
        result['topics'] = [{'path': '02-学习/Python/异常.md', 'markdown': '## 一个例子\nint("abc")会抛出ValueError。', 'evidence_ids': ['e1']}]
        validate_result(result, self.events)
        engine = SyncEngine(self.config, collector=self.collect, generator=lambda *a: result)
        engine.sync_day(date(2026, 10, 18))
        text = (Path(self.config['vault']) / '02-学习/Python/异常.md').read_text(encoding='utf-8')
        self.assertIn('没有个人验证记录时保持待验证', text)

    def test_negative_mastery_statement_is_not_misread_as_a_claim(self):
        result = self.result()
        result['topics'] = [{'path': '02-学习/Windows/缓存验证.md', 'markdown': '## 一个例子\n先核对缓存位置。\n## 我的验证\n没有用户独立操作或解释结果的个人验证记录，不能据此认定用户已经掌握。', 'evidence_ids': ['e1']}]
        validate_result(result, self.events)

    def test_nested_generated_verification_is_replaced_without_losing_knowledge(self):
        engine = SyncEngine(self.config, collector=self.collect)
        body = engine.topic_body('## 例子\n知识甲。\n### 我的验证\n自动生成的个人结论。\n#### 独立实践\n已独立接线并验证通过。\n## 来源\n保留来源。')
        self.assertNotIn('自动生成的个人结论', body)
        self.assertNotIn('已独立接线并验证通过', body)
        self.assertIn('知识甲', body)
        self.assertIn('保留来源', body)
        self.assertEqual(1, body.count('我的验证'))
        self.assertEqual(1, engine.topic_body('## 例子\n知识甲。\n### 我的验证').count('我的验证'))

    def test_model_aliases_roundtrip_and_unknown_evidence_is_rejected(self):
        builder = getattr(engine_module, 'model_payload', None)
        self.assertIsNotNone(builder, 'Compact model evidence protocol is not implemented')
        payload, aliases = builder(date(2026, 10, 18), self.events, [])
        alias = payload['events'][0]['id']
        result = self.result()
        result['items'][0]['evidence_ids'] = [alias]
        expanded = engine_module.expand_evidence(result, aliases)
        self.assertEqual(['e1'], expanded['items'][0]['evidence_ids'])
        result['items'][0]['evidence_ids'] = ['invented']
        with self.assertRaises(ValueError):
            engine_module.expand_evidence(result, aliases)

    def test_multichunk_reconciles_late_results_instead_of_concatenating(self):
        self.config['chunk_chars'] = 1
        self.events.append(dict(self.events[0], id='e2', kind='tool_result', role='tool',
                                evidence={'success': True, 'exit_code': 0}, text='后来的测试成功'))
        modes = []
        def generate(cfg, day, events, inventory):
            modes.append(cfg.get('reconcile', False))
            if cfg.get('reconcile'):
                r = self.result()
                r['items'][0].update(status='verified', text='最初尝试，后来实际测试成功。', evidence_ids=['e2'])
                return r
            r = self.result()
            r['items'][0]['evidence_ids'] = [events[0]['id']]
            return r
        engine = SyncEngine(self.config, collector=self.collect, generator=generate)
        result = engine.generate_chunks(date(2026, 10, 18), self.events, [])
        self.assertEqual([False, False, True], modes)
        self.assertEqual(1, len(result['items']))
        self.assertEqual('verified', result['items'][0]['status'])


if __name__ == '__main__':
    unittest.main()
