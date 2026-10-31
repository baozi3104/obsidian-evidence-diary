# Obsidian Evidence Diary

把 AI 辅助工作与学习过程整理成有来源的日记和主题笔记，并保留自己写的内容。

这是从个人日常使用脚本整理出的早期命令行工具，主要面向 Windows + 中文 Obsidian 笔记库。当前没有 Obsidian 插件界面。Python 标准库即可运行，需要 Python 3.12 或更高版本（使用了目录联接检测接口）。

## 能做什么

- 按项目白名单收集本机 Codex、Claude 和明确归属的 Hermes 记录。
- 区分实际执行、用户自述、讨论或尝试、待确认和建议，并检查生成结果引用的来源。
- 日记只更新 AI 标记区；主题笔记支持补充和保留原文的润色模式。
- 支持预览、备份、重复执行、晚间补记、写入冲突检测及批量失败处理。
- 提供忠实追加个人记录和学习验证记录的入口。

## 先体验：不需要模型或真实聊天

下载或克隆本项目，在项目目录运行：

```powershell
python examples/demo.py --output D:/ObsidianDiaryDemo
python -m unittest discover -s tests -v
```

演示目录必须不存在或为空。演示使用合成文字，生成一篇带个人区与AI区的笔记，验证重复写入不变。把输出目录中的 `vault` 用 Obsidian 打开即可查看。

## 接入自己的笔记库

先用独立测试库试运行，再接入已有笔记。目录约定是 `01-每日记录`、`02-学习`；日期按北京时间处理。当前不提供任意目录映射。

1. 复制 `obsidian-sync.example.json` 为 `obsidian-sync.json`。
2. 修改 vault、runtime_dir、backup_dir、temp_dir、first_date 和项目路径。四个存储目录应使用不同的绝对路径；缓存和备份放在私有目录中。
3. 配置真实存在的 `codex_executable`；启用摘要生成需要本机已安装并登录的 Codex 命令行入口。程序也尝试查找 Windows 桌面安装目录。
4. `sources.codex_home` 指向你明确授权使用的会话目录；不需要的来源留空或不配置。仅收集 projects 白名单覆盖的项目。
5. 用真实存在的日期先收集，再预览：

```powershell
python daily_ai_summary.py --config obsidian-sync.json collect --start 2026-10-31 --end 2026-10-31
python daily_ai_summary.py --config obsidian-sync.json sync --date 2026-10-31 --preview
```

`collect` 只收集来源统计；`--preview` 会调用模型并在运行目录保存预览，不修改笔记。审阅预览文件后使用：

```powershell
python daily_ai_summary.py --config obsidian-sync.json sync --date 2026-10-31 --use-preview
python daily_ai_summary.py --config obsidian-sync.json status
```

普通 `sync` 会生成并写入内容；初次使用建议采用预览流程。`status` 展示脚本状态，不等于验证了 Obsidian 界面或定时任务。

## 个人记录和学习验证

把要写入的原文保存到UTF-8文本文件，避免将私人文本拼入命令：

```powershell
python daily_ai_summary.py --config obsidian-sync.json personal --date 2026-10-31 --text-file D:/NotesInput/personal.txt
python daily_ai_summary.py --config obsidian-sync.json verification --topic 02-学习/Python/异常处理.md --text-file D:/NotesInput/verification.txt
```

`verification` 用于记录已有解释或实践依据；写入记录不等于自动判定已经掌握。默认保留用户原文，AI总结不能代写个人感受或承诺。

## 隐私与运行边界

来源收集和文件写入发生在本机；**摘要生成会把筛选后的聊天、工具结果与笔记上下文提交给你登录的模型服务**。笔记上下文会遍历 `02-学习` 中符合条件的笔记，并非仅选择当前项目或主题相关的笔记；单篇和总量有大小限制，学习日志及越界链接会被排除。首次使用请建立只含可提交内容的独立测试库。当前生成命令还启用网络搜索能力。使用前确认这些材料可以发给该服务。

凭据过滤依赖路径规则、调用关联和文本模式，并不能保证识别所有秘密。不要授权敏感项目，不要公开配置、运行目录、预览、来源清单、备份或真实聊天。本仓库仅包含源码、合成测试和示例配置。

当前来源适配依赖客户端的本机会话存储格式，版本更新可能导致适配失效。写入保护依赖本机文件系统与协作锁，不保证与任意编辑器的所有并发写入互斥。请保留独立备份。

## 测试与兼容范围

回归测试用合成聊天、数据库与笔记，模型生成使用替身；Windows上的目录联接边界也有测试。离线通过不等于真实模型、最新客户端格式或所有平台已完成端到端验证。

当前优先支持Windows。定时运行可由用户自行在任务计划程序中调用 `python daily_ai_summary.py --config <绝对配置路径> sync`，建议禁止任务重叠并记录输出。本首版不自动安装任务。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `obsidian_sync/sources.py` | 来源发现、归属筛选、证据关联和脱敏 |
| `obsidian_sync/engine.py` | 摘要、预览、状态和命令行 |
| `obsidian_sync/notes.py` | 笔记区块、原文保护、备份和写入 |
| `tests/` | 合成数据回归测试 |

欢迎提供不含真实聊天或凭据的最小复现。许可证：MIT，见 [LICENSE](LICENSE)。
