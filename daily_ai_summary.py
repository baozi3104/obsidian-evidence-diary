#!/usr/bin/env python3
"""兼容原入口：默认执行增量补跑，不再使用第三方接口或原文回退。"""
import sys
from obsidian_sync.engine import main

args = sys.argv[1:]
commands = {'sync', 'personal', 'verification', 'status', 'collect'}
position = 0
if args and args[0] == '--config':
    position = min(2, len(args))
elif args and args[0].startswith('--config='):
    position = 1
if (position == len(args) or args[position] not in commands) and not any(
        flag in args for flag in ('-h', '--help')):
    args.insert(position, 'sync')
raise SystemExit(main(args))
