"""Read-only, project-scoped collectors. No database or source file is modified.

Optional source settings: exclude_session_ids, exclude_text_patterns,
hermes_wsl_fallback (default True), hermes_wsl_timeout (default 30 seconds).
Unknown cwd and unknown tool success remain unknown rather than being guessed.
"""
from __future__ import annotations

import collections
import json
import ntpath
import re
import sqlite3
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote


LOCAL = timezone(timedelta(hours=8))
_PRIVATE = re.compile(
    r'99[-_ ]?私密|工作资料|私密凭据|账号配置|(?:^|[\\/\s"\'=:,;(\[{])'
    r'(?:auth\.json|\.?credentials(?:\.json)?|oauth_creds\.json|secrets\.json|id_rsa|id_ed25519|\.env(?:\.[\w-]+)*)'
    r'(?=$|[\\/\s"\'=:,;.)\]}])'
    r'|ProtectedData|\bDPAPI\b|\bUnprotect\b|ConvertTo-SecureString|Get-StoredCredential|\bcmdkey\b'
    r'|Get-(?:ChildItem|Item|Content)\s+(?:-Path\s+)?[\"\']?Env:'
    r'|(?:print|pprint)\s*\(\s*(?:dict\s*\(\s*)?os\.environ|(?:^|[;\n])\s*(?:printenv|env)\s*(?:$|[;\n])'
    r'|(?:decrypt|解密).{0,35}(?:password|credential|token|密码|凭据)', re.I)
_ASSIGN = re.compile(
    r'((?:["\']?)(?:password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|activation[_-]?token|token|secret|client[_-]?secret|密码|口令|密钥|激活\s*token)(?:["\']?)\s*(?:[:：=]|是|为)\s*)'
    r'(["\'])(.*?)(\2)', re.I)
_PLAIN_ASSIGN = re.compile(
    r'((?:password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|activation[_-]?token|token|client[_-]?secret|secret|密码|口令|密钥|激活\s*token)\s*(?:[:：=]|是|为)\s*)([^\s;,}\]\"\']+)', re.I)


def redact_text(text: str) -> str:
    """Remove common credential values, preserving ordinary discussion of tokens."""
    text = str(text or '')
    # Native question replies can contain a bare password only in `answer`.
    if '<send_user_message_question_reply>' in text:
        match = re.search(r'(<send_user_message_question_reply>\s*)(\[.*\])(\s*</send_user_message_question_reply>)', text, re.S)
        if match:
            try:
                replies = json.loads(match[2])
                for reply in replies:
                    question = str(reply.get('question', ''))
                    if re.search(r'(?:提供|确认|实际|正确|告诉|发给).{0,45}(?:密码|口令|密钥|token|凭据)|(?:密码|口令|密钥).{0,20}(?:是什么|多少|内容|提供)', question, re.I):
                        reply['answer'] = '[REDACTED CREDENTIAL RESPONSE]'
                text = text[:match.start(2)] + json.dumps(replies, ensure_ascii=False) + text[match.end(2):]
            except (ValueError, TypeError, AttributeError):
                if re.search(r'密码|口令|密钥|凭据', match[2]):
                    text = text[:match.start(2)] + '[REDACTED CREDENTIAL RESPONSE]' + text[match.end(2):]
    # Oral handoffs often omit punctuation and provide several password candidates.
    # Mask the rest of that line, rather than retaining later comma-separated secrets.
    text = re.sub(r'((?:账号|账户|用户名|root)[^\r\n]{0,80}?(?:密码|口令))[^\r\n]*',
                  r'\1"[REDACTED]"', text, flags=re.I)
    text = re.sub(r'((?:密码|口令|密钥)\s*(?:[:：=]|是|为)?\s*)["\']?[A-Za-z0-9@#$%!^&*+_.=/:?~-]{3,}["\']?'
                  r'(?:\s*[,，、]\s*["\']?[A-Za-z0-9@#$%!^&*+_.=/:?~-]{3,}["\']?)*',
                  r'\1"[REDACTED]"', text)
    text = re.sub(r'(\b[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH)\b\s*[:=]\s*)(?:"[^"]*"|\'[^\']*\'|[^\s;,}\]]+)',
                  r'\1"[REDACTED]"', text)
    text = re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED PRIVATE KEY]', text)
    # Keep delimiters so logs and JSON remain readable after masking.
    text = _ASSIGN.sub(lambda m: m.group(1) + m.group(2) + '[REDACTED]' + m.group(4), text)
    text = _PLAIN_ASSIGN.sub(lambda m: m.group(1) + '[REDACTED]', text)
    text = re.sub(r'(?i)(--(?:password|passwd|api-key|token|secret)\s+)(?:"[^"]*"|\'[^\']*\'|[^\s;]+)', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;\"\']+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,}|github_pat_[A-Za-z0-9_]{15,}|AKIA[A-Z0-9]{16})\b', '[REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED JWT]', text)
    text = re.sub(r'(?<![\w/\\-])[A-Za-z0-9_+/=-]{48,}(?![\w/\\-])', '[REDACTED LONG VALUE]', text)
    text = re.sub(r'(?i)(://[^\s/:]+:)[^\s/@]+(@)', r'\1[REDACTED]\2', text)
    return text


def _json(value, default=None):
    if not isinstance(value, str):
        return value if value is not None else default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _text(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and ('text' in item or 'content' in item):
                parts.append(_text(item.get('text', item.get('content'))))
            elif isinstance(item, dict) and item.get('type') in ('image', 'input_image', 'output_image', 'image_url'):
                parts.append('[image omitted]')
            else:
                parts.append(_text(item))
        return '\n'.join(parts)
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _arguments(value):
    return _text(_json(value, value))


def _norm(path):
    path = str(path or '').strip().replace('/', '\\')
    # Some imported metadata contains doubled separators, including \\?\\.
    path = re.sub(r'\\+', r'\\', path)
    if path.startswith('\\?\\'):
        path = path[3:]
    if path.lower().startswith('unc\\'):
        path = '\\\\' + path[4:]
    match = re.match(r'^\\mnt\\([a-z])(?:\\|$)(.*)', path, re.I)
    if match:
        path = match[1] + ':\\' + match[2]
    if not re.match(r'^[a-z]:\\', path, re.I):
        return ''
    return ntpath.normpath(path).rstrip('\\').casefold()


def _timestamp(value):
    try:
        if isinstance(value, (int, float)):
            if value > 100_000_000_000:
                value /= 1000
            return datetime.fromtimestamp(value, timezone.utc).astimezone(LOCAL)
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            # A naive timestamp has no trustworthy timezone.
            return stamp.astimezone(LOCAL) if stamp.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _background(value):
    text = _text(value).lower()
    return any(s in text for s in ('subagent', 'sub_agent', 'thread_spawn', 'cron', 'background', 'automation'))


def _evidence(value, status=None):
    """Use structured fields or the tool framework's exit-code line, never prose."""
    result = {'success': None}
    if status:
        result['status'] = str(status)
    obj = _json(value, value)
    candidates = [obj]
    failed = False
    succeeded = False
    visited = set()
    for part in candidates:
        if not isinstance(part, dict) or id(part) in visited:
            continue
        visited.add(id(part))
        candidates.extend(part.get(k) for k in ('result', 'structuredContent', 'metadata'))
        for key in ('exit_code', 'exitCode'):
            if isinstance(part.get(key), int) and not isinstance(part[key], bool):
                code = part[key]
                if 'exit_code' not in result or result['exit_code'] == 0:
                    result['exit_code'] = code
                failed = failed or code != 0
                succeeded = succeeded or code == 0
        if part.get('isError') is True or part.get('is_error') is True:
            failed = True
        if part.get('success') is False or part.get('error'):
            failed = True
        if part.get('success') is True:
            succeeded = True
        if str(part.get('status', '')).lower() in ('failed', 'error', 'cancelled', 'canceled', 'declined'):
            failed = True
    if 'exit_code' not in result and isinstance(value, str):
        match = re.search(r'(?m)^Process exited with code (-?\d+)\s*$', value)
        if match:
            code = int(match[1])
            result['exit_code'] = code
            failed = failed or code != 0
            succeeded = succeeded or code == 0
    if str(status).lower() in ('failed', 'error', 'cancelled', 'canceled', 'declined'):
        failed = True
    result['success'] = False if failed else True if succeeded else None
    return result


def _connect(path):
    # Four initial slashes allow Windows UNC paths without an SQLite URI authority.
    uri = 'file:' + quote(str(Path(path).absolute()).replace('\\', '/'), safe='/:') + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _columns(conn, table):
    return {r['name'] for r in conn.execute('PRAGMA table_info(' + table + ')')}


class _Collector:
    def __init__(self, config, start_date, end_date):
        self.config = config
        self.sources = config.get('sources', {})
        self.projects = sorted(((_norm(p['path']), p['name']) for p in config.get('projects', [])), key=lambda p: len(p[0]), reverse=True)
        self.start = datetime.combine(start_date, time.min, LOCAL)
        self.end = datetime.combine(end_date + timedelta(days=1), time.min, LOCAL)
        self.limit = max(1, int(config.get('max_event_chars', 2400)))
        self.exclude_ids = set(self.sources.get('exclude_session_ids', []))
        self.exclude_patterns = [re.compile(p, re.I) for p in self.sources.get('exclude_text_patterns', [])]
        self.events = []
        self.ids = set()
        self.seen_files = set()
        self.malformed = set()
        self.warnings = []
        self.stats = {'events_total': 0, 'by_source': {}, 'truncated_events': 0, 'redacted_events': 0,
                      'malformed_records': 0, 'source_errors': 0, 'files_seen': 0,
                      'sessions_seen': 0, 'sessions_selected': 0, 'excluded_by_reason': {}}

    def exclude(self, reason, n=1):
        d = self.stats['excluded_by_reason']
        d[reason] = d.get(reason, 0) + n

    def warn(self, source, reason, error=None):
        self.stats['source_errors'] += 1
        # Exception messages may contain paths, SQL, or private content.
        note = source + ': ' + reason + (f' ({type(error).__name__})' if error else '')
        if note not in self.warnings:
            self.warnings.append(note)

    def project(self, cwd):
        normal = _norm(cwd)
        if not normal or _PRIVATE.search(normal):
            return None
        for path, name in self.projects:
            if path and (normal == path or normal.startswith(path + '\\')):
                return name
        return None

    def session(self, sid, cwd, origin=None):
        self.stats['sessions_seen'] += 1
        if sid in self.exclude_ids:
            self.exclude('excluded_session'); return None
        if _background(origin):
            self.exclude('background_or_subagent'); return None
        project = self.project(cwd)
        if not project:
            self.exclude('unknown_or_outside_project'); return None
        self.stats['sessions_selected'] += 1
        return project

    def add(self, source, sid, project, ident, stamp, role, kind, text, evidence=None):
        ts = _timestamp(stamp)
        if not ts:
            self.exclude('missing_or_invalid_timestamp'); return
        if not self.start <= ts < self.end:
            self.exclude('outside_date_range'); return
        if not project or sid in self.exclude_ids:
            self.exclude('unknown_or_excluded_session'); return
        text = _text(text).strip()
        if not text:
            self.exclude('empty_text'); return
        if kind == 'message' and self.noise(text):
            self.exclude('injected_or_summary_noise'); return
        stable = source + ':' + str(sid) + ':' + str(ident)
        if stable in self.ids:
            self.exclude('duplicate_event'); return
        cleaned = redact_text(text)
        if cleaned != text:
            self.stats['redacted_events'] += 1
        truncated = len(cleaned) > self.limit
        if truncated:
            cleaned = cleaned[:self.limit]
            self.stats['truncated_events'] += 1
        self.events.append({'id': stable, 'source': source, 'session_id': str(sid),
                            'project': project, 'timestamp': ts.isoformat(), 'role': role,
                            'kind': kind, 'text': cleaned, 'evidence': evidence or {},
                            'text_truncated': truncated})
        self.ids.add(stable)

    def noise(self, text):
        if re.match(r'(?is)^\s*(?:#\s*AGENTS\.md instructions|<INSTRUCTIONS>|<environment_context>|<system-reminder>|<local-command|<command-name>|<task-notification>|<request[_-]card>|You are (?:Codex|an AI assistant)|#\s*Tools\b|##\s*Skills\b|\[Request interrupted)', text):
            return True
        if re.match(r'(?is)^\s*(?:This session is being continued from a previous conversation|The conversation so far|You are performing a context checkpoint|Write a concise handoff summary)', text):
            return True
        return any(pattern.search(text) for pattern in self.exclude_patterns)

    def rows(self, path):
        file_key = str(path).casefold()
        if file_key not in self.seen_files:
            self.stats['files_seen'] += 1
            self.seen_files.add(file_key)
        try:
            with path.open(encoding='utf-8-sig') as stream:
                for ordinal, line in enumerate(stream):
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError()
                    except (ValueError, TypeError):
                        if (file_key, ordinal) not in self.malformed:
                            self.stats['malformed_records'] += 1
                            self.malformed.add((file_key, ordinal))
                        continue
                    yield ordinal, row
        except (OSError, UnicodeError) as error:
            self.warn('jsonl', 'source file could not be read', error)

    def codex(self):
        home = self.sources.get('codex_home')
        if not home:
            return
        home = Path(home)
        if not home.exists():
            self.warn('codex', 'configured source is missing'); return
        handled = set()
        known_paths = set()
        state_path = home / 'state_5.sqlite'
        hist_path = home / 'thread_history_1.sqlite'
        history = None
        try:
            if hist_path.exists():
                history = _connect(hist_path)
                columns = _columns(history, 'thread_items')
                if not {'thread_id', 'item_id', 'item_json', 'created_at_ms'} <= columns:
                    self.warn('codex', 'unsupported history schema'); history.close(); history = None
            if state_path.exists():
                state = _connect(state_path)
                try:
                    columns = _columns(state, 'threads')
                    if not {'id', 'cwd', 'source', 'rollout_path'} <= columns:
                        self.warn('codex', 'unsupported thread schema')
                    else:
                        # Do not fetch first_user_message, title, instructions, or previews.
                        query = 'SELECT id,cwd,source,rollout_path' + (',history_mode' if 'history_mode' in columns else '') + ' FROM threads'
                        for row in state.execute(query):
                            sid = row['id']
                            path = Path(row['rollout_path']) if row['rollout_path'] else None
                            if path:
                                known_paths.add(str(path).casefold())
                            project = self.session(sid, row['cwd'], row['source'])
                            if not project:
                                handled.add(sid); continue
                            n = 0
                            mode = row['history_mode'] if 'history_mode' in columns else 'legacy'
                            if history and mode == 'paginated':
                                order = 'rollout_ordinal' if 'rollout_ordinal' in _columns(history, 'thread_items') else 'created_at_ms'
                                for item in history.execute('SELECT item_id,item_json,created_at_ms FROM thread_items WHERE thread_id=? ORDER BY ' + order, (sid,)):
                                    value = _json(item['item_json'])
                                    if not isinstance(value, dict):
                                        self.stats['malformed_records'] += 1; continue
                                    self.codex_item(sid, project, item['item_id'], item['created_at_ms'], value)
                                    n += 1
                            if not n and path and path.exists():
                                self.codex_rollout(path, sid)
                            elif not n and mode == 'paginated':
                                self.warn('codex', 'selected thread has no readable history')
                            handled.add(sid)
                finally:
                    state.close()
        except (sqlite3.Error, OSError) as error:
            self.warn('codex', 'database could not be read completely', error)
        finally:
            if history:
                history.close()
        # Orphan/archived/older rollouts are discovered by their own first metadata.
        for directory in (home / 'sessions', home / 'archived_sessions'):
            if not directory.exists():
                continue
            for path in directory.rglob('*.jsonl'):
                if str(path).casefold() not in known_paths:
                    self.codex_rollout(path, handled=handled)

    def codex_item(self, sid, project, ident, stamp, item):
        typ = item.get('type')
        if typ == 'userMessage':
            self.add('codex', sid, project, ident, stamp, 'user', 'message', item.get('content'))
        elif typ == 'agentMessage':
            self.add('codex', sid, project, ident, stamp, 'assistant', 'message', item.get('text'))
        elif typ in ('commandExecution', 'mcpToolCall', 'fileChange', 'webSearch'):
            tool = item.get('tool') or typ
            args = item.get('command') if typ == 'commandExecution' else item.get('arguments', item.get('changes', item.get('query')))
            args = _arguments(args)
            if _PRIVATE.search(args):
                self.exclude('sensitive_tool', 2); return
            ev = {'call_id': str(ident), 'tool': str(tool)}
            self.add('codex', sid, project, str(ident) + ':call', stamp, 'assistant', 'tool_call', str(tool) + '\n' + args, ev)
            status = item.get('status')
            if typ == 'commandExecution':
                output = item.get('aggregatedOutput', '')
                evidence = _evidence({'exitCode': item.get('exitCode')}, status)
            elif typ == 'mcpToolCall':
                output = item.get('result') or item.get('error')
                evidence = _evidence(output, status)
                if item.get('error'):
                    evidence['success'] = False
                elif status == 'completed' and output is not None and evidence['success'] is None:
                    evidence['success'] = True
            else:
                output = item.get('changes') if typ == 'fileChange' else item.get('results')
                evidence = _evidence(output, status)
                if typ == 'fileChange' and status == 'completed':
                    evidence['success'] = True
            if output is not None or status:
                evidence.update(ev)
                self.add('codex', sid, project, str(ident) + ':result', stamp, 'tool', 'tool_result', output or ('status: ' + str(status)), evidence)
        else:
            self.exclude('unsupported_or_internal_item')

    def codex_rollout(self, path, expected=None, handled=None):
        # Inspect metadata first; a child rollout may begin with its parent's metadata.
        metas = []
        for _, row in self.rows(path):
            if row.get('type') == 'session_meta' and isinstance(row.get('payload'), dict):
                first = row['payload']
                sid = str(first.get('id') or first.get('session_id'))
                if sid in self.exclude_ids or (handled and sid in handled):
                    self.exclude('excluded_session'); return
                if _background(first.get('source')):
                    self.exclude('background_or_subagent'); return
                if not self.project(first.get('cwd')):
                    self.exclude('unknown_or_outside_project'); return
                break
        else:
            self.exclude('missing_session_metadata'); return
        # Only a whitelisted initial context permits reading the remaining records.
        for _, row in self.rows(path):
            if row.get('type') == 'session_meta' and isinstance(row.get('payload'), dict):
                metas.append(row['payload'])
        if not metas:
            self.exclude('missing_session_metadata'); return
        if any(_background(m.get('source')) for m in metas):
            self.exclude('background_or_subagent'); return
        ids = {str(m.get('id') or m.get('session_id')) for m in metas}
        if len(ids) != 1:
            self.exclude('ambiguous_inherited_context'); return
        meta = metas[0]
        sid = str(meta.get('id') or meta.get('session_id'))
        if (expected and sid != expected) or (handled and sid in handled):
            self.exclude('duplicate_or_mismatched_session'); return
        project = self.session(sid, meta.get('cwd'), meta.get('source'))
        if not project:
            return
        calls = {}
        blocked = set()
        # First pass call linkage ensures even out-of-order sensitive outputs are blocked.
        response_messages = {}
        completed_messages = {}
        completed_ids = set()
        def index_message(index, role, text, stamp):
            index.setdefault((role, _text(text).strip()), []).append(_timestamp(stamp))
        def matches_message(index, role, text, stamp):
            when = _timestamp(stamp)
            return when is not None and any(t is not None and abs((when - t).total_seconds()) <= 2
                                           for t in index.get((role, _text(text).strip()), []))
        for _, row in self.rows(path):
            p = row.get('payload', {})
            if not isinstance(p, dict):
                continue
            if row.get('type') == 'event_msg' and p.get('type') == 'item_completed' and isinstance(p.get('item'), dict):
                item = p['item']
                completed_ids.add(item.get('id'))
                if item.get('type') in ('userMessage', 'agentMessage'):
                    role = 'user' if item['type'] == 'userMessage' else 'assistant'
                    index_message(completed_messages, role, item.get('content') if role == 'user' else item.get('text'), p.get('completed_at_ms', row.get('timestamp')))
            if row.get('type') == 'response_item':
                if p.get('type') == 'message':
                    index_message(response_messages, p.get('role'), p.get('content'), row.get('timestamp'))
                if p.get('type') in ('function_call', 'custom_tool_call'):
                    cid = str(p.get('call_id', p.get('id', '')))
                    calls[cid] = p.get('name', '')
                    if _PRIVATE.search(_arguments(p.get('arguments', p.get('input')))):
                        blocked.add(cid)
        current_project = project
        for ordinal, row in self.rows(path):
            typ, p, stamp = row.get('type'), row.get('payload', {}), row.get('timestamp')
            if not isinstance(p, dict):
                continue
            if typ == 'turn_context' and 'cwd' in p:
                current_project = self.project(p['cwd'])
                continue
            item_type = p.get('type')
            ident = p.get('id') or ('line-' + str(ordinal))
            if typ == 'event_msg' and item_type == 'item_completed':
                # Modern events are supplied by SQLite, or self-contained item_completed.
                item = p.get('item')
                if isinstance(item, dict):
                    self.codex_item(sid, current_project, item.get('id', ident), p.get('completed_at_ms', stamp), item)
            elif typ == 'response_item':
                if item_type == 'message' and p.get('role') in ('user', 'assistant'):
                    if matches_message(completed_messages, p['role'], p.get('content'), stamp):
                        self.exclude('duplicate_event')
                    else:
                        self.add('codex', sid, current_project, ident, stamp, p['role'], 'message', p.get('content'))
                elif item_type in ('function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output'):
                    cid = str(p.get('call_id', p.get('id', '')))
                    if cid in completed_ids or p.get('id') in completed_ids:
                        self.exclude('duplicate_event'); continue
                    if cid in blocked:
                        self.exclude('sensitive_tool'); continue
                    ev = {'call_id': cid, 'tool': calls.get(cid, p.get('name', 'unknown'))}
                    if item_type.endswith('_output'):
                        output = p.get('output')
                        ev.update(_evidence(output))
                        self.add('codex', sid, current_project, cid + ':result', stamp, 'tool', 'tool_result', output, ev)
                    else:
                        self.add('codex', sid, current_project, cid + ':call', stamp, 'assistant', 'tool_call', ev['tool'] + '\n' + _arguments(p.get('arguments', p.get('input'))), ev)
            elif typ == 'event_msg' and item_type in ('user_message', 'agent_message'):
                role = 'user' if item_type == 'user_message' else 'assistant'
                if not matches_message(response_messages, role, p.get('message'), stamp) and not matches_message(completed_messages, role, p.get('message'), stamp):
                    self.add('codex', sid, current_project, ident, stamp, role, 'message', p.get('message'))
                else:
                    self.exclude('duplicate_event')

    def claude(self):
        for directory in self.sources.get('claude_dirs', []):
            root = Path(directory)
            if not root.exists():
                self.warn('claude', 'configured source is missing'); continue
            for path in root.glob('*/*.jsonl'):
                self.claude_file(path)

    def claude_file(self, path):
        contexts, calls, blocked = {}, {}, set()
        selected = False
        # Session cwd comes from real records; directory encodings are never trusted.
        for _, row in self.rows(path):
            sid = row.get('sessionId')
            if sid and row.get('cwd') and sid not in contexts:
                contexts[sid] = row['cwd']
            if self.project(row.get('cwd')):
                selected = True
        if not selected:
            self.exclude('unknown_or_outside_project'); return
        for sid, cwd in contexts.items():
            self.session(sid, cwd)
        for _, row in self.rows(path):
            if row.get('isSidechain') or row.get('isMeta'):
                continue
            msg = row.get('message')
            for block in msg.get('content', []) if isinstance(msg, dict) and isinstance(msg.get('content'), list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    cid = str(block.get('id', ''))
                    calls[cid] = block.get('name', 'unknown')
                    if _PRIVATE.search(_arguments(block.get('input'))):
                        blocked.add(cid)
        for ordinal, row in self.rows(path):
            if row.get('isSidechain') or row.get('isMeta') or row.get('type') not in ('user', 'assistant'):
                self.exclude('background_or_internal_record'); continue
            sid = row.get('sessionId')
            if not sid or sid in self.exclude_ids:
                self.exclude('excluded_session'); continue
            cwd = row.get('cwd', contexts.get(sid))
            project = self.project(cwd)
            if not project:
                self.exclude('unknown_or_outside_project'); continue
            msg = row.get('message', {})
            if not isinstance(msg, dict):
                continue
            content = msg.get('content')
            ident = row.get('uuid') or ('line-' + str(ordinal))
            stamp = row.get('timestamp')
            blocks = content if isinstance(content, list) else [{'type': 'text', 'text': _text(content)}]
            for number, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                typ = block.get('type')
                if typ == 'text':
                    self.add('claude', sid, project, str(ident) + ':' + str(number), stamp, row['type'], 'message', block.get('text'))
                elif typ in ('tool_use', 'tool_result'):
                    cid = str(block.get('id') if typ == 'tool_use' else block.get('tool_use_id', ''))
                    if cid in blocked:
                        self.exclude('sensitive_tool'); continue
                    ev = {'call_id': cid, 'tool': calls.get(cid, block.get('name', 'unknown'))}
                    if typ == 'tool_use':
                        self.add('claude', sid, project, cid + ':call', stamp, 'assistant', 'tool_call', ev['tool'] + '\n' + _arguments(block.get('input')), ev)
                    else:
                        ev.update(_evidence(block))
                        # is_error=False only means Claude did not report an error;
                        # it does not establish the command's exit status.
                        extra = row.get('toolUseResult')
                        if isinstance(extra, dict):
                            extra_evidence = _evidence(extra)
                            ev.update({k: v for k, v in extra_evidence.items() if v is not None})
                            if block.get('is_error') is True:
                                ev['success'] = False
                        self.add('claude', sid, project, cid + ':result', stamp, 'tool', 'tool_result', block.get('content'), ev)

    def hermes(self):
        path = self.sources.get('hermes_db')
        if not path:
            return
        if not Path(path).exists():
            self.warn('hermes', 'configured source is missing'); return
        try:
            conn = _connect(path)
            try:
                self.hermes_conn(conn)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as error:
            match = re.match(r'^\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)(\\.*)$', str(path), re.I)
            if match and self.sources.get('hermes_wsl_fallback', True):
                self.stats['hermes_wsl_fallbacks'] = self.stats.get('hermes_wsl_fallbacks', 0) + 1
                self.hermes_wsl(match[1], match[2].replace('\\', '/'))
            else:
                self.warn('hermes', 'database could not be read', error)

    def hermes_conn(self, conn):
        scols, mcols = _columns(conn, 'sessions'), _columns(conn, 'messages')
        if not {'id', 'cwd', 'source'} <= scols or not {'id', 'session_id', 'role', 'content', 'timestamp'} <= mcols:
            self.warn('hermes', 'unsupported database schema'); return
        wanted = [c for c in ('id', 'cwd', 'source', 'origin_json', 'parent_session_id') if c in scols]
        for row in conn.execute('SELECT ' + ','.join(wanted) + ' FROM sessions'):
            session = dict(row)
            project = self.hermes_session(session)
            if not project:
                continue
            cols = [c for c in ('id', 'role', 'content', 'tool_call_id', 'tool_calls', 'tool_name', 'timestamp', '_compressed_summary', 'active', 'compacted') if c in mcols]
            messages = [dict(r) for r in conn.execute('SELECT ' + ','.join(cols) + ' FROM messages WHERE session_id=? ORDER BY timestamp,id', (row['id'],))]
            self.hermes_messages(row['id'], project, messages)

    def hermes_session(self, session):
        origin = [session.get('source'), session.get('origin_json')]
        if session.get('parent_session_id'):
            origin.append('subagent')
        return self.session(session['id'], session.get('cwd'), origin)

    def hermes_messages(self, sid, project, messages):
        calls, blocked = {}, set()
        for row in messages:
            for call in _json(row.get('tool_calls'), []) or []:
                if not isinstance(call, dict):
                    continue
                fun = call.get('function', call)
                cid = str(call.get('id', ''))
                calls[cid] = fun.get('name', 'unknown')
                if _PRIVATE.search(_arguments(fun.get('arguments'))):
                    blocked.add(cid)
        for row in messages:
            if row.get('_compressed_summary') or row.get('compacted') or row.get('active') == 0:
                self.exclude('compressed_or_inactive_message'); continue
            role, stamp, ident = row['role'], row['timestamp'], row['id']
            if role in ('user', 'assistant'):
                self.add('hermes', sid, project, ident, stamp, role, 'message', row.get('content'))
                for call in _json(row.get('tool_calls'), []) or []:
                    if not isinstance(call, dict):
                        continue
                    cid = str(call.get('id', ''))
                    if cid in blocked:
                        self.exclude('sensitive_tool'); continue
                    fun = call.get('function', call)
                    ev = {'call_id': cid, 'tool': calls[cid]}
                    self.add('hermes', sid, project, cid + ':call', stamp, 'assistant', 'tool_call', calls[cid] + '\n' + _arguments(fun.get('arguments')), ev)
            elif role == 'tool':
                cid = str(row.get('tool_call_id') or '')
                if cid in blocked:
                    self.exclude('sensitive_tool'); continue
                ev = {'call_id': cid, 'tool': calls.get(cid, row.get('tool_name') or 'unknown')}
                ev.update(_evidence(row.get('content')))
                self.add('hermes', sid, project, str(ident) + ':result', stamp, 'tool', 'tool_result', row.get('content'), ev)
            else:
                self.exclude('system_or_internal_role')

    def hermes_wsl(self, distro, linux_path):
        # SQL/path/filter data travels as JSON stdin; no shell or interpolation.
        script = r'''
import json,sqlite3,sys,ntpath,re
p=json.load(sys.stdin)
def norm(v):
 v=re.sub(r'\\+',r'\\',str(v or '').replace('/','\\'))
 if v.startswith('\\?\\'):v=v[3:]
 m=re.match(r'^\\mnt\\([a-z])(?:\\|$)(.*)',v,re.I)
 if m:v=m[1]+':\\'+m[2]
 return ntpath.normpath(v).rstrip('\\').casefold() if re.match(r'^[a-z]:\\',v,re.I) else ''
c=sqlite3.connect('file:'+p['path']+'?mode=ro',uri=True,timeout=2);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
sc={r['name'] for r in c.execute('PRAGMA table_info(sessions)')};mc={r['name'] for r in c.execute('PRAGMA table_info(messages)')}
if not {'id','cwd','source'}<=sc or not {'id','session_id','role','content','timestamp'}<=mc:raise RuntimeError('unsupported schema')
cols=[k for k in ('id','cwd','source','origin_json','parent_session_id') if k in sc]
out=[]
for r in c.execute('SELECT '+','.join(cols)+' FROM sessions'):
 s=dict(r);cwd=norm(s.get('cwd'));selected=any(cwd==root or cwd.startswith(root+'\\') for root in p['roots'] if cwd and root)
 item={'session':s,'messages':[]}
 origin=json.dumps([s.get('source'),s.get('origin_json')]).lower()
 if selected and s['id'] not in p['exclude_ids'] and not s.get('parent_session_id') and not any(x in origin for x in ('cron','background','subagent','automation')):
  mcols=[k for k in ('id','role','content','tool_call_id','tool_calls','tool_name','timestamp','_compressed_summary','active','compacted') if k in mc]
  item['messages']=[dict(m) for m in c.execute('SELECT '+','.join(mcols)+' FROM messages WHERE session_id=? ORDER BY timestamp,id',(s['id'],))]
 out.append(item)
c.close();print(json.dumps(out,ensure_ascii=False))
'''
        data = {'path': linux_path, 'roots': [p[0] for p in self.projects], 'exclude_ids': list(self.exclude_ids)}
        try:
            result = subprocess.run(['wsl.exe', '-d', distro, '--exec', 'python3', '-c', script], input=json.dumps(data),
                                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                                    timeout=float(self.sources.get('hermes_wsl_timeout', 30)), check=True,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            rows = json.loads(result.stdout)
            for row in rows:
                session = row['session']
                project = self.hermes_session(session)
                if project:
                    self.hermes_messages(session['id'], project, row['messages'])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            self.warn('hermes', 'WSL read-only fallback failed', error)


def collect_events(config: dict, start_date: date, end_date: date) -> dict:
    """Collect only explicitly matched projects, with inclusive UTC+8 calendar dates."""
    if end_date < start_date:
        raise ValueError('end_date must be on or after start_date')
    collector = _Collector(config, start_date, end_date)
    for name in ('codex', 'claude', 'hermes'):
        try:
            getattr(collector, name)()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            collector.warn(name, 'source processing incomplete', error)
    collector.events.sort(key=lambda event: (event['timestamp'], event['id']))
    collector.stats['events_total'] = len(collector.events)
    collector.stats['by_source'] = dict(collections.Counter(event['source'] for event in collector.events))
    if collector.stats['malformed_records']:
        collector.warnings.append('Some source records were malformed and were excluded; collection is incomplete for those records.')
    return {'events': collector.events, 'stats': collector.stats, 'warnings': collector.warnings}
