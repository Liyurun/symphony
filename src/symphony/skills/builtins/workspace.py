"""工作区操作类内置 Skill。

提供更接近 Pi Agent 的基础能力：列文件、全文检索、精确替换修改文件、
执行短命令。所有路径默认限制在当前工作目录（或调用方传入的 root）下，
避免模型误操作工作区之外的文件。
"""

import asyncio
from copy import deepcopy
import fnmatch
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from symphony.skills.base import Skill, SkillContext


DEFAULT_MAX_RESULTS = 200
DEFAULT_SEARCH_MAX_RESULTS = 80
DEFAULT_MAX_OUTPUT = 12000
DEFAULT_TIMEOUT = 30

_BLOCKED_COMMAND_MARKERS = [
    "rm -rf",
    "git reset --hard",
    "git checkout --",
    "mkfs",
    "shutdown",
    "reboot",
    "sudo ",
    "chmod -R 777",
]


def _workspace_root(args: dict[str, Any], context: SkillContext) -> Path:
    """解析工作区根目录，默认使用当前进程工作目录。"""
    raw = args.get("root") or context.variables.get("workspace_root") or os.getcwd()
    return Path(str(raw)).expanduser().resolve()


def _resolve_under_root(root: Path, path_value: str | None = None) -> Path:
    """解析路径并确保它位于 root 内。"""
    raw = path_value or "."
    path = Path(raw).expanduser()
    target = path.resolve() if path.is_absolute() else (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Path escapes workspace root: {raw}")
    return target


def _relative(root: Path, path: Path) -> str:
    """返回相对 root 的 POSIX 风格路径。"""
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def _is_hidden(relative_path: str) -> bool:
    """判断相对路径是否包含隐藏路径段。"""
    return any(part.startswith(".") for part in Path(relative_path).parts)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """按字符数截断文本。"""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _safe_int(value: Any, default: int, lower: int, upper: int) -> int:
    """把用户传入的数值限制在合理范围内。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(number, upper))


class WorkspaceListFilesSkill(Skill):
    """列出工作区内的文件和目录。"""

    name = "workspace_list_files"
    description = "List files and directories under the workspace root"
    input_schema = {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Workspace root, defaults to process cwd"},
            "path": {"type": "string", "default": ".", "description": "Directory or file path under root"},
            "glob": {"type": "string", "default": "**/*", "description": "Glob pattern relative to path"},
            "max_results": {"type": "integer", "default": DEFAULT_MAX_RESULTS},
            "include_hidden": {"type": "boolean", "default": False},
        },
    }
    output_schema = {"type": "object"}

    def __init__(self, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        """初始化 workspace_list_files 的默认结果数量上限。"""
        # 默认返回数量上限，注册时可由配置覆盖
        self.default_max_results = _safe_int(max_results, DEFAULT_MAX_RESULTS, 1, 1000)
        # 每个实例持有独立 schema，确保不同配置的注册中心互不影响
        self.input_schema = deepcopy(type(self).input_schema)
        self.input_schema["properties"]["max_results"]["default"] = (
            self.default_max_results
        )

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """列出文件，并返回相对路径、类型与大小。"""
        root = _workspace_root(args, context)
        base = _resolve_under_root(root, args.get("path"))
        max_results = _safe_int(args.get("max_results"), self.default_max_results, 1, 1000)
        include_hidden = bool(args.get("include_hidden", False))
        pattern = args.get("glob") or "**/*"

        if not base.exists():
            raise FileNotFoundError(str(base))

        candidates = [base] if base.is_file() else sorted(base.glob(pattern))
        entries: list[dict[str, Any]] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            rel = _relative(root, resolved)
            if not include_hidden and _is_hidden(rel):
                continue
            if resolved.is_dir():
                kind = "directory"
                size = None
            elif resolved.is_file():
                kind = "file"
                size = resolved.stat().st_size
            else:
                continue
            entries.append({"path": rel, "type": kind, "size": size})
            if len(entries) >= max_results:
                break

        return {
            "root": str(root),
            "path": _relative(root, base),
            "entries": entries,
            "truncated": len(entries) >= max_results,
        }


class WorkspaceSearchSkill(Skill):
    """在工作区内搜索文件名或文件内容。"""

    name = "workspace_search"
    description = "Search workspace files by content or filename"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "root": {"type": "string", "description": "Workspace root, defaults to process cwd"},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "string", "description": "Only include matching file paths, e.g. *.py"},
            "mode": {"type": "string", "enum": ["content", "filename"], "default": "content"},
            "max_results": {"type": "integer", "default": DEFAULT_SEARCH_MAX_RESULTS},
        },
        "required": ["query"],
    }
    output_schema = {"type": "object"}

    def __init__(self, max_results: int = DEFAULT_SEARCH_MAX_RESULTS) -> None:
        """初始化 workspace_search 的默认结果数量上限。"""
        # 默认返回数量上限，注册时可由配置覆盖
        self.default_max_results = _safe_int(
            max_results, DEFAULT_SEARCH_MAX_RESULTS, 1, 500
        )
        # 每个实例持有独立 schema，确保不同配置的注册中心互不影响
        self.input_schema = deepcopy(type(self).input_schema)
        self.input_schema["properties"]["max_results"]["default"] = (
            self.default_max_results
        )

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """执行搜索，优先使用 rg，缺失时回退到 Python 扫描。"""
        root = _workspace_root(args, context)
        base = _resolve_under_root(root, args.get("path"))
        query = str(args["query"])
        mode = args.get("mode", "content")
        max_results = _safe_int(args.get("max_results"), self.default_max_results, 1, 500)
        glob = args.get("glob")

        if mode == "filename":
            matches = self._search_filenames(root, base, query, glob, max_results)
        else:
            matches = await self._search_content(root, base, query, glob, max_results)
        return {
            "root": str(root),
            "query": query,
            "mode": mode,
            "matches": matches,
            "truncated": len(matches) >= max_results,
        }

    def _search_filenames(
        self,
        root: Path,
        base: Path,
        query: str,
        glob: str | None,
        max_results: int,
    ) -> list[dict[str, Any]]:
        """按文件名搜索。"""
        lowered = query.lower()
        matches: list[dict[str, Any]] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = _relative(root, path.resolve())
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            if lowered in path.name.lower() or lowered in rel.lower():
                matches.append({"path": rel})
                if len(matches) >= max_results:
                    break
        return matches

    async def _search_content(
        self,
        root: Path,
        base: Path,
        query: str,
        glob: str | None,
        max_results: int,
    ) -> list[dict[str, Any]]:
        """按内容搜索，优先调用 rg。"""
        if shutil.which("rg"):
            return await self._search_content_with_rg(root, base, query, glob, max_results)
        return self._search_content_with_python(root, base, query, glob, max_results)

    async def _search_content_with_rg(
        self,
        root: Path,
        base: Path,
        query: str,
        glob: str | None,
        max_results: int,
    ) -> list[dict[str, Any]]:
        """通过 rg 搜索内容。"""
        command = [
            "rg",
            "--fixed-strings",
            "--line-number",
            "--column",
            "--no-heading",
            "--color",
            "never",
            query,
            str(base),
        ]
        if glob:
            command[1:1] = ["--glob", glob]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        matches: list[dict[str, Any]] = []
        for line in lines:
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            path_text, line_no, col_no, text = parts
            path = Path(path_text).resolve()
            if path != root and root not in path.parents:
                continue
            matches.append(
                {
                    "path": _relative(root, path),
                    "line": int(line_no),
                    "column": int(col_no),
                    "text": text,
                }
            )
            if len(matches) >= max_results:
                break
        return matches

    def _search_content_with_python(
        self,
        root: Path,
        base: Path,
        query: str,
        glob: str | None,
        max_results: int,
    ) -> list[dict[str, Any]]:
        """纯 Python 内容搜索回退。"""
        matches: list[dict[str, Any]] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = _relative(root, path.resolve())
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(lines, start=1):
                col = line.find(query)
                if col < 0:
                    continue
                matches.append({"path": rel, "line": line_no, "column": col + 1, "text": line})
                if len(matches) >= max_results:
                    return matches
        return matches


class FilePatchSkill(Skill):
    """通过精确文本替换修改工作区内文件。"""

    name = "file_patch"
    description = "Patch a workspace file by replacing an exact old_text with new_text"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "root": {"type": "string", "description": "Workspace root, defaults to process cwd"},
            "encoding": {"type": "string", "default": "utf-8"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_text", "new_text"],
    }
    output_schema = {"type": "object"}

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """执行精确替换，并返回替换次数。"""
        root = _workspace_root(args, context)
        path = _resolve_under_root(root, args["path"])
        encoding = args.get("encoding", "utf-8")
        old_text = args["old_text"]
        new_text = args["new_text"]
        replace_all = bool(args.get("replace_all", False))
        if old_text == "":
            raise ValueError("old_text must not be empty")
        content = path.read_text(encoding=encoding)
        count = content.count(old_text)
        if count == 0:
            raise ValueError("old_text not found")
        if count > 1 and not replace_all:
            raise ValueError(f"old_text matched {count} times; set replace_all=true to replace all")
        updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding=encoding)
        replaced = count if replace_all else 1
        return {
            "path": _relative(root, path),
            "replacements": replaced,
            "bytes_written": len(updated.encode(encoding)),
        }


class BashExecuteSkill(Skill):
    """执行短生命周期 Bash 命令。"""

    name = "bash_execute"
    description = "Execute a short-running bash command in the workspace with timeout and output truncation"
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "root": {"type": "string", "description": "Workspace root, defaults to process cwd"},
            "cwd": {"type": "string", "default": "."},
            "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT},
            "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT},
        },
        "required": ["command"],
    }
    output_schema = {"type": "object"}

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_output_chars: int = DEFAULT_MAX_OUTPUT,
    ) -> None:
        """初始化 bash_execute 的默认限制。"""
        # 默认执行限制，注册时可由配置覆盖
        self.default_timeout = _safe_int(timeout, DEFAULT_TIMEOUT, 1, 120)
        self.default_max_output_chars = _safe_int(
            max_output_chars, DEFAULT_MAX_OUTPUT, 1000, 60000
        )
        # 每个实例持有独立 schema，确保不同配置的注册中心互不影响
        self.input_schema = deepcopy(type(self).input_schema)
        self.input_schema["properties"]["timeout"]["default"] = self.default_timeout
        self.input_schema["properties"]["max_output_chars"]["default"] = (
            self.default_max_output_chars
        )

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """执行命令并返回退出码、stdout 和 stderr。"""
        root = _workspace_root(args, context)
        cwd = _resolve_under_root(root, args.get("cwd"))
        command = str(args["command"])
        timeout = _safe_int(args.get("timeout"), self.default_timeout, 1, 120)
        max_output = _safe_int(
            args.get("max_output_chars"),
            self.default_max_output_chars,
            1000,
            60000,
        )
        self._validate_command(command)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            stdout, stderr = await proc.communicate()

        stdout_text, stdout_truncated = _truncate(stdout.decode("utf-8", errors="replace"), max_output)
        stderr_text, stderr_truncated = _truncate(stderr.decode("utf-8", errors="replace"), max_output)
        return {
            "command": command,
            "cwd": _relative(root, cwd),
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }

    def _validate_command(self, command: str) -> None:
        """拦截明显危险或交互式命令。"""
        normalized = " ".join(command.lower().split())
        for marker in _BLOCKED_COMMAND_MARKERS:
            if marker in normalized:
                raise ValueError(f"Blocked potentially destructive command: {marker}")
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"Invalid shell command: {exc}") from exc
        first = parts[0] if parts else ""
        if first in {"vim", "vi", "nano", "less", "more", "top", "htop"}:
            raise ValueError(f"Interactive command is not allowed: {first}")
