"""Python 代码执行技能。

在受限环境中执行用户提供的 Python 代码，捕获标准输出与标准错误，
并回收执行后的非下划线开头局部变量。执行失败时返回错误堆栈。
"""

import asyncio
from copy import deepcopy
import json
import sys
from typing import Any

from symphony.skills.base import Skill, SkillContext


DEFAULT_TIMEOUT = 30


def _safe_int(value: Any, default: int, lower: int, upper: int) -> int:
    """把用户传入的数值限制在合理范围内。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(number, upper))

# 子进程执行器：在子进程内捕获用户 stdout/stderr，并把结构化结果写回父进程。
_EXECUTION_WRAPPER = r"""
import contextlib
import io
import json
import sys
import traceback

SAFE_BUILTINS = {
    "print": print,
    "range": range,
    "len": len,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "sorted": sorted,
    "sum": sum,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "type": type,
    "isinstance": isinstance,
}

code = sys.stdin.read()
stdout_buf = io.StringIO()
stderr_buf = io.StringIO()
local_vars = {}

try:
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exec(code, {"__builtins__": SAFE_BUILTINS}, local_vars)
    variables = {
        key: str(value)
        for key, value in local_vars.items()
        if not key.startswith("_")
    }
    payload = {
        "success": True,
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "variables": variables,
    }
except Exception:
    payload = {
        "success": False,
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue() + traceback.format_exc(),
    }

sys.stdout.write(json.dumps(payload, ensure_ascii=False))
"""


class PythonExecuteSkill(Skill):
    """在受限环境中执行 Python 代码的技能。"""

    # 技能名称
    name = "python_execute"
    # 技能描述
    description = "Execute Python code in a restricted environment"
    # 输入参数 schema
    input_schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT},
        },
        "required": ["code"],
    }
    # 输出结果 schema
    output_schema = {"type": "object"}

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        """初始化 Python 执行技能的默认参数。"""
        # 默认执行超时时间，注册时可由配置覆盖
        self.default_timeout = _safe_int(timeout, DEFAULT_TIMEOUT, 1, 120)
        # 每个实例持有独立 schema，避免配置默认值跨注册中心串扰
        self.input_schema = deepcopy(type(self).input_schema)
        self.input_schema["properties"]["timeout"]["default"] = self.default_timeout

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """执行代码，捕获输出并回收局部变量。"""
        # 待执行代码
        code = args["code"]
        # 单次调用可覆盖配置默认超时
        timeout = _safe_int(args.get("timeout"), self.default_timeout, 1, 120)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _EXECUTION_WRAPPER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(code.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            stderr_text = stderr.decode("utf-8", errors="replace")
            return {
                "success": False,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": f"{stderr_text}Execution timed out after {timeout} seconds",
                "timed_out": True,
            }

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        try:
            result = json.loads(stdout_text)
        except json.JSONDecodeError:
            return {
                "success": False,
                "stdout": stdout_text,
                "stderr": stderr_text or "Python execution failed before returning a result.",
                "timed_out": False,
            }

        if stderr_text:
            result["stderr"] = f"{result.get('stderr', '')}{stderr_text}"
        result["timed_out"] = False
        return result
