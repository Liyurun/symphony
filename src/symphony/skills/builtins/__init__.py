"""内置技能集合。

导出全部内置技能类，并提供 register_builtins 便捷函数，
用于将所有内置技能一次性注册到给定的 SkillRegistry。
"""

from symphony.config import SkillsConfig
from symphony.skills.builtins.file_ops import FileReadSkill, FileWriteSkill
from symphony.skills.builtins.http_request import HttpRequestSkill
from symphony.skills.builtins.python_execute import PythonExecuteSkill
from symphony.skills.builtins.skill_references import SkillInventorySkill, SkillReferenceSearchSkill
from symphony.skills.builtins.workspace import (
    BashExecuteSkill,
    FilePatchSkill,
    WorkspaceListFilesSkill,
    WorkspaceSearchSkill,
)

# 所有内置技能类清单
BUILTIN_SKILLS = [
    HttpRequestSkill,
    FileReadSkill,
    FileWriteSkill,
    PythonExecuteSkill,
    WorkspaceListFilesSkill,
    WorkspaceSearchSkill,
    FilePatchSkill,
    BashExecuteSkill,
    SkillReferenceSearchSkill,
    SkillInventorySkill,
]

__all__ = [
    "HttpRequestSkill",
    "FileReadSkill",
    "FileWriteSkill",
    "PythonExecuteSkill",
    "WorkspaceListFilesSkill",
    "WorkspaceSearchSkill",
    "FilePatchSkill",
    "BashExecuteSkill",
    "SkillReferenceSearchSkill",
    "SkillInventorySkill",
    "BUILTIN_SKILLS",
    "register_builtins",
]


def register_builtins(registry, skills: SkillsConfig | None = None) -> None:
    """将所有内置技能实例注册到给定的注册中心。"""
    # 未显式传配置时使用模型默认值，保持旧调用兼容
    skills = skills or SkillsConfig()
    skill_instances = [
        HttpRequestSkill(timeout=skills.http_request.timeout_seconds),
        FileReadSkill(),
        FileWriteSkill(),
        PythonExecuteSkill(timeout=skills.python_execute.timeout_seconds),
        WorkspaceListFilesSkill(
            max_results=skills.workspace.list_files_max_results,
        ),
        WorkspaceSearchSkill(
            max_results=skills.workspace.search_max_results,
        ),
        FilePatchSkill(),
        BashExecuteSkill(
            timeout=skills.workspace.bash_timeout_seconds,
            max_output_chars=skills.workspace.max_output_chars,
        ),
        SkillReferenceSearchSkill(),
        SkillInventorySkill(),
    ]
    for skill in skill_instances:
        registry.register(skill, source="builtin")
