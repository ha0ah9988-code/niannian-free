#!/usr/bin/env python3
"""
environments.py — 念念执行环境模块（去掉所有Hermes内部依赖）

提供：
  - BaseEnvironment: 抽象基类（_wait_for_process, _wrap_command, execute, CWD跟踪）
  - LocalEnvironment: 本地bash执行（session snapshot, process group kill）
  - SSHEnvironment: SSH远程执行（ControlMaster连接复用）
  - FileSyncManager: 远程文件同步（SSH用，mtime变化检测+事务性上传）

核心设计（来自Hermes验证过的模式）：
  - spawn-per-call：每次execute()启动新bash进程
  - select()非阻塞输出排空：解决后台进程管道不关闭导致的永久挂起
  - process group kill：SIGTERM→SIGKILL两级，确保孤儿进程不泄漏
  - session snapshot：首次init捕获login shell环境，后续命令source复用
"""

import codecs
import hashlib
import json
import logging
import os
import platform
import re
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from abc import ABC, abstractmethod
from typing import IO, Callable, Optional, Dict

# 内部依赖
from shell_utils import _rewrite_compound_background, _transform_sudo_command

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# ── 中断检查（念念版：默认不中断） ───────────────────
_interrupted_event = threading.Event()

def set_interrupted():
    """外部调用此函数标记中断。"""
    _interrupted_event.set()

def clear_interrupted():
    _interrupted_event.clear()

def is_interrupted() -> bool:
    return _interrupted_event.is_set()


# ═══════════════════════════════════════════════════════
#  通用工具函数
# ═══════════════════════════════════════════════════════

def _pipe_stdin(proc: subprocess.Popen, data: str) -> None:
    """非阻塞写入stdin，避免管道缓冲区死锁。"""
    def _write():
        try:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            target = getattr(proc.stdin, "buffer", proc.stdin)
            target.write(raw)
            target.close()
        except (BrokenPipeError, OSError):
            pass
    threading.Thread(target=_write, daemon=True).start()


# ═══════════════════════════════════════════════════════
#  CWD marker（远程后端用）
# ═══════════════════════════════════════════════════════

def _cwd_marker(session_id: str) -> str:
    return f"__NN_CWD_{session_id}__"


# ═══════════════════════════════════════════════════════
#  BaseEnvironment
# ═══════════════════════════════════════════════════════

class BaseEnvironment(ABC):
    """统一执行接口。子类实现 _run_bash() 和 cleanup()。"""

    def get_temp_dir(self) -> str:
        return "/tmp"

    def __init__(self, cwd: str, timeout: int, env: dict = None):
        self.cwd = cwd
        self.timeout = timeout
        self.env = env or {}
        self._session_id = uuid.uuid4().hex[:12]
        temp_dir = self.get_temp_dir().rstrip("/") or "/"
        self._snapshot_path = f"{temp_dir}/nn-snap-{self._session_id}.sh"
        self._cwd_file = f"{temp_dir}/nn-cwd-{self._session_id}.txt"
        self._cwd_marker = _cwd_marker(self._session_id)
        self._snapshot_ready = False

    @abstractmethod
    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120, stdin_data: str | None = None):
        ...

    @abstractmethod
    def cleanup(self):
        ...

    # ── session snapshot ─────────────────────────────

    def init_session(self):
        """捕获login shell环境到snapshot文件。"""
        _quoted_cwd = shlex.quote(self.cwd)
        _quoted_snap = shlex.quote(self._snapshot_path)
        _quoted_cwd_file = shlex.quote(self._cwd_file)
        bootstrap = (
            f"export -p > {_quoted_snap}\n"
            f"declare -f | grep -vE '^_[^_]' >> {_quoted_snap}\n"
            f"alias -p >> {_quoted_snap}\n"
            f"echo 'shopt -s expand_aliases' >> {_quoted_snap}\n"
            f"echo 'set +e' >> {_quoted_snap}\n"
            f"echo 'set +u' >> {_quoted_snap}\n"
            f"builtin cd {_quoted_cwd} 2>/dev/null || true\n"
            f"pwd -P > {_quoted_cwd_file} 2>/dev/null || true\n"
            f"printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' \"$(pwd -P)\"\n"
        )
        try:
            proc = self._run_bash(bootstrap, login=True, timeout=30)
            result = self._wait_for_process(proc, timeout=30)
            self._snapshot_ready = True
            self._update_cwd(result)
        except Exception:
            self._snapshot_ready = False

    # ── command wrapping ─────────────────────────────

    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        if cwd == "~":
            return cwd
        if cwd == "~/":
            return "$HOME"
        if cwd.startswith("~/"):
            return f"$HOME/{shlex.quote(cwd[2:])}"
        return shlex.quote(cwd)

    def _wrap_command(self, command: str, cwd: str) -> str:
        escaped = command.replace("'", "'\\''")
        _quoted_snap = shlex.quote(self._snapshot_path)
        _quoted_cwd_file = shlex.quote(self._cwd_file)
        parts = []
        if self._snapshot_ready:
            parts.append(f"source {_quoted_snap} >/dev/null 2>&1 || true")
        quoted_cwd = self._quote_cwd_for_cd(cwd)
        parts.append(f"builtin cd -- {quoted_cwd} || exit 126")
        parts.append(f"eval '{escaped}'")
        parts.append("__nn_ec=$?")
        if self._snapshot_ready:
            parts.append(f"export -p > {_quoted_snap} 2>/dev/null || true")
        parts.append(f"pwd -P > {_quoted_cwd_file} 2>/dev/null || true")
        parts.append(f"printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' \"$(pwd -P)\"")
        parts.append("exit $__nn_ec")
        return "\n".join(parts)

    # ── process lifecycle ────────────────────────────

    def _wait_for_process(self, proc, timeout: int = 120) -> dict:
        """select()非阻塞输出排空 + 中断检测 + 超时终止。"""
        output_chunks: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = time.monotonic() + timeout

        def _drain():
            fd = proc.stdout.fileno()
            if os.name == "nt":
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            break
                        output_chunks.append(decoder.decode(chunk))
                except (ValueError, OSError):
                    pass
                finally:
                    try:
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            output_chunks.append(tail)
                    except Exception:
                        pass
                return
            idle_after_exit = 0
            try:
                while True:
                    try:
                        ready, _, _ = select.select([fd], [], [], 0.1)
                    except (ValueError, OSError):
                        break
                    if ready:
                        try:
                            chunk = os.read(fd, 4096)
                        except (ValueError, OSError):
                            break
                        if not chunk:
                            break
                        output_chunks.append(decoder.decode(chunk))
                        idle_after_exit = 0
                    elif proc.poll() is not None:
                        idle_after_exit += 1
                        if idle_after_exit >= 3:
                            break
            finally:
                try:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        output_chunks.append(tail)
                except Exception:
                    pass

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()

        poll_sleep = 0.005
        try:
            while proc.poll() is None:
                if is_interrupted():
                    self._kill_process(proc)
                    drain_thread.join(timeout=2)
                    return {"output": "".join(output_chunks) + "\n[Command interrupted]", "returncode": 130}
                if time.monotonic() > deadline:
                    self._kill_process(proc)
                    drain_thread.join(timeout=2)
                    partial = "".join(output_chunks)
                    timeout_msg = f"\n[Command timed out after {timeout}s]"
                    return {"output": partial + timeout_msg if partial else timeout_msg.lstrip(), "returncode": 124}
                time.sleep(poll_sleep)
                if poll_sleep < 0.2:
                    poll_sleep = min(poll_sleep * 1.5, 0.2)
        except (KeyboardInterrupt, SystemExit):
            try:
                self._kill_process(proc)
                drain_thread.join(timeout=2)
            except Exception:
                pass
            raise

        drain_thread.join(timeout=2)
        try:
            proc.stdout.close()
        except Exception:
            pass
        return {"output": "".join(output_chunks), "returncode": proc.returncode}

    def _kill_process(self, proc):
        """终止进程（子类可重写为process group kill）。"""
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # ── CWD extraction ───────────────────────────────

    def _update_cwd(self, result: dict):
        self._extract_cwd_from_output(result)

    def _extract_cwd_from_output(self, result: dict):
        output = result.get("output", "")
        marker = self._cwd_marker
        last = output.rfind(marker)
        if last == -1:
            return
        search_start = max(0, last - 4096)
        first = output.rfind(marker, search_start, last)
        if first == -1 or first == last:
            return
        cwd_path = output[first + len(marker):last].strip()
        if cwd_path:
            self.cwd = cwd_path
        line_start = output.rfind("\n", 0, first)
        if line_start == -1:
            line_start = first
        line_end = output.find("\n", last + len(marker))
        line_end = line_end + 1 if line_end != -1 else len(output)
        result["output"] = output[:line_start] + output[line_end:]

    # ── execute ──────────────────────────────────────

    def execute(self, command: str, cwd: str = "", *,
                timeout: int | None = None, stdin_data: str | None = None) -> dict:
        """执行命令，返回 {"output": str, "returncode": int}。

        自动处理：
          - sudo密码注入（如设置了SUDO_PASSWORD环境变量）
          - 复合命令后台重写（A && B & → A && { B & }）
        """
        # sudo密码注入
        exec_command, sudo_stdin = _transform_sudo_command(command)
        if sudo_stdin is not None and stdin_data is not None:
            effective_stdin = sudo_stdin + stdin_data
        elif sudo_stdin is not None:
            effective_stdin = sudo_stdin
        else:
            effective_stdin = stdin_data

        # 复合命令后台重写（防止子shell永久挂起）
        exec_command = _rewrite_compound_background(exec_command)

        effective_timeout = timeout or self.timeout
        effective_cwd = cwd or self.cwd

        wrapped = self._wrap_command(exec_command, effective_cwd)
        login = not self._snapshot_ready

        proc = self._run_bash(wrapped, login=login, timeout=effective_timeout, stdin_data=effective_stdin)
        result = self._wait_for_process(proc, timeout=effective_timeout)
        self._update_cwd(result)
        return result

    def stop(self):
        self.cleanup()

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════
#  LocalEnvironment
# ═══════════════════════════════════════════════════════

def _find_bash() -> str:
    """查找bash路径。"""
    if _IS_WINDOWS:
        return shutil.which("bash") or "bash.exe"
    return (
        shutil.which("bash")
        or ("/usr/bin/bash" if os.path.isfile("/usr/bin/bash") else None)
        or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
        or os.environ.get("SHELL")
        or "/bin/sh"
    )


def _resolve_safe_cwd(cwd: str) -> str:
    """如果cwd不存在，向上查找最近存在的祖先目录。"""
    if cwd and os.path.isdir(cwd):
        return cwd
    parent = os.path.dirname(cwd) if cwd else ""
    while parent:
        if os.path.isdir(parent):
            return parent
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    return tempfile.gettempdir()


_SANE_PATH = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def _make_run_env(env: dict) -> dict:
    """构建子进程环境变量，注入sane PATH。"""
    merged = dict(os.environ | env)
    existing_path = merged.get("PATH", "")
    if not _IS_WINDOWS and "/usr/bin" not in existing_path.split(":"):
        merged["PATH"] = f"{existing_path}:{_SANE_PATH}" if existing_path else _SANE_PATH
    return merged


class LocalEnvironment(BaseEnvironment):
    """本地bash执行环境。"""

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict = None):
        if cwd:
            cwd = os.path.expanduser(cwd)
        super().__init__(cwd=cwd or os.getcwd(), timeout=timeout, env=env)
        self.init_session()

    def get_temp_dir(self) -> str:
        for env_var in ("TMPDIR", "TMP", "TEMP"):
            candidate = self.env.get(env_var) or os.environ.get(env_var)
            if candidate and candidate.startswith("/"):
                return candidate.rstrip("/") or "/"
        if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK | os.X_OK):
            return "/tmp"
        candidate = tempfile.gettempdir()
        if candidate.startswith("/"):
            return candidate.rstrip("/") or "/"
        return "/tmp"

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120, stdin_data: str | None = None):
        bash = _find_bash()
        args = [bash, "-l", "-c", cmd_string] if login else [bash, "-c", cmd_string]
        run_env = _make_run_env(self.env)

        safe_cwd = _resolve_safe_cwd(self.cwd)
        if safe_cwd != self.cwd:
            self.cwd = safe_cwd

        proc = subprocess.Popen(
            args,
            text=True,
            env=run_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
            cwd=self.cwd,
        )
        if not _IS_WINDOWS:
            try:
                proc._nn_pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pass
        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)
        return proc

    def _kill_process(self, proc):
        """SIGTERM→SIGKILL 两级process group kill。"""
        if _IS_WINDOWS:
            proc.terminate()
            return

        def _group_alive(pgid):
            try:
                os.killpg(pgid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True

        def _wait_for_group(pgid, timeout_s):
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    proc.poll()
                except Exception:
                    pass
                if not _group_alive(pgid):
                    return True
                time.sleep(0.05)
            try:
                proc.poll()
            except Exception:
                pass
            return not _group_alive(pgid)

        try:
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = getattr(proc, "_nn_pgid", None)
                if pgid is None:
                    raise
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                return
            if _wait_for_group(pgid, 1.0):
                return
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return
            _wait_for_group(pgid, 2.0)
            try:
                proc.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass

    def _update_cwd(self, result: dict):
        """从临时文件读取CWD（本地优化）。"""
        try:
            with open(self._cwd_file, encoding="utf-8") as f:
                cwd_path = f.read().strip()
            if cwd_path and os.path.isdir(cwd_path):
                self.cwd = cwd_path
        except (OSError, FileNotFoundError):
            pass
        self._extract_cwd_from_output(result)

    def cleanup(self):
        for f in (self._snapshot_path, self._cwd_file):
            try:
                os.unlink(f)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════
#  SSHEnvironment
# ═══════════════════════════════════════════════════════

def _ensure_ssh_available():
    if not shutil.which("ssh"):
        raise RuntimeError("SSH客户端未安装: apt install openssh-client")


class SSHEnvironment(BaseEnvironment):
    """SSH远程执行环境（ControlMaster连接复用）。"""

    def __init__(self, host: str, user: str, cwd: str = "~",
                 timeout: int = 60, port: int = 22, key_path: str = ""):
        super().__init__(cwd=cwd, timeout=timeout)
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path

        self.control_dir = Path(tempfile.gettempdir()) / "nn-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        _socket_id = hashlib.sha256(f"{user}@{host}:{port}".encode()).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"
        _ensure_ssh_available()
        self._establish_connection()

        self.init_session()

    def _build_ssh_command(self, extra_args: list = None) -> list:
        cmd = ["ssh"]
        cmd.extend(["-o", f"ControlPath={self.control_socket}"])
        cmd.extend(["-o", "ControlMaster=auto"])
        cmd.extend(["-o", "ControlPersist=300"])
        cmd.extend(["-o", "BatchMode=yes"])
        cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
        cmd.extend(["-o", "ConnectTimeout=10"])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _establish_connection(self):
        cmd = self._build_ssh_command()
        cmd.append("echo 'SSH connected'")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"SSH连接失败: {error_msg}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH连接超时: {self.user}@{self.host}")

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120, stdin_data: str | None = None):
        cmd = self._build_ssh_command()
        if login:
            cmd.extend(["bash", "-l", "-c", shlex.quote(cmd_string)])
        else:
            cmd.extend(["bash", "-c", shlex.quote(cmd_string)])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)
        return proc

    def cleanup(self):
        if self.control_socket.exists():
            try:
                cmd = ["ssh", "-o", f"ControlPath={self.control_socket}",
                       "-O", "exit", f"{self.user}@{self.host}"]
                subprocess.run(cmd, capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self.control_socket.unlink()
            except OSError:
                pass


# ═══════════════════════════════════════════════════════
#  FileSyncManager — 远程文件同步（SSH用）
# ═══════════════════════════════════════════════════════

_SYNC_INTERVAL_SECONDS = 5.0
_SYNC_BACK_MAX_RETRIES = 3
_SYNC_BACK_BACKOFF = (2, 4, 8)
_SYNC_BACK_MAX_BYTES = 2 * 1024 * 1024 * 1024

import tarfile as _tarfile_mod


def _file_mtime_key(host_path: str) -> tuple | None:
    try:
        st = Path(host_path).stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class FileSyncManager:
    """远程文件同步管理器。用于SSH等远程后端。"""

    def __init__(self, get_files_fn, upload_fn, delete_fn,
                 bulk_upload_fn=None, bulk_download_fn=None):
        self._get_files_fn = get_files_fn
        self._upload_fn = upload_fn
        self._delete_fn = delete_fn
        self._bulk_upload_fn = bulk_upload_fn
        self._bulk_download_fn = bulk_download_fn
        self._synced_files: dict = {}
        self._pushed_hashes: dict = {}
        self._last_sync_time: float = 0.0

    def sync(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_sync_time < _SYNC_INTERVAL_SECONDS:
            return
        current_files = self._get_files_fn()
        current_remote_paths = {remote for _, remote in current_files}
        to_upload = []
        new_files = dict(self._synced_files)
        for host_path, remote_path in current_files:
            file_key = _file_mtime_key(host_path)
            if file_key is None:
                continue
            if self._synced_files.get(remote_path) == file_key:
                continue
            to_upload.append((host_path, remote_path))
            new_files[remote_path] = file_key
        to_delete = [p for p in self._synced_files if p not in current_remote_paths]
        if not to_upload and not to_delete:
            self._last_sync_time = time.monotonic()
            return
        prev_files = dict(self._synced_files)
        prev_hashes = dict(self._pushed_hashes)
        try:
            if to_upload and self._bulk_upload_fn:
                self._bulk_upload_fn(to_upload)
            else:
                for host_path, remote_path in to_upload:
                    self._upload_fn(host_path, remote_path)
            if to_delete:
                self._delete_fn(to_delete)
            for host_path, remote_path in to_upload:
                self._pushed_hashes[remote_path] = _sha256_file(host_path)
            for p in to_delete:
                new_files.pop(p, None)
                self._pushed_hashes.pop(p, None)
            self._synced_files = new_files
            self._last_sync_time = time.monotonic()
        except Exception as exc:
            self._synced_files = prev_files
            self._pushed_hashes = prev_hashes
            self._last_sync_time = time.monotonic()
            logger.warning("file_sync: sync failed, rolled back: %s", exc)

    def sync_back(self, hermes_home=None) -> None:
        if self._bulk_download_fn is None:
            return
        if not self._pushed_hashes and not self._synced_files:
            return
        last_exc = None
        for attempt in range(_SYNC_BACK_MAX_RETRIES):
            try:
                self._sync_back_impl()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _SYNC_BACK_MAX_RETRIES - 1:
                    time.sleep(_SYNC_BACK_BACKOFF[attempt])
        logger.warning("sync_back: all %d attempts failed: %s", _SYNC_BACK_MAX_RETRIES, last_exc)

    def _sync_back_impl(self) -> None:
        file_mapping = list(self._get_files_fn())
        with tempfile.NamedTemporaryFile(suffix=".tar") as tf:
            self._bulk_download_fn(Path(tf.name))
            try:
                tar_size = os.path.getsize(tf.name)
            except OSError:
                tar_size = 0
            if tar_size > _SYNC_BACK_MAX_BYTES:
                return
            with tempfile.TemporaryDirectory(prefix="nn-sync-back-") as staging:
                with _tarfile_mod.open(tf.name) as tar:
                    tar.extractall(staging, filter="data")
                applied = 0
                for dirpath, _dirnames, filenames in os.walk(staging):
                    for fname in filenames:
                        staged_file = os.path.join(dirpath, fname)
                        rel = os.path.relpath(staged_file, staging)
                        remote_path = "/" + rel
                        pushed_hash = self._pushed_hashes.get(remote_path)
                        if pushed_hash and _sha256_file(staged_file) == pushed_hash:
                            continue
                        host_path = None
                        for host, remote in file_mapping:
                            if remote == remote_path:
                                host_path = host
                                break
                        if host_path is None:
                            continue
                        os.makedirs(os.path.dirname(host_path), exist_ok=True)
                        shutil.copy2(staged_file, host_path)
                        applied += 1
                if applied:
                    logger.info("sync_back: applied %d changed file(s)", applied)
