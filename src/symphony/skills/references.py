"""外部 Skill 文档检索。

Symphony 的可执行 Skill 是 Python 类；Trae/system 的 Skill 多数是 SKILL.md
说明文档。这个模块负责把这些说明文档作为只读参考资料纳入 Symphony：
扫描、建立轻量索引、按用户问题检索相关 Skill 文档片段，并生成可注入给 LLM 的
上下文提示。
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SkillReference:
    """一个外部 Skill 文档条目。"""

    name: str
    description: str
    path: str
    source: str
    content: str

    def to_dict(self, snippet: str = "") -> dict[str, Any]:
        """转成 API 可返回的字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "source": self.source,
            "snippet": snippet,
        }


@dataclass(frozen=True)
class SkillReferenceMatch:
    """一次检索命中的外部 Skill 文档。"""

    reference: SkillReference
    score: int
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        """转成 API 可返回的字典。"""
        data = self.reference.to_dict(snippet=self.snippet)
        data["score"] = self.score
        return data


class SkillReferenceIndex:
    """外部 Skill 文档轻量索引。"""

    def __init__(self, references: list[SkillReference]) -> None:
        """保存条目列表。"""
        self.references = references

    @classmethod
    def from_default_locations(cls) -> "SkillReferenceIndex":
        """从默认 Trae/system 目录构建索引。"""
        return cls.from_roots(default_skill_reference_roots())

    @classmethod
    def from_roots(cls, roots: list[str | Path]) -> "SkillReferenceIndex":
        """扫描多个根目录下的 Skill Markdown 并构建索引。"""
        refs: list[SkillReference] = []
        seen: set[str] = set()
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.exists():
                continue
            for path in _iter_skill_reference_files(root_path):
                resolved = str(path.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    refs.append(parse_skill_reference(path, root_path))
                except Exception:
                    # 外部文档不是运行边界，坏文档只跳过，不影响服务启动。
                    continue
        refs.sort(key=lambda item: (item.source, item.name))
        return cls(refs)

    def list_all(self, limit: int = 100) -> list[SkillReferenceMatch]:
        """列出前 limit 个外部 Skill 文档。"""
        return [
            SkillReferenceMatch(reference=ref, score=0, snippet=_first_lines(ref.content))
            for ref in self.references[: max(0, limit)]
        ]

    def search(self, query: str, limit: int = 5) -> list[SkillReferenceMatch]:
        """按查询文本检索外部 Skill 文档。"""
        text = query.strip()
        if not text or is_skill_inventory_query(text):
            return self.list_all(limit=limit)

        tokens = _tokens(text)
        matches: list[SkillReferenceMatch] = []
        for ref in self.references:
            score = _score_reference(ref, text, tokens)
            if score <= 0:
                continue
            matches.append(
                SkillReferenceMatch(
                    reference=ref,
                    score=score,
                    snippet=_snippet(ref.content, text, tokens),
                )
            )
        matches.sort(key=lambda item: (-item.score, item.reference.name))
        return matches[: max(0, limit)]


def default_skill_reference_roots() -> list[Path]:
    """返回默认外部 Skill 文档根目录。

    可通过 ``SYMPHONY_SKILL_REFERENCE_DIRS`` 追加或覆盖，多个路径用 os.pathsep
    分隔。未设置时扫描当前项目本地目录与当前机器上 Trae 常见 Skill 目录。
    """
    override = os.environ.get("SYMPHONY_SKILL_REFERENCE_DIRS")
    if override:
        return [Path(item).expanduser() for item in override.split(os.pathsep) if item.strip()]

    cwd = Path.cwd()
    trae_root = Path.home() / ".trae-cn"
    return [
        cwd / ".symphony" / "skills",
        cwd / ".pi" / "skills",
        cwd / "skills",
        Path.home() / ".symphony" / "skill_references",
        trae_root / "skills",
        trae_root / "builtin_skills",
        trae_root / "builtin" / "global" / "skills",
        trae_root / "builtin" / "trae",
        trae_root / "design_libraries",
    ]


def parse_skill_reference(path: Path, root: Path) -> SkillReference:
    """解析一个 Skill Markdown 文档。"""
    content = path.read_text(encoding="utf-8", errors="replace")
    meta, body = _frontmatter(content)
    name = str(meta.get("name") or _name_from_path(path))
    description = str(meta.get("description") or _description_from_body(body))
    return SkillReference(
        name=name,
        description=description,
        path=str(path),
        source=_source_label(path, root),
        content=content,
    )


def _iter_skill_reference_files(root: Path) -> list[Path]:
    """枚举支持的 Skill Markdown 文件。

    支持两种布局：
    - Trae 风格：``skill-name/SKILL.md``；
    - Pi 本地风格：``.pi/skills/skill-name.md``。
    """
    paths: list[Path] = []
    if root.is_file() and root.suffix.lower() == ".md":
        paths.append(root)
    elif root.is_dir():
        paths.extend(root.rglob("SKILL.md"))
        if root.name == "skills":
            paths.extend(path for path in root.glob("*.md") if path.name != "SKILL.md")
    return sorted(dict.fromkeys(paths))


def _name_from_path(path: Path) -> str:
    """从不同 Skill 文档布局中推断名称。"""
    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def build_skill_reference_guidance(matches: list[SkillReferenceMatch]) -> str:
    """把检索结果转成可注入给 LLM 的上下文提示。"""
    if not matches:
        return ""
    lines = [
        "外部 Skill 参考资料：",
        "以下内容来自本机 Trae/system 的 SKILL.md，只作为能力说明和操作参考。",
        "这些参考资料不代表 Symphony 已具备对应执行能力；需要实际执行时，请优先使用当前已注册工具，或说明缺少执行型 Skill/MCP 适配。",
        "如果需要继续检索更多外部 Skill 文档，请调用 skill_reference_search 工具。",
    ]
    for match in matches:
        ref = match.reference
        lines.append(f"- {ref.name} ({ref.source})")
        if ref.description:
            lines.append(f"  description: {ref.description[:240]}")
        if match.snippet:
            lines.append(f"  snippet: {match.snippet[:500]}")
        lines.append(f"  path: {ref.path}")
    return "\n".join(lines)


def is_skill_inventory_query(query: str) -> bool:
    """判断用户是否在请求 Skill 清单，而非按主题搜索。"""
    text = query.strip().lower().replace(" ", "")
    exact = {
        "skill",
        "skills",
        "tool",
        "tools",
        "capability",
        "capabilities",
        "allskills",
        "listskills",
        "listtools",
        "所有skill",
        "全部skill",
        "有哪些skill",
        "有哪些工具",
        "有哪些能力",
        "你有哪些工具",
        "你有哪些能力",
        "你能做什么",
        "能做什么",
    }
    if text in exact:
        return True
    nouns = ("skill", "tool", "capability", "工具", "能力")
    markers = ("所有", "全部", "哪些", "有什么", "有哪些", "list", "all", "what")
    return any(noun in text for noun in nouns) and any(marker in text for marker in markers)


def _frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown frontmatter；不存在时返回空元数据。"""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    raw = parts[1]
    body = parts[2]
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        data = {}
    return data if isinstance(data, dict) else {}, body


def _description_from_body(body: str) -> str:
    """从正文中提取一段简短描述。"""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line[:300]
    return ""


def _source_label(path: Path, root: Path) -> str:
    """生成短来源标签。"""
    try:
        rel = path.relative_to(Path.home() / ".trae-cn")
        parts = rel.parts
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return parts[0] if parts else root.name
    except ValueError:
        if root.name == "skills" and root.parent.name in {".pi", ".symphony"}:
            return f"{root.parent.name}/skills"
        return root.name


def _tokens(text: str) -> list[str]:
    """提取英文/数字 token 与中文短语 token。"""
    lowered = text.lower()
    raw_tokens = re.findall(r"[a-z0-9_\-]+|[\u4e00-\u9fff]{2,}", lowered)
    result: list[str] = []
    for token in raw_tokens:
        result.append(token)
        # 中文长句拆成双字窗口，提升“查日志”匹配“日志”等场景的召回。
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            result.extend(token[i : i + 2] for i in range(len(token) - 1))
    return list(dict.fromkeys(result))


def _score_reference(ref: SkillReference, query: str, tokens: list[str]) -> int:
    """计算一个文档相对查询的简单相关性分数。"""
    q = query.lower()
    name = ref.name.lower()
    desc = ref.description.lower()
    content = ref.content.lower()
    score = 0
    if q and q in name:
        score += 80
    if q and q in desc:
        score += 50
    if q and q in content:
        score += 8
    for token in tokens:
        if token in name:
            score += 25
        if token in desc:
            score += 12
        if token in content:
            score += 2
    return score


def _snippet(content: str, query: str, tokens: list[str], max_length: int = 360) -> str:
    """截取命中位置附近的一小段内容。"""
    lowered = content.lower()
    anchors = [query.lower(), *tokens]
    positions = [lowered.find(item) for item in anchors if item and lowered.find(item) >= 0]
    if not positions:
        return _first_lines(content, max_length=max_length)
    pos = min(positions)
    start = max(0, pos - 120)
    end = min(len(content), pos + max_length)
    return _normalize_space(content[start:end])


def _first_lines(content: str, max_length: int = 240) -> str:
    """取文档开头的有效文本片段。"""
    lines = [line.strip() for line in content.splitlines() if line.strip() and line.strip() != "---"]
    return _normalize_space(" ".join(lines)[:max_length])


def _normalize_space(text: str) -> str:
    """把多行文本压成一行，方便 TUI/Web 展示。"""
    return " ".join(text.split())
