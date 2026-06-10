#!/usr/bin/env python3
"""
terminal.py — 念念终端工具（薄适配层）
==========================================
底层能力委托给 niannian-base/tools/:
  - environments.py → LocalEnvironment / SSHEnvironment
  - process_manager.py → ProcessManager（后台进程管理）
  - ansi_strip.py → ANSI输出清洗

本文件只做：配置向导、命令解析路由、安全检查、帮助。
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ── 底层模块 ───────────────────────────────────────
from environments import (
    LocalEnvironment,
    SSHEnvironment,
    is_interrupted,
    set_interrupted,
    clear_interrupted,
)
from process_manager import process_manager
from ansi_strip import strip_ansi

COMMAND = "终端"

FOREGROUND_MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 180

# ── 本地执行环境（复用session snapshot） ────────────
_local_env: Optional[LocalEnvironment] = None
_ssh_env: Optional[SSHEnvironment] = None


def _get_local_env() -> LocalEnvironment:
    global _local_env
    if _local_env is None:
        _local_env = LocalEnvironment()
    return _local_env


def _get_ssh_env(host: str, user: str, port: int = 22, key: str = "") -> SSHEnvironment:
    global _ssh_env
    if _ssh_env is None or _ssh_env.host != host or _ssh_env.user != user:
        if _ssh_env:
            _ssh_env.cleanup()
        _ssh_env = SSHEnvironment(host=host, user=user, port=port, key_path=key)
    return _ssh_env


# ── 危险命令拦截 ──────────────────────────────────

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/",
    r"\brm\s+-rf\s+~",
    r"\brm\s+-rf\s+\*",
    r"\bmkfs\.",
    r"\bdd\s+if=",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\bchmod\s+-R\s+777\s+/",
    r">\s*/dev/sda",
]

_WORKDIR_SAFE_RE = re.compile(r'^[A-Za-z0-9/\\:_\-\.~ +@=,]+$')


def _check_dangerous(command: str) -> Optional[str]:
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return f"⛔ 危险命令已拦截：匹配模式 `{pattern}`"
    return None


# ── 配置管理 ──────────────────────────────────────

_CORE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _CORE_DIR / "data" / "config.json"


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _save_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── 命令执行 ──────────────────────────────────────

def _run_local(command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """本地执行（通过LocalEnvironment）。"""
    env = _get_local_env()
    try:
        result = env.execute(command, timeout=timeout)
        output = strip_ansi(result.get("output", ""))
        return {
            "output": output,
            "exit_code": result.get("returncode", 0),
            "error": "",
            "status": "ok" if result.get("returncode") == 0 else "error",
        }
    except Exception as e:
        return {"output": "", "exit_code": -1, "error": f"执行异常: {e}", "status": "error"}


def _run_ssh(host: str, user: str, command: str, port: int = 22,
             key: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """SSH远程执行（通过SSHEnvironment）。"""
    if not shutil.which("ssh"):
        return {"output": "", "exit_code": -1, "error": "SSH客户端未安装。apt install openssh-client", "status": "error"}

    try:
        env = _get_ssh_env(host, user, port, key)
        result = env.execute(command, timeout=timeout)
        output = strip_ansi(result.get("output", ""))
        return {
            "output": output,
            "exit_code": result.get("returncode", 0),
            "error": "",
            "status": "ok" if result.get("returncode") == 0 else "error",
        }
    except RuntimeError as e:
        return {"output": "", "exit_code": -1, "error": str(e), "status": "error"}
    except Exception as e:
        return {"output": "", "exit_code": -1, "error": f"SSH异常: {e}", "status": "error"}


# ── SSH命令解析 ───────────────────────────────────

def _parse_ssh_target(text: str) -> tuple:
    """解析 user@host[:port]"""
    parts = text.split()[0] if " " in text else text
    if "@" not in parts:
        return None, None, 22
    user, host_part = parts.split("@", 1)
    if ":" in host_part:
        host, port_str = host_part.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            host, port = host_part, 22
    else:
        host, port = host_part, 22
    return user, host, port


# ── 配置向导 ──────────────────────────────────────

def _wizard_llm() -> dict:
    print()
    print("┌" + "─" * 50 + "┐")
    print("│  🔧 LLM 配置向导" + " " * 32 + "│")
    print("├" + "─" * 50 + "┤")
    print("│  1. 提供商 2. 模型 3. API地址 4. API Key    │")
    print("└" + "─" * 50 + "┘")
    print()
    provider = input("  提供商: ").strip()
    model = input("  模型名: ").strip()
    base_url = input("  API地址: ").strip()
    api_key = input("  API Key: ").strip()
    if not all([provider, model, base_url, api_key]):
        return {"output": "❌ 配置取消：所有字段都是必填的", "status": "error"}
    cfg = _load_config()
    cfg["llm"] = {"provider": provider, "model": model, "base_url": base_url, "api_key": api_key}
    _save_config(cfg)
    return {"output": f"✅ LLM配置完成\n  提供商: {provider}\n  模型: {model}\n  配置已写入 data/config.json", "exit_code": 0, "error": "", "status": "ok"}


def _wizard_tg() -> dict:
    print()
    print("┌──────────────────────────────────┐")
    print("│  🔧 Telegram Bot 配置向导         │")
    print("│  请提供TG Bot Token               │")
    print("└──────────────────────────────────┘")
    print()
    token = input("  Bot Token: ").strip()
    if not token:
        return {"output": "❌ 配置取消：Token是必填的", "status": "error"}
    cfg = _load_config()
    cfg["tg"] = {"bot_token": token}
    _save_config(cfg)
    return {"output": "✅ TG Bot配置完成\n  运行 '终端 tg启动' 启动Bot", "exit_code": 0, "error": "", "status": "ok"}


def _wizard_ssh() -> dict:
    print()
    print("┌" + "─" * 50 + "┐")
    print("│  🔧 SSH 配置向导" + " " * 32 + "│")
    print("├" + "─" * 50 + "┤")
    print("│  1. 主机 2. 用户名 3. 端口(默认22) 4. 密钥(可选) │")
    print("└" + "─" * 50 + "┘")
    print()
    host = input("  主机: ").strip()
    user = input("  用户名: ").strip()
    port_str = input("  端口 [22]: ").strip()
    key = input("  密钥路径 [可选]: ").strip()
    if not host or not user:
        return {"output": "❌ 主机和用户名是必填的", "status": "error"}
    port = int(port_str) if port_str else 22
    cfg = _load_config()
    cfg["ssh"] = {"ssh_host": host, "ssh_user": user, "ssh_port": port, "ssh_key": key}
    _save_config(cfg)
    return {"output": f"✅ SSH配置完成\n  目标: {user}@{host}:{port}", "exit_code": 0, "error": "", "status": "ok"}


# ── 入口 ─────────────────────────────────────────

def handle(text: str) -> str:
    """工具入口。core.py 通过此函数调用。返回JSON字符串。"""
    if not isinstance(text, str):
        return json.dumps({
            "output": "", "exit_code": -1,
            "error": f"Invalid: expected string, got {type(text).__name__}",
            "status": "error",
        }, ensure_ascii=False)

    text = text.strip()
    if not text:
        return json.dumps(_show_help(), ensure_ascii=False)

    # ── 配置向导 ──
    if text == "配置llm":
        return json.dumps(_wizard_llm(), ensure_ascii=False)
    if text == "配置tg":
        return json.dumps(_wizard_tg(), ensure_ascii=False)
    if text == "配置ssh":
        return json.dumps(_wizard_ssh(), ensure_ascii=False)

    # ── TG ──
    if text == "tg启动":
        cfg = _load_config()
        token = cfg.get("tg", {}).get("bot_token", "")
        if not token:
            return json.dumps({"output": "❌ TG Bot未配置。请先运行 '终端 配置tg'", "exit_code": -1, "error": "TG未配置", "status": "error"}, ensure_ascii=False)
        return json.dumps({"output": "✅ TG Bot token已配置。在主进程中通过 main.py --tg 启动", "exit_code": 0, "error": "", "status": "ok"}, ensure_ascii=False)

    # ── 帮助 ──
    if text in ("help", "帮助"):
        return json.dumps(_show_help(), ensure_ascii=False)

    # ── 查看配置 ──
    if text == "查看配置":
        cfg = _load_config()
        safe_cfg = {}
        if "llm" in cfg:
            safe_cfg["llm"] = {k: ("***" if k == "api_key" else v) for k, v in cfg["llm"].items()}
        if "tg" in cfg:
            safe_cfg["tg"] = {k: ("***" if "token" in k else v) for k, v in cfg["tg"].items()}
        if "ssh" in cfg:
            safe_cfg["ssh"] = {k: ("***" if k == "ssh_key" else v) for k, v in cfg["ssh"].items()}
        return json.dumps({"output": json.dumps(safe_cfg, indent=2, ensure_ascii=False), "exit_code": 0, "error": "", "status": "ok"}, ensure_ascii=False)

    # ── 后台进程管理 ──
    if text.startswith("bg:spawn "):
        cmd = text[9:].strip()
        try:
            session = process_manager.spawn(cmd)
            return json.dumps({
                "output": f"后台进程已启动\n  session_id: {session.id}\n  PID: {session.pid}\n  \n管理命令:\n  终端 bg:poll {session.id}\n  终端 bg:log {session.id}\n  终端 bg:kill {session.id}\n  终端 bg:wait {session.id}",
                "exit_code": 0, "error": "", "status": "background",
                "session_id": session.id, "pid": session.pid,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"output": "", "exit_code": -1, "error": f"后台启动失败: {e}", "status": "error"}, ensure_ascii=False)

    if text.startswith("bg:poll "):
        sid = text[8:].strip()
        info = process_manager.poll(sid)
        if "error" in info:
            return json.dumps({"output": info["error"], "exit_code": -1, "error": info["error"], "status": "error"}, ensure_ascii=False)
        return json.dumps({"output": json.dumps(info, indent=2, ensure_ascii=False), "exit_code": 0, "error": "", "status": "ok"}, ensure_ascii=False)

    if text.startswith("bg:log "):
        parts = text[7:].strip().split(maxsplit=1)
        sid = parts[0]
        lines = int(parts[1]) if len(parts) > 1 else 200
        info = process_manager.log(sid, lines)
        if "error" in info:
            return json.dumps({"output": info["error"], "exit_code": -1, "error": info["error"], "status": "error"}, ensure_ascii=False)
        return json.dumps({"output": f"[{info['status']}] PID: {info.get('pid', '?')}\n\n{info['output']}", "exit_code": 0, "error": "", "status": "ok"}, ensure_ascii=False)

    if text.startswith("bg:kill "):
        sid = text[8:].strip()
        info = process_manager.kill(sid)
        if "error" in info:
            return json.dumps({"output": info["error"], "exit_code": -1, "error": info["error"], "status": "error"}, ensure_ascii=False)
        return json.dumps({"output": f"已终止 (PID: {info.get('pid')})", "exit_code": 0, "error": "", "status": "ok"}, ensure_ascii=False)

    if text.startswith("bg:wait "):
        parts = text[8:].strip().split(maxsplit=1)
        sid = parts[0]
        timeout = int(parts[1]) if len(parts) > 1 else 300
        info = process_manager.wait(sid, timeout)
        if "error" in info:
            return json.dumps({"output": info["error"], "exit_code": -1, "error": info["error"], "status": "error"}, ensure_ascii=False)
        return json.dumps({"output": json.dumps(info, indent=2, ensure_ascii=False), "exit_code": info.get("exit_code", 0), "error": "", "status": "ok"}, ensure_ascii=False)

    if text == "bg:list":
        sessions = process_manager.list_sessions()
        if not sessions:
            return json.dumps({"output": "没有后台进程", "exit_code": 0, "error": "", "status": "ok"}, ensure_ascii=False)
        lines = [f"{'SESSION_ID':<20} {'PID':<8} {'状态':<12} 命令"]
        lines.append("-" * 60)
        for s in sessions:
            lines.append(f"{s['id']:<20} {s['pid']:<8} {s['status']:<12} {s['command'][:40]}")
        return json.dumps({"output": "\n".join(lines), "exit_code": 0, "error": "", "status": "ok"}, ensure_ascii=False)

    # ── SSH ──
    if text.startswith("ssh "):
        ssh_cmd = text[4:].strip()
        cfg = _load_config()
        ssh_cfg = cfg.get("ssh", {})

        # 如果第一个参数是user@host格式，解析它
        if "@" in ssh_cmd.split()[0] if ssh_cmd else False:
            user, host, port = _parse_ssh_target(ssh_cmd)
            parts = ssh_cmd.split(None, 1)
            remote_cmd = parts[1] if len(parts) > 1 else "pwd"
            key = ssh_cfg.get("ssh_key", "")
            return json.dumps(_run_ssh(host, user, remote_cmd, port, key), ensure_ascii=False)
        else:
            # 从配置读取
            host = ssh_cfg.get("ssh_host") or os.getenv("TERMINAL_SSH_HOST", "")
            user = ssh_cfg.get("ssh_user") or os.getenv("TERMINAL_SSH_USER", "")
            port = int(ssh_cfg.get("ssh_port") or os.getenv("TERMINAL_SSH_PORT", "22"))
            key = ssh_cfg.get("ssh_key", "")
            if not host or not user:
                return json.dumps({"output": "SSH未配置。使用 '终端 配置ssh' 设置", "exit_code": -1, "error": "SSH not configured", "status": "error"}, ensure_ascii=False)
            return json.dumps(_run_ssh(host, user, ssh_cmd + " pwd" if not ssh_cmd else ssh_cmd, port, key), ensure_ascii=False)

    # ── 危险命令拦截 ──
    danger = _check_dangerous(text)
    if danger:
        return json.dumps({"output": danger, "exit_code": -1, "error": danger, "status": "blocked"}, ensure_ascii=False)

    # ── 本地shell执行 ──
    return json.dumps(_run_local(text), ensure_ascii=False)


def _show_help() -> dict:
    help_text = """
┌──────────────────────────────────────────────────────────┐
│                   终端工具 — 使用说明                       │
├──────────────────────────────────────────────────────────┤
│  终端 <命令>              执行shell命令（本地）              │
│  终端 ssh user@host 命令   远程SSH执行                      │
│  终端 配置llm             交互式配置LLM                      │
│  终端 配置tg              交互式配置Telegram Bot             │
│  终端 配置ssh             交互式配置SSH                      │
│  终端 tg启动              查看TG启动方式                     │
│  终端 查看配置             查看当前配置（密钥已隐藏）           │
│  终端 help                显示此帮助                         │
│                                                              │
│  ── 后台进程管理 ──                                          │
│  终端 bg:spawn <命令>     启动后台进程 → 返回session_id        │
│  终端 bg:poll <sid>       查询后台进程状态                     │
│  终端 bg:log <sid> [N]    查看后台进程输出（默认200行）         │
│  终端 bg:kill <sid>       终止后台进程                        │
│  终端 bg:wait <sid> [秒]  阻塞等待后台进程结束（默认300s）      │
│  终端 bg:list             列出所有后台进程                     │
└──────────────────────────────────────────────────────────┘
"""
    return {"output": help_text.strip(), "exit_code": 0, "error": "", "status": "ok"}


# ── 独立测试 ─────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = " ".join(sys.argv[1:])
        print(handle(cmd))
    else:
        print("用法: python3 terminal.py <命令>")
