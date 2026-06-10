#!/usr/bin/env python3
"""
hermes_adapters.py — Hermes工具适配器

让念念通过 COMMAND/handle() 接口无缝使用Hermes工具生态。

使用importlib直接加载Hermes工具模块（避免包导入命名冲突）。
"""

import importlib.util as _iu
import json as _json
import os as _os
from pathlib import Path as _Path

# ── Hermes工具目录（自动检测，支持VPS/Termux） ──────────
_HERMES_TOOLS = None
_HERMES_OK = False
for _cand in [
    _os.environ.get("HERMES_AGENT_DIR", ""),
    _os.environ.get("HERMES_HOME", ""),
    _os.path.expanduser("~/.hermes/hermes-agent"),
    _os.path.expanduser("~/hermes/hermes-agent"),
    "/data/data/com.termux/files/home/.hermes/hermes-agent",
    "/data/data/com.termux/files/home/hermes/hermes-agent",
]:
    if _cand:
        _p = _Path(_cand) / "tools"
        if _p.is_dir():
            _HERMES_TOOLS = _p
            _HERMES_OK = True
            break

_web = _term = _cron = _send = _mem = _skills = _delegate = _todo = _image = _tts = _vision = _xsearch = None


def _load_hermes_module(name: str):
    """用importlib加载Hermes工具模块，避免包命名冲突。"""
    f = _HERMES_TOOLS / f"{name}.py"
    if not f.exists():
        # 尝试子目录
        for sub in _HERMES_TOOLS.iterdir():
            if sub.is_dir() and not sub.name.startswith("_"):
                sf = sub / f"{name}.py"
                if sf.exists():
                    f = sf
                    break
        else:
            return None
    spec = _iu.spec_from_file_location(f"hermes_{name}", f)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get(mod_name, func_name):
    """懒加载Hermes工具模块，返回指定函数。"""
    global _web, _term, _cron, _send, _mem, _skills, _delegate, _todo, _image
    cache = {
        "web_tools": "_web", "terminal_tool": "_term", "cronjob_tools": "_cron",
        "send_message_tool": "_send", "memory_tool": "_mem", "skills_tool": "_skills",
        "delegate_tool": "_delegate", "todo_tool": "_todo", "image_generation_tool": "_image",
        "tts_tool": "_tts", "vision_tools": "_vision", "x_search_tool": "_xsearch",
    }
    if mod_name in cache:
        var = cache[mod_name]
        if globals().get(var) is None:
            globals()[var] = _load_hermes_module(mod_name)
        mod = globals()[var]
    else:
        mod = _load_hermes_module(mod_name)
    if mod is None:
        raise ImportError(f"Hermes工具不可用: {mod_name}")
    return getattr(mod, func_name)


# ═══════════════════════════════════════════════════════
#  搜索
# ═══════════════════════════════════════════════════════

COMMAND_SEARCH = "搜索"

def handle_search(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("web_tools", "web_search_tool")
        result = fn(text)
        return _json.dumps({"output": result, "status": "ok"}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


COMMAND_EXTRACT = "提取"

def handle_extract(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("web_tools", "web_extract_tool")
        urls = text.strip().split() if text else []
        result = fn(urls)
        return _json.dumps({"output": result, "status": "ok"}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  定时任务
# ═══════════════════════════════════════════════════════

COMMAND_CRON = "定时"

def handle_cron(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("cronjob_tools", "cronjob")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  记忆
# ═══════════════════════════════════════════════════════

COMMAND_MEMORY = "记忆"

def handle_memory(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("memory_tool", "memory_tool")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  技能
# ═══════════════════════════════════════════════════════

COMMAND_SKILLS = "技能"

def handle_skills(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        mod = _load_hermes_module("skills_tool")
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0] if parts else "list"
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("列表", "list"):
            result = mod.skills_list()
        elif cmd in ("查看", "view"):
            result = mod.skill_view(arg)
        elif cmd in ("管理", "manage"):
            result = mod.skill_manage(arg)
        else:
            result = mod.skills_list()
        return _json.dumps({"output": str(result), "status": "ok"}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  To-Do
# ═══════════════════════════════════════════════════════

COMMAND_TODO = "待办"

def handle_todo(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("todo_tool", "todo_tool")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  图片生成
# ═══════════════════════════════════════════════════════

COMMAND_IMAGE = "画图"

def handle_image(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("image_generation_tool", "image_generate_tool")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  发送消息
# ═══════════════════════════════════════════════════════

COMMAND_SEND = "发送"

def handle_send(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("send_message_tool", "send_message_tool")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  委托任务
# ═══════════════════════════════════════════════════════

COMMAND_DELEGATE = "委托"

def handle_delegate(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("delegate_tool", "delegate_task")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  MCP 工具发现
# ═══════════════════════════════════════════════════════

COMMAND_MCP = "MCP"

def handle_mcp(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        mod = _load_hermes_module("mcp_tool")
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0] if parts else "status"
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("列表", "list", "discover"):
            result = mod.discover_mcp_tools()
        elif cmd in ("状态", "status"):
            result = mod.get_mcp_status()
        elif cmd in ("注册", "register"):
            result = mod.register_mcp_servers()
        else:
            result = mod.discover_mcp_tools()
        return _json.dumps({"output": str(result), "status": "ok"}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  语音
# ═══════════════════════════════════════════════════════

COMMAND_TTS = "语音"

def handle_tts(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("tts_tool", "text_to_speech_tool")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  视觉
# ═══════════════════════════════════════════════════════

COMMAND_VISION = "视觉"

def handle_vision(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("vision_tools", "vision_analyze_tool")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  X/Twitter 搜索
# ═══════════════════════════════════════════════════════

COMMAND_XSEARCH = "推搜"

def handle_xsearch(text: str) -> str:
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        fn = _get("x_search_tool", "x_search_tool")
        result = fn(text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  浏览器
# ═══════════════════════════════════════════════════════

COMMAND_BROWSER = "浏览"

def handle_browser(text: str) -> str:
    """浏览器工具——分发到子命令。需要Hermes安装Chromium。"""
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        mod = _load_hermes_module("browser_tool")
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0] if parts else "navigate"
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "navigate":
            result = mod.browser_navigate(arg)
        elif cmd == "snapshot":
            result = mod.browser_snapshot()
        elif cmd == "click":
            result = mod.browser_click(arg)
        elif cmd == "scroll":
            result = mod.browser_scroll(arg)
        elif cmd == "type":
            result = mod.browser_type(arg)
        elif cmd == "back":
            result = mod.browser_back()
        elif cmd == "press":
            result = mod.browser_press(arg)
        elif cmd == "console":
            result = mod.browser_console()
        elif cmd == "images":
            result = mod.browser_get_images()
        elif cmd == "vision":
            result = mod.browser_vision(arg)
        else:
            result = mod.browser_navigate(arg)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
#  防爬浏览器（Camofox — Docker Firefix改版，过Cloudflare）
# ═══════════════════════════════════════════════════════

COMMAND_ANTIBOT = "防爬"

def handle_antibot(text: str) -> str:
    """Camofox防爬浏览器 — 需要: docker run -p 9377:9377 jo-inc/camofox-browser"""
    if not _HERMES_OK:
        return _json.dumps({"output": "Hermes未安装", "status": "error"}, ensure_ascii=False)
    try:
        mod = _load_hermes_module("browser_camofox")
        parts = text.strip().split(maxsplit=2)
        cmd = parts[0] if parts else "navigate"
        arg1 = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""

        if cmd == "navigate":
            result = mod.camofox_navigate(arg1)
        elif cmd == "snapshot":
            result = mod.camofox_snapshot()
        elif cmd == "click":
            result = mod.camofox_click(arg1)
        elif cmd == "type":
            result = mod.camofox_type(arg1, arg2)
        elif cmd == "scroll":
            result = mod.camofox_scroll(arg1)
        elif cmd == "back":
            result = mod.camofox_back()
        elif cmd == "press":
            result = mod.camofox_press(arg1)
        elif cmd == "images":
            result = mod.camofox_get_images()
        elif cmd == "vision":
            result = mod.camofox_vision(arg1)
        elif cmd == "console":
            result = mod.camofox_console()
        else:
            result = mod.camofox_navigate(arg1 if arg1 else text)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return _json.dumps({"output": "", "error": str(e), "status": "error"}, ensure_ascii=False)
