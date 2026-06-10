#!/usr/bin/env python3
"""
main.py — 念念源代码系统的唯一入口
=======================================
三种启动模式：
  python3 main.py           → CLI 交互模式
  python3 main.py --tg      → Telegram Bot 模式
  python3 main.py --serve   → (未来) MCP Server 模式

用法：
  cd niannian-origin/niannian-core
  python3 main.py              # CLI模式
  python3 main.py --tg         # TG Bot模式（需先配置TG）
"""

import argparse
import json
import sys
from pathlib import Path

# 设置路径
CORE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE_DIR))

from core import process_input, get_tools, get_identity, _get_help_text
from bridge import Bridge


def _banner():
    """打印启动横幅。"""
    print(r"""
   ╔══════════════════════════════════════╗
   ║      念念源代码系统 v0.1             ║
   ║      niannian-origin                ║
   ║      借脑写脑 · 先为人再为己         ║
   ╚══════════════════════════════════════╝
""")


def cli_mode(bridge: Bridge, tools: dict, identity):
    """CLI交互模式。"""
    _banner()

    # 检查LLM状态
    if bridge.has_llm():
        llm = bridge.config.get("llm", {})
        print(f"✅ LLM已配置: {llm.get('model', 'unknown')}")
    else:
        print("⚠ LLM未配置。输入 '终端 配置llm' 开始配置。")

    print(f"📦 已加载 {len(tools)} 个工具")
    print(f"输入 'help' 查看命令，Ctrl+C 退出。")
    print()

    while True:
        try:
            user_input = input("念念> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见。")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "退出"):
            print("👋 再见。")
            break

        response = process_input(user_input, bridge, tools, identity)
        print(response)
        print()


def tg_bot_mode(bridge: Bridge, tools: dict, identity):
    """Telegram Bot polling模式。"""
    import time
    import urllib.request

    token = bridge.config.get("tg", {}).get("bot_token", "")
    if not token:
        print("❌ TG Bot未配置。先在CLI模式下运行 '终端 配置tg'")
        sys.exit(1)

    print("🤖 启动Telegram Bot...")
    print(f"📦 已加载 {len(tools)} 个工具")

    offset = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": offset, "timeout": 30}
            query = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{url}?{query}"

            req = urllib.request.Request(full_url)
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if not data.get("ok"):
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()

                if not chat_id or not text:
                    continue

                print(f"[TG] {chat_id}: {text[:80]}")
                response = process_input(text, bridge, tools, identity)

                # 发送回复
                bridge.send_tg(chat_id, response)

        except (KeyboardInterrupt, SystemExit):
            print("\n👋 TG Bot已停止。")
            break
        except Exception as e:
            print(f"[TG] 轮询错误: {e}")
            time.sleep(5)


# ── 主入口 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="念念源代码系统")
    parser.add_argument("--tg", action="store_true", help="TG Bot模式")
    parser.add_argument("--serve", action="store_true", help="MCP Server模式（预留）")
    args = parser.parse_args()

    # 初始化
    bridge = Bridge()
    tools = get_tools()
    identity = get_identity()

    if args.serve:
        print("MCP Server模式尚未实现")
        sys.exit(0)

    if args.tg:
        tg_bot_mode(bridge, tools, identity)
    else:
        cli_mode(bridge, tools, identity)


if __name__ == "__main__":
    main()
