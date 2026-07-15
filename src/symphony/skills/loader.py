"""自定义 Skill 加载器。

从配置的自定义技能目录中加载 Python 文件，发现其中的 Skill 子类或显式导出的
SKILL/SKILLS 对象并注册到 SkillRegistry。加载失败会被记录为错误报告，避免一个
有问题的用户脚本阻塞整个服务启动。
"""

import hashlib
import importlib.util
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from symphony.skills.base import Skill
from symphony.skills.registry import SkillRegistry


@dataclass
class SkillLoadError:
    """单个自定义 Skill 文件的加载错误。"""

    path: str
    error: str


@dataclass
class SkillLoadResult:
    """一次自定义 Skill 加载的结果。"""

    loaded: list[str] = field(default_factory=list)
    errors: list[SkillLoadError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转成可直接通过 API 返回的字典。"""
        return {
            "loaded": list(self.loaded),
            "errors": [item.__dict__ for item in self.errors],
        }


def load_custom_skills(registry: SkillRegistry, directory: str | Path) -> SkillLoadResult:
    """扫描目录并把其中的自定义 Skill 注册到 registry。

    支持两种写法：
    - 文件中定义一个或多个 ``Skill`` 子类，且构造函数无需参数；
    - 文件中显式导出 ``SKILL`` 或 ``SKILLS``，其中元素可以是 Skill 实例或子类。
    """
    result = SkillLoadResult()
    root = Path(directory).expanduser()
    if not root.exists():
        return result
    if not root.is_dir():
        result.errors.append(SkillLoadError(path=str(root), error="Custom skills path is not a directory"))
        return result

    for path in sorted(root.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _load_module(path)
            skills = _discover_skills(module)
            if not skills:
                raise ValueError("No Skill subclass, SKILL, or SKILLS export found")
            for skill in skills:
                _validate_skill(skill)
                registry.register(skill, source=f"custom:{path}")
                result.loaded.append(skill.name)
        except Exception as exc:  # 用户扩展代码边界
            result.errors.append(SkillLoadError(path=str(path), error=f"{type(exc).__name__}: {exc}"))
    return result


def _load_module(path: Path) -> ModuleType:
    """按文件路径加载一个隔离模块名的 Python 模块。"""
    digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    module_name = f"_symphony_custom_skill_{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to create module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _discover_skills(module: ModuleType) -> list[Skill]:
    """从模块中发现可注册的 Skill 实例。"""
    explicit = _explicit_exports(module)
    if explicit:
        return [_coerce_skill(item) for item in explicit]

    skills: list[Skill] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj is Skill or not issubclass(obj, Skill):
            continue
        if obj.__module__ != module.__name__:
            continue
        skills.append(_coerce_skill(obj))
    return skills


def _explicit_exports(module: ModuleType) -> list[Any]:
    """读取模块显式导出的 SKILL/SKILLS。"""
    if hasattr(module, "SKILLS"):
        value = getattr(module, "SKILLS")
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
    if hasattr(module, "SKILL"):
        return [getattr(module, "SKILL")]
    return []


def _coerce_skill(value: Any) -> Skill:
    """把 Skill 子类或实例统一转成实例。"""
    if isinstance(value, Skill):
        return value
    if inspect.isclass(value) and issubclass(value, Skill) and value is not Skill:
        return value()
    raise TypeError(f"Unsupported skill export: {value!r}")


def _validate_skill(skill: Skill) -> None:
    """校验 Skill 元信息是否满足运行时最低要求。"""
    if not isinstance(getattr(skill, "name", None), str) or not skill.name.strip():
        raise ValueError("Skill.name must be a non-empty string")
    if not isinstance(getattr(skill, "description", None), str):
        raise ValueError(f"Skill {skill.name} description must be a string")
    if not isinstance(getattr(skill, "input_schema", None), dict):
        raise ValueError(f"Skill {skill.name} input_schema must be a dict")
    if not isinstance(getattr(skill, "output_schema", None), dict):
        raise ValueError(f"Skill {skill.name} output_schema must be a dict")
