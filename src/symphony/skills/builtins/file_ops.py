"""文件读写技能。

提供读取与写入本地文件的两个技能，基于 pathlib 处理路径，
支持 ~ 展开、指定编码，写入时自动创建父目录并支持追加模式。
"""

from pathlib import Path
from typing import Any

from symphony.skills.base import Skill, SkillContext


class FileReadSkill(Skill):
    """读取本地文件内容的技能。"""

    # 技能名称
    name = "file_read"
    # 技能描述
    description = "Read the content of a local file"
    # 输入参数 schema
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path"],
    }
    # 输出结果 schema
    output_schema = {"type": "object"}

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """读取文件并返回路径、内容与字节大小。"""
        # 展开 ~ 得到实际路径
        path = Path(args["path"]).expanduser()
        # 读取编码，默认 utf-8
        encoding = args.get("encoding", "utf-8")
        # 文件系统边界：读取文本内容
        content = path.read_text(encoding=encoding)
        return {
            "path": str(path),
            "content": content,
            "size": len(content.encode(encoding)),
        }


class FileWriteSkill(Skill):
    """写入本地文件的技能，支持覆盖与追加。"""

    # 技能名称
    name = "file_write"
    # 技能描述
    description = "Write content to a local file"
    # 输入参数 schema
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "encoding": {"type": "string", "default": "utf-8"},
            "append": {"type": "boolean", "default": False},
        },
        "required": ["path", "content"],
    }
    # 输出结果 schema
    output_schema = {"type": "object"}

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """写入文件并返回路径与写入字节数。"""
        # 展开 ~ 得到实际路径
        path = Path(args["path"]).expanduser()
        # 待写入内容
        content = args["content"]
        # 写入编码，默认 utf-8
        encoding = args.get("encoding", "utf-8")
        # 是否追加，默认覆盖
        append = args.get("append", False)
        # 写前确保父目录存在
        path.parent.mkdir(parents=True, exist_ok=True)
        # 追加或覆盖模式
        mode = "a" if append else "w"
        # 文件系统边界：写入文本内容
        with path.open(mode, encoding=encoding) as f:
            f.write(content)
        return {
            "path": str(path),
            "bytes_written": len(content.encode(encoding)),
        }
