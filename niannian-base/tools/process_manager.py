#!/usr/bin/env python3
"""
process_manager.py — 念念后台进程管理器

提供：
  - ProcessManager: 后台进程生命周期管理（spawn/poll/log/kill/wait/list）

设计要点（来自Hermes process_registry，大幅简化）：
  - 内存注册表：track Popen对象 + 输出缓冲区（200KB滚动窗口）
  - 完整生命周期：spawn→poll→log→kill→wait
  - 线程安全：所有操作持有锁
"""

import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ansi_strip import strip_ansi

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 200_000
FINISHED_TTL_SECONDS = 1800
MAX_PROCESSES = 64

_STAGGER_FILE = None  # Path for checkpoint persistence, set by init if needed


@dataclass
class BgSession:
    """一个被追踪的后台进程。"""
    id: str
    command: str
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    started_at: float = 0.0
    output: List[str] = field(default_factory=list)
    _output_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def is_running(self) -> bool:
        if not self.process:
            return False
        return self.process.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        if not self.process:
            return None
        return self.process.poll()

    def append_output(self, text: str):
        with self._output_lock:
            self.output.append(text)
            total = sum(len(l) for l in self.output)
            while total > MAX_OUTPUT_CHARS and self.output:
                total -= len(self.output.pop(0))

    def get_output(self, lines: int = 200) -> str:
        with self._output_lock:
            recent = self.output[-lines:]
            return "".join(recent)


class ProcessManager:
    """念念后台进程管理器 —— 单例。"""

    def __init__(self):
        self._sessions: Dict[str, BgSession] = {}
        self._lock = threading.Lock()

    # ── spawn ─────────────────────────────────────────

    def spawn(self, command: str, workdir: str = "") -> BgSession:
        """启动后台进程，返回session对象。"""
        with self._lock:
            # LRU裁剪
            if len(self._sessions) >= MAX_PROCESSES:
                oldest = min(self._sessions.values(),
                            key=lambda s: s.started_at)
                self._kill_and_remove(oldest.id)

            sid = "bg_" + uuid.uuid4().hex[:12]
            try:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=workdir or None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    preexec_fn=os.setsid,
                )
            except Exception as e:
                raise RuntimeError(f"后台进程启动失败: {e}") from e

            session = BgSession(
                id=sid,
                command=command,
                process=proc,
                pid=proc.pid,
                started_at=time.time(),
            )
            self._sessions[sid] = session

            # 启动输出收集线程
            t = threading.Thread(target=self._drain_output, args=(session,), daemon=True)
            t.start()

            return session

    def _drain_output(self, session: BgSession):
        """非阻塞收集后台进程输出。"""
        try:
            if not session.process or not session.process.stdout:
                return
            for line in session.process.stdout:
                cleaned = strip_ansi(line)
                session.append_output(cleaned)
        except (ValueError, OSError):
            pass

    # ── poll ──────────────────────────────────────────

    def poll(self, session_id: str) -> dict:
        """查询后台进程状态。"""
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {"error": f"session_id 不存在: {session_id}", "status": "not_found"}

        rc = session.returncode
        uptime = time.time() - session.started_at
        preview = session.get_output(10)

        if rc is None:
            return {
                "status": "running",
                "pid": session.pid,
                "uptime_seconds": int(uptime),
                "preview": preview,
            }
        else:
            return {
                "status": "exited",
                "pid": session.pid,
                "exit_code": rc,
                "uptime_seconds": int(uptime),
                "preview": preview,
            }

    # ── log ───────────────────────────────────────────

    def log(self, session_id: str, lines: int = 200) -> dict:
        """获取后台进程输出日志。"""
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {"error": f"session_id 不存在: {session_id}", "status": "not_found"}

        output = session.get_output(lines)
        status = "running" if session.is_running else f"exited({session.returncode})"
        return {
            "status": status,
            "pid": session.pid,
            "lines": min(lines, len(session.output)),
            "output": output if output else "(无输出)",
        }

    # ── kill ──────────────────────────────────────────

    def kill(self, session_id: str) -> dict:
        """终止后台进程（process group kill）。"""
        session = None
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if not session:
            return {"error": f"session_id 不存在: {session_id}", "status": "not_found"}

        self._kill_and_remove_impl(session)
        return {"status": "killed", "pid": session.pid}

    def _kill_and_remove(self, session_id: str):
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            self._kill_and_remove_impl(session)

    def _kill_and_remove_impl(self, session: BgSession):
        if not session.process or session.process.poll() is not None:
            return
        try:
            pgid = os.getpgid(session.process.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                session.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                try:
                    session.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        except (ProcessLookupError, OSError):
            pass

    # ── wait ──────────────────────────────────────────

    def wait(self, session_id: str, timeout: int = 300) -> dict:
        """阻塞等待后台进程结束。"""
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {"error": f"session_id 不存在: {session_id}", "status": "not_found"}

        if not session.process:
            return {"status": "exited", "exit_code": None}

        try:
            rc = session.process.wait(timeout=timeout)
            output = session.get_output(200)
            return {
                "status": "exited",
                "pid": session.pid,
                "exit_code": rc,
                "output": output if output else "(无输出)",
            }
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "pid": session.pid, "error": f"等待超时 ({timeout}s)"}

    # ── list ──────────────────────────────────────────

    def list_sessions(self) -> list:
        """列出所有后台进程。"""
        with self._lock:
            result = []
            for sid, s in list(self._sessions.items()):
                rc = s.returncode
                status = "running" if rc is None else f"exited({rc})"
                result.append({
                    "id": sid,
                    "pid": s.pid,
                    "status": status,
                    "command": s.command[:60],
                    "uptime_seconds": int(time.time() - s.started_at),
                })
            return result

    # ── cleanup ───────────────────────────────────────

    def cleanup_stale(self):
        """清理已结束超过TTL的进程。"""
        now = time.time()
        with self._lock:
            stale = []
            for sid, s in self._sessions.items():
                if not s.is_running and now - s.started_at > FINISHED_TTL_SECONDS:
                    stale.append(sid)
            for sid in stale:
                self._sessions.pop(sid, None)


# ── 全局单例 ─────────────────────────────────────────

process_manager = ProcessManager()
