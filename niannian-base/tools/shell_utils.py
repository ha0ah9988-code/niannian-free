"""
shell_utils.py — Shell命令解析与安全重写

提供：
  - _read_shell_token: 读一个shell token（处理引号/转义）
  - _rewrite_compound_background: A && B & → A && { B & }（防止子shell永久挂起）
  - _rewrite_real_sudo_invocations: 将裸sudo重写为 sudo -S -p ''
  - _transform_sudo_command: sudo密码自动注入

来源：Hermes terminal_tool.py，去掉所有Hermes内部依赖。
"""

import os


def _read_shell_token(command: str, start: int) -> tuple[str, int]:
    """Read one shell token, preserving quotes/escapes, starting at *start*."""
    i = start
    n = len(command)

    while i < n:
        ch = command[i]
        if ch.isspace() or ch in ";|&()":
            break
        if ch == "'":
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                inner = command[i]
                if inner == "\\" and i + 1 < n:
                    i += 2
                    continue
                if inner == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1

    return command[start:i], i


def _looks_like_env_assignment(token: str) -> bool:
    """Heuristic: does *token* look like a VAR=value assignment?"""
    if "=" not in token:
        return False
    name = token.split("=", 1)[0]
    return name and not name[0].isdigit() and all(
        c.isalnum() or c == "_" for c in name
    )


# ═══════════════════════════════════════════════════════
#  复合命令后台重写
# ═══════════════════════════════════════════════════════

def _rewrite_compound_background(command: str) -> str:
    """Wrap `A && B &` (or `A || B &`) to `A && { B & }` at depth 0.

    Bash parses ``A && B &`` with `&&` tighter than `&`, so it forks a
    subshell for the whole `A && B` compound and backgrounds it. Inside
    the subshell, `B` runs foreground, so the subshell waits for `B` to
    finish. When `B` is a long-running process, the subshell never exits,
    and its open stdout pipe prevents the terminal tool from returning.

    Rewriting to `A && { B & }` preserves `&&`'s error semantics while
    replacing the subshell with a brace group that runs B as a normal
    backgrounded child.

    Handles redirects, quoted strings, parenthesised subshells.
    """
    n = len(command)
    i = 0
    paren_depth = 0
    brace_depth = 0
    last_chain_op_end = -1
    rewrites: list[tuple[int, int]] = []  # (chain_op_end, amp_pos)

    while i < n:
        ch = command[i]

        if ch == "\n" and paren_depth == 0 and brace_depth == 0:
            last_chain_op_end = -1
            i += 1
            continue

        if ch.isspace():
            i += 1
            continue

        if ch == "#":
            nl = command.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue

        if ch == "\\" and i + 1 < n:
            i += 2
            continue

        if ch in {"'", '"'}:
            _, next_i = _read_shell_token(command, i)
            i = max(next_i, i + 1)
            continue

        if ch == "(":
            paren_depth += 1
            i += 1
            continue

        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue

        if ch == "{" and i + 1 < n and (command[i + 1].isspace() or command[i + 1] == "\n"):
            brace_depth += 1
            i += 1
            continue

        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
            last_chain_op_end = -1
            i += 1
            continue

        if paren_depth > 0 or brace_depth > 0:
            i += 1
            continue

        if command.startswith("&&", i) or command.startswith("||", i):
            last_chain_op_end = i + 2
            i += 2
            continue

        if ch == ";":
            last_chain_op_end = -1
            i += 1
            continue

        if ch == "|":
            last_chain_op_end = -1
            i += 1
            continue

        if ch == "&":
            if i + 1 < n and command[i + 1] == ">":
                i += 2
                continue
            j = i - 1
            while j >= 0 and command[j].isspace():
                j -= 1
            if j >= 0 and command[j] in "<>":
                i += 1
                continue
            if last_chain_op_end >= 0:
                rewrites.append((last_chain_op_end, i))
            last_chain_op_end = -1
            i += 1
            continue

        _, next_i = _read_shell_token(command, i)
        i = max(next_i, i + 1)

    if not rewrites:
        return command

    result = command
    for chain_end, amp_pos in reversed(rewrites):
        insert_pos = chain_end
        while insert_pos < amp_pos and result[insert_pos].isspace():
            insert_pos += 1
        prefix = result[:insert_pos]
        middle = result[insert_pos:amp_pos]
        suffix = result[amp_pos + 1:]
        result = prefix + "{ " + middle + "& }" + suffix

    return result


# ═══════════════════════════════════════════════════════
#  sudo密码缓存
# ═══════════════════════════════════════════════════════

def _rewrite_real_sudo_invocations(command: str) -> tuple[str, bool]:
    """Rewrite only real unquoted sudo command words, not plain text mentions."""
    out: list[str] = []
    i = 0
    n = len(command)
    command_start = True
    found = False

    while i < n:
        ch = command[i]

        if ch.isspace():
            out.append(ch)
            if ch == "\n":
                command_start = True
            i += 1
            continue

        if ch == "#" and command_start:
            comment_end = command.find("\n", i)
            if comment_end == -1:
                out.append(command[i:])
                break
            out.append(command[i:comment_end])
            i = comment_end
            continue

        if command.startswith("&&", i) or command.startswith("||", i) or command.startswith(";;", i):
            out.append(command[i:i + 2])
            i += 2
            command_start = True
            continue

        if ch in ";|&(":
            out.append(ch)
            i += 1
            command_start = True
            continue

        if ch == ")":
            out.append(ch)
            i += 1
            command_start = False
            continue

        token, next_i = _read_shell_token(command, i)
        if command_start and token == "sudo":
            out.append("sudo -S -p ''")
            found = True
        else:
            out.append(token)

        if command_start and _looks_like_env_assignment(token):
            command_start = True
        else:
            command_start = False
        i = next_i

    return "".join(out), found


def _transform_sudo_command(command: str | None) -> tuple[str | None, str | None]:
    """Transform sudo commands to use -S flag if SUDO_PASSWORD is available.

    Returns:
        (transformed_command, sudo_stdin) where:
        - transformed_command has every bare ``sudo`` replaced with
          ``sudo -S -p ''`` so sudo reads its password from stdin.
        - sudo_stdin is the password if SUDO_PASSWORD is set, else None.
    """
    env_password = os.environ.get("SUDO_PASSWORD")
    if command is None:
        return None, None

    if env_password:
        transformed, found = _rewrite_real_sudo_invocations(command)
        if found:
            return transformed, env_password + "\n"

    return command, None
