"""Atomic, backed-up edits with explicit ownership of generated blocks."""
from contextlib import contextmanager, ExitStack
from datetime import date, datetime
import hashlib
import os
from pathlib import Path
import re
import tempfile

DAILY_START = '<!-- auto-ai-summary:start -->'
DAILY_END = '<!-- auto-ai-summary:end -->'
TOPIC_START = '<!-- auto-learning:start -->'
TOPIC_END = '<!-- auto-learning:end -->'


class WriteConflict(RuntimeError):
    """A user or another writer changed a file after it was read."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        if stream.read(1) == b'':
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WriteConflict('Another sync process is already writing.') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def replace_block(text, body, start, end):
    if any(marker in body for marker in (DAILY_START, DAILY_END, TOPIC_START, TOPIC_END)):
        raise ValueError('Generated content may not contain ownership markers.')
    a, b = text.count(start), text.count(end)
    newline = '\r\n' if '\r\n' in text else '\n'
    body = body.replace('\r\n', '\n').strip().replace('\n', newline)
    block = start + newline + body + newline + end
    if a == b == 0:
        return text.rstrip() + newline * 2 + block + newline
    if a != 1 or b != 1 or text.index(start) >= text.index(end):
        raise ValueError('Ownership markers are missing, duplicated or reversed; file was not modified.')
    left, right = text.index(start), text.index(end) + len(end)
    return text[:left] + block + text[right:]


def protected_passages(text):
    """Keep personal/practical sections verbatim when refining knowledge prose."""
    text = re.sub(re.escape(TOPIC_START) + r'.*?' + re.escape(TOPIC_END), '', text, flags=re.S)
    headings = list(re.finditer(r'^(#{2,6})[ \t]+([^\r\n]+)', text, re.M))
    spans = []
    for i, heading in enumerate(headings):
        personal = re.search(r'我的|个人|感受|反思|实践|操作记录|验证|实验|实测|日志', heading[2])
        knowledge = re.search(r'定义|概念|原理|用法|例子|示例|误区|注意事项|参考|官方|简介|概述|速查|基础|总结|是什么|何时', heading[2])
        if personal or not knowledge:
            end = next((h.start() for h in headings[i + 1:] if len(h[1]) <= len(heading[1])), len(text))
            if not any(a <= heading.start() < b for a, b in spans):
                spans.append((heading.start(), end))
    for paragraph in re.finditer(r'(?:^|\r?\n\r?\n)([^\r\n].*?)(?=\r?\n\r?\n|\Z)', text, re.S):
        if re.search(r'(?:^|[。\r\n])\s*(?:(?:[-*+>]|\d+[.)、])\s*)*(?:我|今天|当时|本机|实测|现场|亲自)'
                     r'|我(?:亲自|实测|已经|发现|在|用|独立|把|的)|运行结果|\d{4}[-年]\d{1,2}[-月]\d{1,2}', paragraph[1]):
            if not any(a <= paragraph.start(1) < b for a, b in spans):
                spans.append((paragraph.start(1), paragraph.end(1)))
    return [text[a:b].strip() for a, b in sorted(spans) if text[a:b].strip()]


class VaultWriter:
    def __init__(self, vault, backup_dir):
        self.vault = Path(vault).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def target(self, relative, area):
        relative = str(relative).replace('\\', '/')
        parts = relative.split('/')
        if (not parts or parts[0] != area or any(p in ('', '.', '..') for p in parts)
                or any(re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((' ', '.')) for p in parts)
                or not relative.endswith('.md')):
            raise ValueError('Invalid or out-of-scope note path.')
        if any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p) for p in parts):
            raise ValueError('Reserved Windows filename.')
        path = (self.vault / relative).resolve()
        if not path.is_relative_to(self.vault / area):
            raise ValueError('Note path escapes its authorized area through a link or junction.')
        return path

    def _edit(self, path, transform, expected_hash=None):
        key = digest(str(path).lower().encode())
        with file_lock(self.backup_dir / 'locks' / (key + '.lock')):
            original = path.read_bytes() if path.exists() else b''
            if expected_hash is not None and digest(original) != expected_hash:
                raise WriteConflict('Note changed after generation; keeping the user version.')
            text = original.decode('utf-8-sig')
            updated = transform(text).encode('utf-8')
            self._replace_checked(path, original, updated)
            return path

    def _replace_checked(self, path, original, updated):
            if updated == original:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            if original:
                relative = path.relative_to(self.vault)
                folder = self.backup_dir / 'notes' / relative.parent
                folder.mkdir(parents=True, exist_ok=True)
                backup = folder / (path.stem + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + digest(original)[:10] + '.md')
                backup.write_bytes(original)
            fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                current = path.read_bytes() if path.exists() else b''
                if current != original:
                    raise WriteConflict('Note was edited during writing; keeping the user version.')
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _daily_text(self, day, markdown, text):
        if not text:
            text = (f'# {day}\n\n## 个人记录与反思\n\n## 我的学习计划\n\n## AI\n'
                    f'{DAILY_START}\n{DAILY_END}\n')
        elif DAILY_START not in text and DAILY_END not in text:
            text = text.rstrip() + '\n\n## AI\n'
        return replace_block(text, markdown, DAILY_START, DAILY_END)

    def _topic_text(self, path, markdown, text, edit_mode):
        if edit_mode not in ('supplement', 'refine'):
            raise ValueError('Unknown topic edit mode.')
        # Validate old ownership markers before any editorial restructuring.
        if TOPIC_START in text or TOPIC_END in text:
            replace_block(text, markdown, TOPIC_START, TOPIC_END)
        if edit_mode == 'refine' and text and '<!-- preserved-original:start -->' not in text:
            title = next((x for x in text.splitlines() if x.startswith('# ')), '# ' + path.stem)
            # Natural-language heuristics cannot reliably identify every personal story.
            # Preserve ALL unmarked original prose verbatim outside the editable block.
            original = re.sub(re.escape(TOPIC_START) + r'.*?' + re.escape(TOPIC_END), '', text, flags=re.S).strip()
            text = (title + '\n\n## 知识整理与补充\n' + TOPIC_START + '\n' + TOPIC_END + '\n\n'
                    '## 原始笔记与个人记录\n\n<details>\n<summary>展开原文，核对经历和实践</summary>\n\n'
                    '<!-- preserved-original:start -->\n' + original + '\n<!-- preserved-original:end -->\n\n</details>\n')
        elif not text:
            text = '# ' + path.stem + '\n\n'
        if edit_mode == 'supplement' and TOPIC_START in text:
            old = text.split(TOPIC_START, 1)[1].split(TOPIC_END, 1)[0].strip()
            if old and markdown.strip() not in old:
                if old not in markdown:
                    markdown = old + '\n\n### 本次补充\n\n' + markdown.strip()
            elif old:
                markdown = old
        if TOPIC_START not in text and TOPIC_END not in text:
            text = text.rstrip() + '\n\n## 知识整理与补充\n' if not text.rstrip().endswith('## 知识整理与补充') else text
        return replace_block(text, markdown, TOPIC_START, TOPIC_END)

    def write_daily(self, day, markdown):
        day = date.fromisoformat(str(day)).isoformat()
        path = self.target(f'01-每日记录/{day}.md', '01-每日记录')
        return self._edit(path, lambda text: self._daily_text(day, markdown, text))

    def write_topic(self, relative_path, markdown, expected_hash=None, edit_mode='supplement'):
        path = self.target(relative_path, '02-学习')
        if edit_mode == 'refine' and path.exists() and expected_hash is None:
            raise ValueError('Refining an existing topic requires its original content hash.')
        return self._edit(path, lambda text: self._topic_text(path, markdown, text, edit_mode), expected_hash)

    def apply_batch(self, operations):
        """Preflight every file, then commit; conditionally roll back our own writes."""
        targets = []
        for op in operations:
            if op['kind'] == 'daily':
                day = date.fromisoformat(str(op['day'])).isoformat()
                path = self.target(f'01-每日记录/{day}.md', '01-每日记录')
                transform = lambda old, d=day, body=op['markdown']: self._daily_text(d, body, old)
            elif op['kind'] == 'topic':
                path = self.target(op['path'], '02-学习')
                mode = op.get('edit_mode', 'supplement')
                if mode == 'refine' and path.exists() and op.get('expected_hash') is None:
                    raise ValueError('Refining an existing topic requires its original hash.')
                transform = lambda old, p=path, body=op['markdown'], m=mode: self._topic_text(p, body, old, m)
            elif op['kind'] == 'sources':
                day = date.fromisoformat(str(op['day'])).isoformat()
                path = self.target(f'06-脚本/运行记录/{day}-来源.md', '06-脚本')
                transform = lambda old, d=day, events=op['events']: self._source_text(d, events)
            else:
                raise ValueError('Unknown batch operation.')
            targets.append((path, transform, op.get('expected_hash')))
        if len({str(p).casefold() for p, _, _ in targets}) != len(targets):
            raise ValueError('Duplicate batch target.')
        with ExitStack() as locks:
            for path, _, _ in sorted(targets, key=lambda x: str(x[0]).casefold()):
                locks.enter_context(file_lock(self.backup_dir / 'locks' / (digest(str(path).lower().encode()) + '.lock')))
            plans = []
            for path, transform, expected in targets:
                existed = path.exists()
                original = path.read_bytes() if existed else b''
                if expected is not None and digest(original) != expected:
                    raise WriteConflict('A topic changed before the batch could be committed.')
                updated = transform(original.decode('utf-8-sig')).encode('utf-8')
                plans.append((path, original, updated, existed))
            completed = []
            try:
                for path, original, updated, existed in plans:
                    self._replace_checked(path, original, updated)
                    if updated != original:
                        completed.append((path, original, updated, existed))
            except Exception:
                for path, original, updated, existed in reversed(completed):
                    if path.exists() and path.read_bytes() == updated:
                        if existed:
                            self._replace_checked(path, updated, original)
                        else:
                            path.unlink()
                raise
            return [path for path, _, _, _ in plans]

    def append_personal(self, day, text):
        day = date.fromisoformat(str(day)).isoformat()
        if not text.strip() or any(m in text for m in (DAILY_START, DAILY_END, TOPIC_START, TOPIC_END)):
            raise ValueError('Personal record is empty or contains reserved markers.')
        path = self.target(f'01-每日记录/{day}.md', '01-每日记录')
        marker = '<!-- personal-entry:' + digest(text.strip().encode())[:24] + ' -->'
        def update(old):
            if marker in old:
                return old
            if not old:
                old = f'# {day}\n\n## 个人记录与反思\n\n## AI\n{DAILY_START}\n{DAILY_END}\n'
            match = re.search(r'^## (?:📝 )?(?:个人记录[^\n]*|个人书写[^\n]*)\n', old, re.M)
            addition = '\n' + marker + '\n' + text.strip() + '\n\n'
            if match:
                return old[:match.end()] + addition + old[match.end():]
            return old.rstrip() + '\n\n## 个人记录与反思\n' + addition
        return self._edit(path, update)

    def _source_text(self, day, events):
        rows = [f'# {day} 记录依据', '', '这里只记录来源定位和执行状态，不保存完整聊天。', '']
        for event in events:
            evidence = event.get('evidence') or {}
            rows.extend([f'## {event["id"]}',
                         f'- 时间：{event.get("timestamp", "")}',
                         f'- 来源：{event.get("source", "")} / {event.get("project", "")}',
                         f'- 会话：`{event.get("session_id", "")}`',
                         f'- 类型：{event.get("role", "")} / {event.get("kind", "")}',
                         f'- 执行结果：{evidence.get("status", "未单独验证")}；退出码 {evidence.get("exit_code", "未提供")}', ''])
        return '\n'.join(rows).rstrip() + '\n'

    def write_source_index(self, day, events):
        day = date.fromisoformat(str(day)).isoformat()
        path = self.target(f'06-脚本/运行记录/{day}-来源.md', '06-脚本')
        return self._edit(path, lambda _: self._source_text(day, events))

    def append_validation(self, relative_path, text):
        path = self.target(relative_path, '02-学习')
        if not text.strip() or any(m in text for m in (DAILY_START, DAILY_END, TOPIC_START, TOPIC_END)):
            raise ValueError('Validation record is empty or contains reserved markers.')
        marker = '<!-- personal-validation:' + digest(text.strip().encode())[:24] + ' -->'
        def update(old):
            if marker in old:
                return old
            if not old:
                old = '# ' + path.stem + '\n'
            return old.rstrip() + '\n\n## 我的验证记录\n\n' + marker + '\n' + text.strip() + '\n'
        return self._edit(path, update)
