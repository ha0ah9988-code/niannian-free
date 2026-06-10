#!/usr/bin/env python3
"""
bridge.py — 念念外部桥接层
============================
所有外部调用通过这里——
  "借脑"就是借这里的通道。

职责：
  1. call_llm()     — 借LLM的脑（OpenAI兼容API）
  2. call_hermes()  — 借Hermes的脑（CLI子进程）
  3. call_openclaw()— 借OpenClaw的脑（CLI子进程）
  4. send_tg()      — TG Bot消息发送
  5. has_llm()      — 检查LLM是否已配置

设计要点：
  - 所有外部调用统一入口——换provider只改这一个文件
  - 错误处理：调用失败优雅降级，不崩溃
  - 密钥安全：不从日志暴露API key
"""

import json
import os
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


class Bridge:
    """念念的外部桥接器。"""

    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化桥接器。
        参数:
            config_path: config.json的路径，默认 niannian-core/data/config.json
        """
        if config_path is None:
            config_path = Path(__file__).resolve().parent / "data" / "config.json"
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self) -> dict:
        """加载配置。"""
        if self.config_path.exists():
            with open(self.config_path) as f:
                return json.load(f)
        return {}

    def has_llm(self) -> bool:
        """检查LLM是否已配置（每次从文件读取，支持运行时更新）。"""
        self.config = self._load_config()  # 实时重读
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

        参数:
            system_context: 系统上下文（身份+规则+记忆）
            messages: 对话消息列表 [{"role": "user", "content": "..."}]
            tools: 工具定义列表（OpenAI tool格式），可选

        返回:
            {"content": "LLM回复", "tool_calls": [...]} 或 {"error": "..."}

        tool_calls格式: [{"name": "terminal", "arguments": {"command": "ls"}}]
        """
        llm = self.config.get("llm", {})
        if not llm.get("api_key"):
            return {"error": "LLM未配置", "content": "需要先配置LLM。运行 '终端 配置llm'"}

        provider = llm.get("provider", "opencode")
        model = llm.get("model", "")
        base_url = llm.get("base_url", "https://api.openai.com/v1")
        api_key = llm.get("api_key", "")

        # 构建请求体
        api_messages = [{"role": "system", "content": system_context}]
        api_messages.extend(messages)

        body = {
            "model": model,
            "messages": api_messages,
            "temperature": 0.7,
            "max_tokens": 16384,
            "thinking": {"type": "disabled"},
        }

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
                "User-Agent": "niannian-origin/0.1",
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
