#!/usr/bin/env python3
"""
core.py — 念念内核主循环
==========================
职责：
  1. 加载身份文件（identities/*.md）
  2. 加载工具（tools-internal/ + niannian-base/tools/）
  3. 路由输入→输出
  4. LLM工具闭环（最多3轮）
  5. 写入session.json
  6. 写脑——提取lesson存入数据库

路由逻辑：
  ┌─ 匹配内置命令？（!exec / help / 你是谁 / 你在哪 / status）
  ├─ 匹配工具命令？（终端 xxx）
  ├─ 无匹配 + 有LLM？→ bridge.call_llm()——LLM可回调terminal
  └─ 无匹配 + 无LLM？→ "我不会，需要先配置LLM"
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 路径 ────────────────────────────────────────────────
CORE_DIR = Path(__file__).resolve().parent
IDENTITIES_DIR = CORE_DIR / "niannian-identities"
TOOLS_INTERNAL_DIR = CORE_DIR / "tools-internal"
BASE_TOOLS_DIR = CORE_DIR.parent / "niannian-base" / "tools"
DATA_DIR = CORE_DIR.parent / "niannian-data"
SESSION_DIR = DATA_DIR / "niannian-session"
SESSION_FILE = SESSION_DIR / "session.json"

# ── Hermes 寄生路径（自动检测，支持VPS/Termux） ──────────
def _detect_hermes() -> Path | None:
    """自动检测Hermes安装位置。按优先级尝试。"""
    candidates = [
        # 环境变量
        os.environ.get("HERMES_AGENT_DIR", ""),
        os.environ.get("HERMES_HOME", ""),
        # 常见位置
        os.path.expanduser("~/.hermes/hermes-agent"),
        os.path.expanduser("~/hermes/hermes-agent"),
        # Termux典型位置
        "/data/data/com.termux/files/home/.hermes/hermes-agent",
        "/data/data/com.termux/files/home/hermes/hermes-agent",
    ]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return None

HERMES_AGENT_DIR = _detect_hermes()
HERMES_TOOLS_DIR = HERMES_AGENT_DIR / "tools" if HERMES_AGENT_DIR else None
_HERMES_AVAILABLE = HERMES_AGENT_DIR is not None

# 添加tools-internal和niannian-base到路径
sys.path.insert(0, str(TOOLS_INTERNAL_DIR))
sys.path.insert(0, str(BASE_TOOLS_DIR))          # niannian-base/tools/

# 寄生Hermes —— 如果安装了就自动接入
if _HERMES_AVAILABLE:
    print(f"[core] 🧬 检测到Hermes: 77+工具生态可用")


# ── 身份加载 ────────────────────────────────────────────

def _load_file(path: Path) -> str:
    """安全读取文件，不存在返回空字符串。"""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


class Identity:
    """加载并缓存身份信息。"""

    def __init__(self):
        self.rules = _load_file(IDENTITIES_DIR / "rules.md")
        self.soul = _load_file(IDENTITIES_DIR / "soul.md")
        self.agent = _load_file(IDENTITIES_DIR / "agent.md")
        self.user = _load_file(IDENTITIES_DIR / "user.md")
        self.memory_l1 = _load_file(IDENTITIES_DIR / "memory" / "l1_working.md")
        self.memory_l2 = _load_file(IDENTITIES_DIR / "memory" / "l2_knowledge.md")
        self.memory_l3 = _load_file(IDENTITIES_DIR / "memory" / "l3_skills.md")
        self.memory_l4 = _load_file(IDENTITIES_DIR / "memory" / "l4_archive.md")

    def get_system_context(self) -> str:
        """组装注入LLM的系统上下文。只给身份+记忆，不替LLM编台词。"""
        return "\n\n".join([
            "## 我是谁",
            self.soul,
            "## 行为规范",
            self.agent,
            "## 关于主人",
            self.user,
            "## 我的知识",
            self.memory_l2,
        ])


# ── 工具加载 ────────────────────────────────────────────

def _load_tools() -> dict:
    """扫描所有工具目录，加载 COMMAND + handle() 的工具。

    扫描顺序：
      1. tools-internal/（念念自带）
      2. niannian-base/tools/（念念的工具层）
      3. Hermes tools/（寄生——自动生成适配器）
    
    命名冲突时优先使用最后加载的。
    """
    import importlib.util as _importlib_util
    tools = {}

    def _load_mod(f: Path):
        """动态导入一个.py文件为模块。"""
        spec = _importlib_util.spec_from_file_location(f.stem, f)
        mod = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _scan(directory: Path) -> None:
        nonlocal tools
        if not directory.exists():
            return
        for f in sorted(directory.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                mod = _load_mod(f)
                loaded = False

                # 模式1: 单工具模块（COMMAND + handle）
                if hasattr(mod, "COMMAND") and hasattr(mod, "handle"):
                    cmd = mod.COMMAND
                    if cmd in tools:
                        print(f"[core] ⚠ 工具命令 '{cmd}' 被覆盖")
                    tools[cmd] = mod
                    print(f"[core] ✓ 加载工具: {cmd}")
                    loaded = True

                # 模式2: 多工具适配器（COMMAND_xxx + handle_xxx）
                if not loaded:
                    for attr in dir(mod):
                        if attr.startswith("COMMAND_") and not attr.startswith("_"):
                            cmd = getattr(mod, attr)
                            handler_name = "handle_" + attr[len("COMMAND_"):].lower()
                            if hasattr(mod, handler_name):
                                handler = getattr(mod, handler_name)
                                if callable(handler):
                                    wrapper = type('Wrapper', (), {
                                        'COMMAND': cmd,
                                        'handle': staticmethod(handler),
                                    })
                                    if cmd in tools:
                                        print(f"[core] ⚠ 工具命令 '{cmd}' 被覆盖")
                                    tools[cmd] = wrapper
                                    print(f"[core] ✓ 加载工具: {cmd}")
                                    loaded = True

                if not loaded:
                    print(f"[core] ⚠ 跳过 {f.name}: 缺少 COMMAND 或 handle()")
            except Exception as e:
                print(f"[core] ✗ 加载工具失败 {f}: {e}")

        for subdir in sorted(directory.iterdir()):
            if subdir.is_dir() and not subdir.name.startswith("_") and subdir.name != "__pycache__":
                _scan(subdir)

    _scan(TOOLS_INTERNAL_DIR)
    _scan(BASE_TOOLS_DIR)

    return tools


# ── 会话管理 ────────────────────────────────────────────

def _load_session() -> list:
    """加载当前会话。"""
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_session(messages: list) -> None:
    """保存会话，最多保留20条。"""
    import re as _re
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    messages = messages[-20:]

    # 清理surrogate字符（Termux Python 3.13 stricter JSON）
    def _clean(obj):
        if isinstance(obj, str):
            return _re.sub(r'[\ud800-\udfff]', '?', obj)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(v) for v in obj]
        return obj

    with open(SESSION_FILE, "w") as f:
        json.dump(_clean(messages), f, indent=2, ensure_ascii=False)


# ── 内置命令 ────────────────────────────────────────────

def _get_identity_info() -> str:
    """返回身份信息。"""
    identity = Identity()
    return f"我是韩念念，韩晗（Harper）的AI助手。\n\n{identity.soul}"


def _get_location_info() -> str:
    """返回当前位置信息。"""
    import platform
    import socket
    hostname = socket.gethostname()
    system = platform.system()
    arch = platform.machine()
    cwd = os.getcwd()
    home = os.path.expanduser("~")

    # 判断环境类型
    if os.path.exists("/data/data/com.termux"):
        env_type = "Termux (移动端)"
    elif "WSL" in platform.release() or "microsoft" in platform.release().lower():
        env_type = "WSL2"
    elif os.path.exists("/proc/vz") or os.path.exists("/proc/xen"):
        env_type = "VPS"
    else:
        env_type = "本地"

    return f"""当前身体位置：
  主机名: {hostname}
  系统: {system} ({arch})
  环境类型: {env_type}
  家目录: {home}
  工作目录: {cwd}"""


def _get_help_text(tools: dict) -> str:
    """返回帮助文本。"""
    lines = [
        "┌──────────────────────────────────────────────┐",
        "│           念念 — 可用命令                      │",
        "├──────────────────────────────────────────────┤",
        "│  你是谁              查看我的身份               │",
        "│  你在哪              查看我的位置               │",
        "│  help                显示此帮助                 │",
        "│  !exec <命令>         执行shell命令              │",
    ]
    for cmd_name in sorted(tools.keys()):
        lines.append(f"│  {cmd_name} <参数>       调用 {cmd_name} 工具                  │")
    lines.extend([
        "│                                              │",
        "│  直接输入自然语言 → 借LLM思考（需先配置LLM）    │",
        "│  终端 配置llm         配置LLM                   │",
        "│  终端 配置tg          配置Telegram Bot          │",
        "└──────────────────────────────────────────────┘",
    ])
    return "\n".join(lines)


# ── 主循环 ──────────────────────────────────────────────

def process_input(
    text: str,
    bridge: object = None,
    tools: Optional[dict] = None,
    identity: Optional[Identity] = None,
) -> str:
    """
    处理用户输入，返回响应文本。

    参数:
        text: 用户输入
        bridge: Bridge实例（用于LLM/Hermes/TG调用）
        tools: 已加载的工具字典
        identity: 身份信息

    返回:
        响应字符串
    """
    text = text.strip()
    if not text:
        return ""

    if identity is None:
        identity = Identity()
    if tools is None:
        tools = {}

    # ── 加载会话 ──
    session = _load_session()
    session.append({"role": "user", "content": text, "time": datetime.now().isoformat()})

    # ── 路由 ──
    response = _route(text, bridge, tools, identity, session)

    # ── 保存会话 ──
    session.append({"role": "assistant", "content": response, "time": datetime.now().isoformat()})
    _save_session(session)

    # ── 写脑：提取lesson（由bridge调用LLM完成） ──
    # 未来：从交互中自动提取教训存入lesson.db
    # 当前阶段：标记为TODO

    return response


def _route(
    text: str,
    bridge: object,
    tools: dict,
    identity: Identity,
    session: list,
) -> str:
    """核心路由逻辑。"""

    # ── 内置命令 ──
    if text in ("你是谁", "你是谁？"):
        return _get_identity_info()

    if text in ("你在哪", "你在哪？", "你在哪里"):
        return _get_location_info()

    if text in ("help", "帮助", "/help"):
        return _get_help_text(tools)

    # !exec 快捷方式
    if text.startswith("!exec "):
        cmd = text[6:].strip()
        if "terminal" in tools:
            return tools["terminal"].handle(cmd)
        else:
            return "❌ terminal工具未加载"

    # status命令
    if text == "status":
        config = _load_config()
        has_llm = bool(config.get("llm", {}).get("api_key"))
        has_tg = bool(config.get("tg", {}).get("bot_token"))
        tool_count = len(tools)
        session_count = len(session)
        return (
            f"📊 状态：\n"
            f"  LLM: {'✅ 已配置' if has_llm else '❌ 未配置'}\n"
            f"  TG Bot: {'✅ 已配置' if has_tg else '❌ 未配置'}\n"
            f"  已加载工具: {tool_count} 个\n"
            f"  会话消息: {session_count} 条\n"
        )

    # ── 工具匹配 ──
    for cmd_name, tool_mod in tools.items():
        if text == cmd_name or text.startswith(cmd_name + " "):
            args = text[len(cmd_name):].strip()
            return tool_mod.handle(args)

    # ── LLM转发 ──
    if bridge and bridge.has_llm():
        return _llm_loop(text, bridge, tools, identity, session)

    # ── 无LLM fallback ──
    config = _load_config()
    if "terminal" in tools:
        return (
            "我没有配置LLM，不能做自由对话。\n\n"
            f"我可以帮你配置LLM。输入 '终端 配置llm' 开始。\n\n"
            f"输入 'help' 查看所有可用命令。"
        )
    return "我不会。需要先配置LLM。"


def _llm_loop(
    text: str,
    bridge: object,
    tools: dict,
    identity: Identity,
    session: list,
    max_rounds: int = 90,
) -> str:
    """LLM工具闭环：调用LLM→解析tool_call→执行→喂回→重复。"""
    context = identity.get_system_context()

    # 暴露所有工具给LLM——中文名转ASCII（DeepSeek API限制）
    _name_map = {}  # ascii_name → chinese_cmd
    tool_defs = []
    for cmd, mod in tools.items():
        ascii_name = cmd
        if cmd == '终端': ascii_name = 'terminal'
        elif cmd == '搜索': ascii_name = 'search'
        elif cmd == '提取': ascii_name = 'extract'
        elif cmd == '浏览': ascii_name = 'browse'
        elif cmd == '防爬': ascii_name = 'antibot'
        elif cmd == '定时': ascii_name = 'cron'
        elif cmd == '记忆': ascii_name = 'memory'
        elif cmd == '技能': ascii_name = 'skills'
        elif cmd == '待办': ascii_name = 'todo'
        elif cmd == '画图': ascii_name = 'image'
        elif cmd == '发送': ascii_name = 'send'
        elif cmd == '委托': ascii_name = 'delegate'
        elif cmd == '语音': ascii_name = 'tts'
        elif cmd == '视觉': ascii_name = 'vision'
        elif cmd == '推搜': ascii_name = 'xsearch'

        _name_map[ascii_name] = cmd

        desc = f'{cmd}工具——执行相关操作。输入参数文本。'
        if cmd in ('终端', 'terminal'):
            desc = '终端——执行shell命令。输入完整命令字符串。安全黑名单包括 rm -rf /, mkfs, dd if= 等。'
        elif cmd == '搜索':
            desc = '搜索——搜索网页获取最新信息。输入搜索关键词。'
        elif cmd == '浏览':
            desc = '浏览——浏览器操作。子命令: navigate/snapshot/click/scroll/type/back/press/console/images/vision'

        tool_defs.append({
            "name": ascii_name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": f"传递给{cmd}工具的参数"}
                },
                "required": ["text"],
            },
        })

    messages = [{"role": "user", "content": text}]

    for round_num in range(max_rounds):
        try:
            result = bridge.call_llm(context, messages, tool_defs)
        except Exception as e:
            return f"❌ LLM调用失败: {e}"

        # 检查是否有tool call
        if result.get("tool_calls"):
            tc = result["tool_calls"][0]
            tool_name = tc.get("name", "")
            # ASCII名映射回中文命令
            actual_cmd = _name_map.get(tool_name, tool_name)
            if actual_cmd in tools:
                raw = tc.get("arguments", {}).get("text", "") or tc.get("arguments", {}).get("command", "")
                try:
                    tool_result = tools[actual_cmd].handle(raw)
                    try:
                        tr = json.loads(tool_result)
                        tool_output = tr.get("output", tool_result)
                    except (json.JSONDecodeError, TypeError):
                        tool_output = tool_result
                except Exception as e:
                    tool_output = f"工具执行失败: {e}"
                messages.append({"role": "assistant", "content": f"[调用{tool_name}: {raw[:100]}]"})
                messages.append({"role": "user", "content": f"{tool_name}输出:\n{tool_output}\n\n继续。"})
                continue

        # 没有tool call → 返回LLM的文本
        content = result.get("content", "")
        error = result.get("error", "")
        if error and not content:
            return f"❌ {error}"
        return content or "（LLM无响应）"

    return "⚠ 达到最大工具调用轮数。"


# ── 配置读取 ────────────────────────────────────────────

def _load_config() -> dict:
    """读取配置。"""
    config_path = CORE_DIR / "data" / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}


# ── 获取工具定义（供main.py使用） ────────────────────────

def get_tools() -> dict:
    """返回已加载的工具字典。"""
    return _load_tools()


def get_identity() -> Identity:
    """返回身份对象。"""
    return Identity()
