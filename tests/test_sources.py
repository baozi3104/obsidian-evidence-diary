import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

try:
    from obsidian_sync.sources import collect_events, redact_text
except ImportError:
    collect_events = redact_text = None


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(collect_events), 'collection API is not implemented')
        self.tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.claude = self.root / 'claude'
        self.claude.mkdir()
        self.config = {'projects': [{'name': 'foo', 'path': r'D:\work\foo'},
                                    {'name': 'nested', 'path': r'D:\work\foo\nested'}],
                       'sources': {'codex_home': str(self.home), 'claude_dirs': [str(self.claude)]}}

    def collect(self, **changes):
        self.config.update(changes)
        return collect_events(self.config, date(2026, 10, 18), date(2026, 10, 18))

    def jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf8')

    def codex(self, threads, items):
        c = sqlite3.connect(self.home / 'state_5.sqlite')
        c.execute('create table threads(id text primary key,cwd text,source text,history_mode text,rollout_path text)')
        c.executemany('insert into threads values(?,?,?,?,?)', threads)
        c.commit(); c.close()
        c = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        c.execute('create table thread_items(thread_id text,turn_id text,item_id text,rollout_ordinal integer,created_at_ms integer,item_json text,item_type text,updated_at_ordinal integer)')
        for i, (sid, ident, payload, stamp) in enumerate(items):
            c.execute('insert into thread_items values(?,?,?,?,?,?,?,?)',
                      (sid, 'turn', ident, i, stamp, json.dumps(payload), payload['type'], i))
        c.commit(); c.close()

    def test_redacts_secrets_without_removing_normal_token_discussion(self):
        text = 'token budget is 2400. password="hunter2"; api_key: sk-proj-1234567890abcdefghij; Authorization: Bearer abc.def.ghi'
        cleaned = redact_text(text)
        self.assertIn('token budget is 2400', cleaned)
        for secret in ('hunter2', 'sk-proj-1234567890abcdefghij', 'abc.def.ghi'):
            self.assertNotIn(secret, cleaned)

    def test_unseparated_chinese_credential_handoff_masks_all_candidates(self):
        text = '连接已准备，账号root：密码DummyPass321，SecondDummy@，ThirdDummy尝试一下'
        cleaned = redact_text(text)
        for value in ('DummyPass321', 'SecondDummy', 'ThirdDummy'):
            self.assertNotIn(value, cleaned)
        self.assertIn('连接已准备', cleaned)
        self.assertEqual('新增密码修改功能，讨论 token budget。', redact_text('新增密码修改功能，讨论 token budget。'))

    def test_generic_uppercase_secret_key_assignment_is_masked(self):
        self.assertNotIn('DUMMY-PRIVATE-VALUE', redact_text('SESSION_KEY=DUMMY-PRIVATE-VALUE'))

    def test_dialog_answer_to_credential_question_is_masked_even_without_label(self):
        text = '<send_user_message_question_reply>\n' + json.dumps([{'question': '请确认服务器的实际root密码，或提供可用SSH密钥。', 'answer': 'DUMMY-BARE-CREDENTIAL'}], ensure_ascii=False) + '\n</send_user_message_question_reply>'
        self.assertNotIn('DUMMY-BARE-CREDENTIAL', redact_text(text))
        text = '<send_user_message_question_reply>\n' + json.dumps([{'question': '请提供数据库密码。', 'answer': 'DUMMY-DB-CREDENTIAL'}], ensure_ascii=False) + '\n</send_user_message_question_reply>'
        self.assertNotIn('DUMMY-DB-CREDENTIAL', redact_text(text))

    def test_sqlite_uses_exact_root_deepest_project_and_local_day(self):
        rows = [('main', r'\\?\D:\work\foo\nested\src', 'vscode', 'paginated', ''),
                ('sibling', r'D:\work\foo-other', 'vscode', 'paginated', ''),
                ('child', r'D:\work\foo', '{"subagent":{"thread_spawn":{}}}', 'paginated', ''),
                ('unknown', '', 'vscode', 'paginated', ''),
                ('wsl', '/mnt/d/work/foo', 'vscode', 'paginated', '')]
        from datetime import datetime
        ms = lambda s: int(datetime.fromisoformat(s).timestamp() * 1000)
        msg = lambda t: {'type': 'userMessage', 'content': [{'type': 'text', 'text': t}]}
        self.codex(rows, [('main', 'a', msg('included'), ms('2026-10-17T16:00:00+00:00')),
                          ('main', 'before', msg('before'), ms('2026-10-17T15:59:59+00:00')),
                          ('main', 'after', msg('after'), ms('2026-10-18T16:00:00+00:00')),
                          ('sibling', 'b', msg('wrong'), ms('2026-10-18T00:00:00+00:00')),
                          ('child', 'c', msg('inherited'), ms('2026-10-18T00:00:00+00:00')),
                          ('unknown', 'd', msg('unknown'), ms('2026-10-18T00:00:00+00:00')),
                          ('wsl', 'e', msg('wsl included'), ms('2026-10-18T00:00:00+00:00'))])
        result = self.collect()
        self.assertEqual({e['text'] for e in result['events']}, {'included', 'wsl included'})
        self.assertEqual({e['project'] for e in result['events']}, {'foo', 'nested'})
        self.assertTrue(all(e['timestamp'].endswith('+08:00') for e in result['events']))

    def test_tool_evidence_distinguishes_actual_failure_from_assistant_claim(self):
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'claim', {'type': 'agentMessage', 'text': 'All tests pass.'}, 1792281600000),
            ('main', 'bad', {'type': 'commandExecution', 'command': 'run tests', 'status': 'completed', 'exitCode': 2, 'aggregatedOutput': '2 failed'}, 1792281600001),
            ('main', 'good', {'type': 'commandExecution', 'command': 'syntax check', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'ok'}, 1792281600002)])
        result = self.collect()
        evidence = {e['evidence']['call_id']: e['evidence'] for e in result['events'] if e['kind'] == 'tool_result'}
        self.assertIs(evidence['bad']['success'], False)
        self.assertIs(evidence['good']['success'], True)
        claim = next(e for e in result['events'] if e['kind'] == 'message')
        self.assertNotIn('success', claim['evidence'])

    def test_sensitive_tool_output_is_excluded_via_call_id(self):
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'secret', {'type': 'commandExecution', 'command': 'Get-Content D:\\工作资料\\私密凭据\\password.txt', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'PRIVATE-RAW-VALUE'}, 1792281600000),
            ('main', 'normal', {'type': 'agentMessage', 'text': 'Set token budget to 2400.'}, 1792281600001)])
        result = self.collect()
        self.assertEqual(len(result['events']), 1)
        self.assertNotIn('PRIVATE-RAW-VALUE', json.dumps(result))
        self.assertGreater(result['stats']['excluded_by_reason'].get('sensitive_tool', 0), 0)

    def test_legacy_rollout_associates_tool_result_and_deduplicates(self):
        rows = [{'type': 'session_meta', 'payload': {'id': 'legacy', 'cwd': r'D:\work\foo', 'source': 'cli'}},
                {'timestamp': '2026-10-18T02:00:00Z', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'hello'}},
                {'timestamp': '2026-10-18T02:00:00Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}},
                {'timestamp': '2026-10-18T02:00:01Z', 'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'c1', 'name': 'exec_command', 'arguments': '{"cmd":"test"}'}},
                {'timestamp': '2026-10-18T02:00:02Z', 'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'c1', 'output': '{"exit_code":3,"output":"failed"}'}}]
        self.jsonl(self.home / 'sessions' / 'old.jsonl', rows)
        first, second = self.collect(), self.collect()
        self.assertEqual(len(first['events']), 3)
        self.assertEqual([e['id'] for e in first['events']], [e['id'] for e in second['events']])
        output = next(e for e in first['events'] if e['kind'] == 'tool_result')
        self.assertEqual(output['evidence']['exit_code'], 3)
        self.assertIs(output['evidence']['success'], False)
        self.assertEqual(output['evidence']['tool'], 'exec_command')

    def test_multiple_session_meta_excludes_inherited_subagent_context(self):
        self.jsonl(self.home / 'archived_sessions' / 'child.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'parent', 'cwd': r'D:\work\foo', 'source': 'cli'}},
            {'type': 'session_meta', 'payload': {'id': 'child', 'cwd': r'D:\work\foo', 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'parent'}}}}},
            {'type': 'response_item', 'timestamp': '2026-10-18T00:00:00Z', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'inherited'}]}}])
        self.assertEqual(self.collect()['events'], [])

    def test_noise_and_summary_sessions_are_excluded_and_truncation_counted(self):
        self.config['sources']['exclude_session_ids'] = ['summary']
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', ''), ('summary', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'noise', {'type': 'userMessage', 'content': [{'type': 'text', 'text': '# AGENTS.md instructions\n<INSTRUCTIONS>system prompt'}]}, 1792281600000),
            ('main', 'long', {'type': 'agentMessage', 'text': 'ordinary technical discussion about the project. ' * 8}, 1792281600001),
            ('summary', 'sum', {'type': 'agentMessage', 'text': 'daily generated summary'}, 1792281600002)])
        result = self.collect(max_event_chars=30)
        self.assertEqual(len(result['events']), 1)
        self.assertLessEqual(len(result['events'][0]['text']), 30)
        self.assertTrue(result['events'][0]['text_truncated'])
        self.assertEqual(result['stats']['truncated_events'], 1)

    def test_generic_token_assignments_are_masked_but_token_usage_is_preserved(self):
        result = redact_text('token budget 2400; token=abc123; "TOKEN": "sensitive-short"; access_token=other')
        self.assertIn('token budget 2400', result)
        for secret in ('abc123', 'sensitive-short', 'other'):
            self.assertNotIn(secret, result)

    def test_paginated_rollout_fallback_does_not_repeat_response_items(self):
        self.jsonl(self.home / 'sessions' / 'modern.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'modern', 'cwd': r'D:\work\foo', 'source': 'cli', 'history_mode': 'paginated'}},
            {'type': 'event_msg', 'timestamp': '2026-10-18T00:00:00Z', 'payload': {'type': 'item_completed', 'item': {'type': 'agentMessage', 'id': 'modern-message', 'text': 'Actual answer'}}},
            {'type': 'response_item', 'timestamp': '2026-10-18T00:00:00Z', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Actual answer'}]}}])
        self.assertEqual(len(self.collect()['events']), 1)

    def test_mixed_rollout_preserves_legacy_messages_before_modern_items(self):
        self.jsonl(self.home / 'archived_sessions' / 'mixed.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'mixed', 'cwd': r'D:\work\foo', 'source': 'cli'}},
            {'type': 'response_item', 'timestamp': '2026-10-18T00:00:00Z', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'older request'}]}},
            {'type': 'event_msg', 'timestamp': '2026-10-18T01:00:00Z', 'payload': {'type': 'item_completed', 'item': {'type': 'agentMessage', 'id': 'new', 'text': 'new answer'}}},
            {'type': 'response_item', 'timestamp': '2026-10-18T01:00:00Z', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'new answer'}]}}])
        self.assertEqual({e['text'] for e in self.collect()['events']}, {'older request', 'new answer'})

    def test_claude_error_flag_is_not_cleared_by_unrelated_tool_metadata(self):
        self.jsonl(self.claude / 'x' / 'error.jsonl', [
            {'type': 'user', 'uuid': 'failure', 'sessionId': 'cl', 'cwd': r'D:\work\foo', 'timestamp': '2026-10-18T03:00:00Z',
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'content': 'failed', 'is_error': True}]}, 'toolUseResult': {'stdout': 'failed'}}])
        self.assertIs(self.collect()['events'][0]['evidence']['success'], False)

    def test_malformed_counter_counts_records_once_despite_discovery_passes(self):
        self.jsonl(self.home / 'sessions' / 'bad.jsonl', [{'type': 'session_meta', 'payload': {'id': 'bad', 'cwd': r'D:\work\foo'}}])
        with (self.home / 'sessions' / 'bad.jsonl').open('a') as f:
            f.write('BROKEN\n')
        result = self.collect()
        self.assertEqual(result['stats']['malformed_records'], 1)
        self.assertEqual(result['stats']['files_seen'], 1)

    def test_outside_rollout_is_rejected_after_first_metadata(self):
        self.jsonl(self.home / 'sessions' / 'outside.jsonl', [{'type': 'session_meta', 'payload': {'id': 'outside', 'cwd': r'C:\outside'}}])
        with (self.home / 'sessions' / 'outside.jsonl').open('a') as f:
            f.write('OUTSIDE BODY MUST NOT BE PARSED\n')
        self.assertEqual(self.collect()['stats']['malformed_records'], 0)

    def test_file_change_preserves_path_and_diff_as_tool_evidence(self):
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'edit', {'type': 'fileChange', 'status': 'completed', 'changes': [{'path': r'D:\work\foo\app.py', 'kind': 'update', 'diff': '+fixed line'}]}, 1792281600000)])
        result = self.collect()
        output = next((e for e in result['events'] if e['kind'] == 'tool_result'), None)
        self.assertIsNotNone(output)
        self.assertIn('app.py', output['text'])
        self.assertIn('+fixed line', output['text'])
        self.assertIs(output['evidence']['success'], True)

    def test_credentials_file_and_environment_dump_outputs_are_not_collected(self):
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'claude-auth', {'type': 'commandExecution', 'command': 'type C:\\Users\\x\\.claude\\.credentials.json', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'PRIVATE-ONE'}, 1792281600000),
            ('main', 'env', {'type': 'commandExecution', 'command': 'Get-ChildItem Env:', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'PRIVATE-TWO'}, 1792281600001)])
        self.assertEqual(self.collect()['events'], [])

    def test_sensitive_relative_basenames_exclude_whole_command_results(self):
        commands = ['Get-Content .env', 'type secrets.json', 'cat ".credentials.json"',
                    "Get-Content -LiteralPath 'auth.json'", 'Get-Content credentials',
                    'Get-Content ".env.production"', 'type credentials.json',
                    'python -c "open(\'.env\').read()"']
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'sensitive-' + str(i), {'type': 'commandExecution', 'command': command,
                                           'status': 'completed', 'exitCode': 0,
                                           'aggregatedOutput': 'SESSION_KEY=FAKE-PRIVATE-VALUE'}, 1792281600000 + i)
            for i, command in enumerate(commands)])
        result = self.collect()
        self.assertEqual(result['events'], [])
        self.assertNotIn('FAKE-PRIVATE-VALUE', json.dumps(result))
        self.assertEqual(result['stats']['excluded_by_reason']['sensitive_tool'], 2 * len(commands))

    def test_json_relative_sensitive_path_blocks_mcp_call_and_result(self):
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'relative-json', {'type': 'mcpToolCall', 'tool': 'read_file',
                                      'arguments': {'file_path': '.env'}, 'status': 'completed',
                                      'result': {'content': [{'type': 'text', 'text': 'SESSION_KEY=FAKE-PRIVATE-VALUE'}]}}, 1792281600000)])
        self.assertEqual(self.collect()['events'], [])

    def test_claude_relative_sensitive_path_blocks_out_of_order_result_by_call_id(self):
        base = {'sessionId': 'cl', 'timestamp': '2026-10-18T03:00:00Z', 'cwd': r'D:\work\foo'}
        self.jsonl(self.claude / 'x' / 'relative-secret.jsonl', [
            dict(base, type='user', uuid='result', message={'content': [{'type': 'tool_result', 'tool_use_id': 'relative-secret', 'content': 'SESSION_KEY=FAKE-PRIVATE-VALUE'}]}),
            dict(base, type='assistant', uuid='call', message={'content': [{'type': 'tool_use', 'id': 'relative-secret', 'name': 'Read', 'input': {'file_path': '.env'}}]})])
        self.assertEqual(self.collect()['events'], [])

    def test_sensitive_basename_prefix_does_not_match_longer_normal_filenames(self):
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'normal', {'type': 'commandExecution', 'command': 'Get-Content mysecrets.json; Get-Content .environment; Get-Content auth.jsonschema',
                               'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'ordinary project output'}, 1792281600000)])
        self.assertEqual(len(self.collect()['events']), 2)

    def test_chinese_password_colon_and_command_password_flag_are_masked(self):
        text = redact_text('密码：secret123; command --password flagsecret; token usage is normal')
        self.assertNotIn('secret123', text)
        self.assertNotIn('flagsecret', text)
        self.assertIn('token usage is normal', text)

    def test_unstructured_success_claim_in_tool_output_remains_unknown(self):
        self.jsonl(self.home / 'sessions' / 'unknown-result.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'unknown-result', 'cwd': r'D:\work\foo'}},
            {'type': 'response_item', 'timestamp': '2026-10-18T03:00:00Z', 'payload': {'type': 'function_call', 'call_id': 'c', 'name': 'some_tool', 'arguments': '{}'}},
            {'type': 'response_item', 'timestamp': '2026-10-18T03:00:01Z', 'payload': {'type': 'function_call_output', 'call_id': 'c', 'output': 'All tasks completed successfully.'}}])
        result = self.collect()
        output = next(e for e in result['events'] if e['kind'] == 'tool_result')
        self.assertIsNone(output['evidence']['success'])

    def assert_nested_error_wins(self, result):
        self.codex([('main', r'D:\work\foo', 'vscode', 'paginated', '')], [
            ('main', 'mcp', {'type': 'mcpToolCall', 'tool': 'test_tool', 'arguments': {}, 'status': 'completed', 'result': result}, 1792281600000)])
        output = next(e for e in self.collect()['events'] if e['kind'] == 'tool_result')
        self.assertIs(output['evidence']['success'], False)

    def test_explicit_error_overrides_nested_metadata_success(self):
        self.assert_nested_error_wins({'isError': True, 'metadata': {'success': True}})

    def test_explicit_error_overrides_nested_structured_zero_exit(self):
        self.assert_nested_error_wins({'isError': True, 'structuredContent': {'exit_code': 0}})

    def test_explicit_snake_error_overrides_nested_result_zero_exit(self):
        self.assert_nested_error_wins({'is_error': True, 'result': {'exitCode': 0}})

    def test_claude_uses_record_cwd_not_directory_name_and_retains_tool_results(self):
        base = {'sessionId': 'claude-main', 'timestamp': '2026-10-18T03:00:00Z', 'cwd': '/mnt/d/work/foo'}
        self.jsonl(self.claude / 'misleading-directory' / 'session.jsonl', [
            dict(base, type='assistant', uuid='a', message={'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tool-1', 'name': 'Bash', 'input': {'command': 'test'}}]}),
            dict(base, type='user', uuid='b', message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'failed', 'is_error': True}]}),
            dict(base, type='user', uuid='c', cwd=r'D:\work\foo-other', message={'role': 'user', 'content': 'wrong project'}),
            dict(base, type='user', uuid='side', isSidechain=True, message={'role': 'user', 'content': 'sidechain'}),
            dict(base, type='user', uuid='d', message={'role': 'user', 'content': 'normal request'})])
        result = self.collect()
        self.assertEqual(len(result['events']), 3)
        output = next(e for e in result['events'] if e['kind'] == 'tool_result')
        self.assertIs(output['evidence']['success'], False)
        self.assertEqual(output['evidence']['tool'], 'Bash')

    def test_claude_sensitive_call_result_and_unknown_cwd_are_excluded(self):
        base = {'sessionId': 'cl', 'timestamp': '2026-10-18T03:00:00Z', 'cwd': r'D:\work\foo'}
        self.jsonl(self.claude / 'x' / 'a.jsonl', [
            dict(base, type='assistant', uuid='a', message={'content': [{'type': 'tool_use', 'id': 'secret', 'name': 'Read', 'input': {'file_path': 'C:\\Users\\x\\.codex\\auth.json'}}]}),
            dict(base, type='user', uuid='b', message={'content': [{'type': 'tool_result', 'tool_use_id': 'secret', 'content': 'SUPER-PRIVATE'}]})])
        self.jsonl(self.claude / 'x' / 'unknown.jsonl', [{'type': 'user', 'uuid': 'u', 'sessionId': 'unknown', 'timestamp': '2026-10-18T03:00:00Z', 'message': {'content': 'unknown cwd'}}])
        self.assertEqual(self.collect()['events'], [])

    def test_hermes_requires_cwd_excludes_cron_and_compressed_summary(self):
        p = self.root / 'hermes.db'
        self.config['sources']['hermes_db'] = str(p)
        c = sqlite3.connect(p)
        c.execute('create table sessions(id text, source text,cwd text,git_repo_root text,origin_json text,parent_session_id text)')
        c.execute('create table messages(id integer,session_id text,role text,content text,tool_call_id text,tool_calls text,tool_name text,timestamp real,_compressed_summary integer,active integer)')
        c.executemany('insert into sessions values(?,?,?,?,?,?)', [('good','cli','/mnt/d/work/foo',None,'{}',None), ('cron','cron','/mnt/d/work/foo',None,'{}',None),('unknown','cli',None,'/mnt/d/work/foo','{}',None)])
        c.executemany('insert into messages values(?,?,?,?,?,?,?,?,?,?)', [(1,'good','user','normal request',None,None,None,1792281600,0,1), (2,'good','assistant','compressed',None,None,None,1792281601,1,1), (3,'cron','user','cron job',None,None,None,1792281602,0,1),(4,'unknown','user','guess from repo',None,None,None,1792281603,0,1)])
        c.commit(); c.close()
        result = self.collect()
        self.assertEqual([(e['source'], e['text']) for e in result['events']], [('hermes', 'normal request')])

    def test_invalid_json_and_missing_source_are_reported_without_private_content(self):
        self.jsonl(self.home / 'sessions' / 'valid.jsonl', [{'type': 'session_meta', 'payload': {'id': 'x', 'cwd': r'D:\work\foo'}}])
        with (self.home / 'sessions' / 'valid.jsonl').open('a') as f:
            f.write('PRIVATE BROKEN JSON\n')
        self.config['sources']['hermes_db'] = str(self.root / 'missing.db')
        result = self.collect()
        self.assertGreater(result['stats'].get('malformed_records', 0), 0)
        self.assertTrue(result['warnings'])
        self.assertNotIn('PRIVATE BROKEN JSON', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
