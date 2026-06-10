# L3 — 技能记忆（Skills）

> 可复用的操作流程和工具使用模式。
> 从成功执行的任务中提取。

---

## 内置技能

### 终端工具使用
- 命令：`终端 <shell命令>` 或 `!exec <命令>`
- 所有shell执行通过terminal.py
- 危险模式自动拦截（rm -rf /, mkfs, dd if= 等）
- 输出三段式：命令 → 输出 → 分析

### 身份查询
- `你是谁` / `你在哪` → 从soul.md返回
- `help` → 列出所有可用命令

### 配置向导
- `终端 配置llm` → 交互式LLM配置
- `终端 配置tg` → 交互式TG Bot配置
- 配置写入 data/config.json

## 借脑技能

### 借LLM
- 自然语言输入→自动路由到bridge.call_llm()
- LLM可回调terminal工具（最多3轮）

### 借Hermes
- 复杂任务通过bridge.call_hermes()调用
- Hermes CLI必须已安装

### 借OpenClaw
- 通过bridge.call_openclaw()调用
- OpenClaw必须已安装

---

*此文件由core.py从tasklog.db自动提取更新。*
