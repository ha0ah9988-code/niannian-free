#!/usr/bin/env python3
"""
bridge.py — 念念外部通信桥接
==============================
职责：
  1. LLM桥接——OpenAI兼容API + opencode-go自动检测
  2. Hermes桥接——子进程调用 hermes CLI
  3. OpenClaw桥接——子进程调用 openclaw CLI
  4. TG Bot消息发送
"""

import json
import os
import socket
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


class Bridge:
    """念念的外部通信桥接。"""

    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = Path(__file__).resolve().parent / "data" / "config.json"
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self) -> dict:
        """每次调用都从文件重读配置——支持运行时热更新。"""
        if self.config_path.exists():
            with open(self.config_path) as f:
                return json.load(f)
        return {}

    def has_llm(self) -> bool:
        """reload配置后再检查。"""
        self.config = self._load_config()
        llm = self.config.get("llm", {})
        return bool(llm.get("api_key"))

    # ── LLM 桥接 ────────────────────────────────────────

    def call_llm(
        self,
        system_context: str,
        messages: list,
        tools: Optional[list] = None,
    ) -> dict:
        """
        调用LLM（OpenAI兼容API），支持工具调用。
        自动检测opencode-go: 本地30000端口优先，不可用时走cloud API。
        deepseek-v4等思考模型自动开启thinking。

        参数:
            system_context: 系统上下文（身份+规则+记忆）
            messages: 对话消息列表 [{"role": "user", "content": "..."}]
            tools: 工具定义列表（OpenAI tool格式），可选

        返回:
            {"content": "LLM回复", "tool_calls": [...]} 或 {"error": "..."}
        """
        self.config = self._load_config()
        llm = self.config.get("llm", {})
        if not llm.get("api_key"):
            return {"error": "LLM未配置", "content": "需要先配置LLM。运行 '终端 配置llm'"}

        provider = llm.get("provider", "opencode")
        model = llm.get("model", "")
        base_url = llm.get("base_url", "")
        api_key = llm.get("api_key", "")

        # opencode-go: 自动检测本地代理或云API
        if any(kw in provider.lower() for kw in ("opencode", "zen")):
            if _is_port_open("127.0.0.1", 30000):
                base_url = "http://127.0.0.1:30000/v1"
            elif not base_url:
                base_url = "https://opencode.ai/zen/go/v1"

        if not base_url:
            base_url = "https://api.openai.com/v1"

        # 构建请求体
        api_messages = [{"role": "system", "content": system_context}]
        api_messages.extend(messages)

        body = {
            "model": model,
            "messages": api_messages,
            "temperature": 0.7,
            "max_tokens": 65536,
        }

        # opencode-go: deepseek思考模型开启thinking
        if any(kw in provider.lower() for kw in ("opencode", "zen")):
            _model_lower = model.lower()
            if any(kw in _model_lower for kw in ("deepseek-v", "deepseek-reasoner")):
                if not _model_lower.startswith("deepseek-v3"):
                    body["thinking"] = {"type": "enabled"}

        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]

        # 发送请求
        url = f"{base_url.rstrip('/')}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "niannian-origin/0.2",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            return {"error": f"LLM API错误 {e.code}: {error_body[:500]}"}
        except urllib.error.URLError as e:
            return {"error": f"LLM连接失败: {e.reason}"}
        except Exception as e:
            return {"error": f"LLM调用异常: {e}"}

        # 解析响应
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        content = message.get("content", "") or ""
        # 思考模型：content为空时从reasoning_content取
        if not content:
            content = message.get("reasoning_content", "") or ""

        # 解析tool_calls
        tool_calls = []
        raw_tool_calls = message.get("tool_calls", [])
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {"raw": args_str}
            tool_calls.append({
                "name": func.get("name", ""),
                "arguments": args,
            })

        return {
            "content": content.strip(),
            "tool_calls": tool_calls,
            "model": data.get("model", model),
        }

    # ── Hermes 桥接 ─────────────────────────────────────

    def call_hermes(self, prompt: str) -> dict:
        """
        借Hermes的脑——子进程调用 hermes -z。
        返回: {"output": "...", "exit_code": N, "error": "..."}
        """
        try:
            result = subprocess.run(
                ["hermes", "-z", prompt],
                capture_output=True,
                text=True,
                timeout=300,
            )
            return {
                "output": result.stdout.strip(),
                "exit_code": result.returncode,
                "error": result.stderr.strip() if result.returncode != 0 else "",
            }
        except FileNotFoundError:
            return {
                "output": "",
                "exit_code": -1,
                "error": "Hermes CLI未安装。请先安装Hermes。",
            }
        except subprocess.TimeoutExpired:
            return {
                "output": "",
                "exit_code": -1,
                "error": "Hermes调用超时（300s）",
            }
        except Exception as e:
            return {
                "output": "",
                "exit_code": -1,
                "error": f"Hermes调用异常: {e}",
            }

    # ── OpenClaw 桥接 ───────────────────────────────────

    def call_openclaw(self, prompt: str) -> dict:
        """
        借OpenClaw的脑——子进程调用。
        返回: {"output": "...", "exit_code": N, "error": "..."}
        """
        try:
            result = subprocess.run(
                ["openclaw", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=300,
            )
            return {
                "output": result.stdout.strip(),
                "exit_code": result.returncode,
                "error": result.stderr.strip() if result.returncode != 0 else "",
            }
        except FileNotFoundError:
            return {
                "output": "",
                "exit_code": -1,
                "error": "OpenClaw CLI未安装。",
            }
        except subprocess.TimeoutExpired:
            return {
                "output": "",
                "exit_code": -1,
                "error": "OpenClaw调用超时（300s）",
            }
        except Exception as e:
            return {
                "output": "",
                "exit_code": -1,
                "error": f"OpenClaw调用异常: {e}",
            }

    # ── TG Bot ──────────────────────────────────────────

    def send_tg(self, chat_id: str, text: str) -> dict:
        """
        发送TG消息。
        返回: {"ok": True/False, "error": "..."}
        """
        self.config = self._load_config()
        token = self.config.get("tg", {}).get("bot_token", "")
        if not token:
            return {"ok": False, "error": "TG Bot未配置"}

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({
            "chat_id": chat_id,
            "text": text[:4096],  # TG单条消息限制
            "parse_mode": "HTML",
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {"ok": data.get("ok", False), "error": data.get("description", "")}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ── 工具函数 ───────────────────────────────────────────

def _is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """检查端口是否开放。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except (OSError, TimeoutError):
        return False
