"""SOP 模板的持久化加载器与提示词渲染工具。

TemplateLoader 负责把 SOPTemplate 以 ``{id}.sop.json`` 形式读写到本地目录，
并提供列出/删除能力。render_prompt 基于 jinja2 对提示词模板做严格变量渲染。
"""

import json
from pathlib import Path
from typing import Optional

from jinja2 import BaseLoader, Environment, StrictUndefined

from symphony.workflow.models import SOPTemplate


class TemplateLoader:
    """SOP 模板文件加载器，按 id 读写本地 JSON 文件。"""

    def __init__(self, templates_dir):
        """初始化并确保模板目录存在。

        :param templates_dir: 模板根目录，支持 ``~`` 展开。
        """
        # 统一转为绝对路径的 Path
        self.templates_dir = Path(templates_dir).expanduser()
        # 目录不存在则递归创建
        self.templates_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sop_id: str) -> Path:
        """返回指定 sop_id 对应的文件路径。"""
        return self.templates_dir / f"{sop_id}.sop.json"

    def save(self, template: SOPTemplate) -> Path:
        """将模板序列化为 JSON 写入磁盘，返回文件路径。"""
        # 目标文件路径
        path = self._path(template.id)
        # 按别名导出（edges 输出 "from" 键），保留非 ASCII 字符
        data = template.model_dump(by_alias=True)
        # 写入格式化 JSON
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, sop_id: str) -> Optional[SOPTemplate]:
        """按 id 加载模板，文件不存在时返回 None。"""
        # 目标文件路径
        path = self._path(sop_id)
        # 文件不存在直接返回 None
        if not path.exists():
            return None
        # 读取并解析 JSON，构造模型（populate_by_name 支持 from 别名还原）
        data = json.loads(path.read_text(encoding="utf-8"))
        return SOPTemplate(**data)

    def list_all(self) -> list[SOPTemplate]:
        """列出目录下所有可解析的模板，跳过损坏文件。"""
        # 结果列表
        templates: list[SOPTemplate] = []
        # 遍历所有 *.sop.json 文件
        for path in sorted(self.templates_dir.glob("*.sop.json")):
            # 文件边界：解析失败的损坏文件直接跳过
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                templates.append(SOPTemplate(**data))
            except Exception:
                continue
        return templates

    def delete(self, sop_id: str) -> bool:
        """删除指定模板，存在则删除返回 True，否则 False。"""
        # 目标文件路径
        path = self._path(sop_id)
        # 文件不存在返回 False
        if not path.exists():
            return False
        # 删除文件并返回 True
        path.unlink()
        return True


def render_prompt(template_str: str, variables: dict) -> str:
    """用 jinja2 严格渲染提示词模板。

    使用 StrictUndefined：模板中引用了未提供的变量时会直接抛错，
    避免静默产出错误提示词。

    :param template_str: 含 jinja2 变量的模板字符串。
    :param variables: 渲染用的变量表。
    :return: 渲染后的字符串。
    """
    # 基于内存字符串的 jinja2 环境，未定义变量严格报错
    env = Environment(loader=BaseLoader(), undefined=StrictUndefined)
    # 从字符串编译模板并渲染
    return env.from_string(template_str).render(**variables)
