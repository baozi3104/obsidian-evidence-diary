"""Run a synthetic, offline example without reading conversations or calling AI."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from obsidian_sync.notes import VaultWriter


def main():
    # Redirected Windows consoles may use a codec without Chinese glyphs.
    # Preserve a readable escaped path instead of failing after writing the demo.
    sys.stdout.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='A new, empty demo directory')
    args = parser.parse_args()
    root = Path(args.output).resolve()
    if root.exists() and any(root.iterdir()):
        parser.error('Choose a new or empty directory; existing notes are not demo input.')
    vault = root / 'vault'
    writer = VaultWriter(vault, root / 'backups')
    day = '2026-10-31'
    writer.append_personal(day, '这是一条合成的个人记录，用于演示原文保护。')
    body = '## 工作进展\n\n- 示例项目：尝试编写按键消抖程序，尚未在开发板上验证。'
    path = writer.write_daily(day, body)
    before = path.read_bytes()
    writer.write_daily(day, body)
    assert path.read_bytes() == before
    assert '合成的个人记录' in path.read_text(encoding='utf-8')
    print('Offline demo passed: personal text preserved; repeated write unchanged.')
    print(path)


if __name__ == '__main__':
    main()
