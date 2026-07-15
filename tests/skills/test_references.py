"""外部 Skill 文档检索测试。"""

from pathlib import Path

from symphony.skills.references import (
    SkillReferenceIndex,
    build_skill_reference_guidance,
    parse_skill_reference,
)
from symphony.skills.builtins import SkillInventorySkill, SkillReferenceSearchSkill
from symphony.skills import SkillContext


def test_parse_skill_reference_frontmatter(tmp_path: Path):
    """应从 SKILL.md frontmatter 中解析 name/description。"""
    path = tmp_path / "demo" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(
        """---
name: demo-skill
description: 用于查询日志和排查服务问题。
---

# Demo

更多说明。
""",
        encoding="utf-8",
    )

    ref = parse_skill_reference(path, tmp_path)

    assert ref.name == "demo-skill"
    assert "查询日志" in ref.description
    assert ref.source == tmp_path.name


def test_skill_reference_index_searches_skill_docs(tmp_path: Path):
    """索引应能按用户问题检索相关 SKILL.md。"""
    log_skill = tmp_path / "bytedance-log" / "SKILL.md"
    log_skill.parent.mkdir()
    log_skill.write_text(
        """---
name: bytedance-log
description: 查询服务日志、LogID、pod 日志。
---

Use this skill when users need to search logs.
""",
        encoding="utf-8",
    )
    doc_skill = tmp_path / "lark-doc" / "SKILL.md"
    doc_skill.parent.mkdir()
    doc_skill.write_text(
        """---
name: lark-doc
description: 读取和编辑飞书文档。
---
""",
        encoding="utf-8",
    )

    index = SkillReferenceIndex.from_roots([tmp_path])
    matches = index.search("帮我查日志", limit=3)

    assert matches
    assert matches[0].reference.name == "bytedance-log"
    assert "日志" in matches[0].snippet


def test_skill_reference_index_search_lists_for_inventory_queries(tmp_path: Path):
    """索引层应统一把 skill/tool/能力清单问题转成 list_all。"""
    for name in ["alpha-tool", "beta-tool"]:
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {name} description
---
""",
            encoding="utf-8",
        )
    index = SkillReferenceIndex.from_roots([tmp_path])

    assert [m.reference.name for m in index.search("你有哪些能力", limit=10)] == [
        "alpha-tool",
        "beta-tool",
    ]


def test_skill_reference_index_supports_symphony_local_markdown_files(tmp_path: Path):
    """索引应支持 .symphony/skills/name.md 项目本地格式。"""
    local_skills = tmp_path / ".symphony" / "skills"
    local_skills.mkdir(parents=True)
    (local_skills / "adseek-hive-explorer.md").write_text(
        """---
name: adseek-hive-explorer
description: Explore Hive metadata, schema, lineage, and producer Dorado tasks.
---

# adseek Hive Explorer

用于查询 Hive 表、字段来源和下游影响。
""",
        encoding="utf-8",
    )

    index = SkillReferenceIndex.from_roots([local_skills])
    matches = index.search("Hive 字段来源", limit=3)

    assert matches
    assert matches[0].reference.name == "adseek-hive-explorer"
    assert matches[0].reference.source == ".symphony/skills"


def test_skill_reference_index_supports_pi_local_markdown_files(tmp_path: Path):
    """索引应兼容 .pi/skills/name.md 这种 Pi Agent 本地格式。"""
    pi_skills = tmp_path / ".pi" / "skills"
    pi_skills.mkdir(parents=True)
    (pi_skills / "adseek-dorado-publish.md").write_text(
        """---
name: adseek-dorado-publish
description: Prepare and execute Dorado publish flows.
---

# adseek Dorado Publish
""",
        encoding="utf-8",
    )

    index = SkillReferenceIndex.from_roots([pi_skills])
    matches = index.search("Dorado publish", limit=3)

    assert matches
    assert matches[0].reference.name == "adseek-dorado-publish"
    assert matches[0].reference.source == ".pi/skills"


def test_build_skill_reference_guidance_warns_reference_only(tmp_path: Path):
    """注入提示必须明确这些外部 Skill 只是参考资料。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: sample
description: sample desc
---
sample body
""",
        encoding="utf-8",
    )
    index = SkillReferenceIndex.from_roots([tmp_path])
    guidance = build_skill_reference_guidance(index.search("sample", limit=1))

    assert "外部 Skill 参考资料" in guidance
    assert "不代表 Symphony 已具备对应执行能力" in guidance
    assert "skill_reference_search" in guidance
    assert "sample" in guidance


async def test_skill_reference_search_skill_returns_matches(tmp_path: Path):
    """skill_reference_search 应作为可执行 Skill 检索外部 SKILL.md。"""
    skill_dir = tmp_path / "database-toolbox"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: database-toolbox
description: 数据库连接、查询、数据分析和治理。
---
""",
        encoding="utf-8",
    )
    index = SkillReferenceIndex.from_roots([tmp_path])
    skill = SkillReferenceSearchSkill(index=index)

    result = await skill.execute(
        {"query": "数据库", "limit": 3},
        SkillContext(task_id="t", node_id="n"),
    )

    assert result["items"][0]["name"] == "database-toolbox"
    assert "数据库" in result["items"][0]["description"]


async def test_skill_reference_search_lists_inventory_for_broad_skill_query(tmp_path: Path):
    """query=skill 应被识别为清单请求，而不是普通关键词搜索。"""
    for name in ["alpha-tool", "beta-tool"]:
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {name} description
---
""",
            encoding="utf-8",
        )
    index = SkillReferenceIndex.from_roots([tmp_path])
    skill = SkillReferenceSearchSkill(index=index)

    result = await skill.execute(
        {"query": "skill", "limit": 10},
        SkillContext(task_id="t", node_id="n"),
    )

    assert result["mode"] == "list"
    assert result["total"] == 2
    assert [item["name"] for item in result["items"]] == ["alpha-tool", "beta-tool"]


async def test_skill_inventory_lists_executable_and_external_skills(tmp_path: Path):
    """skill_inventory 应同时返回可执行 Skill 与外部参考 Skill。"""
    skill_dir = tmp_path / "external-one"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: external-one
description: external reference
---
""",
        encoding="utf-8",
    )
    index = SkillReferenceIndex.from_roots([tmp_path])
    skill = SkillInventorySkill()

    result = await skill.execute(
        {"include_external": True, "limit": 10},
        SkillContext(
            task_id="t",
            node_id="n",
            variables={
                "available_skills": [
                    {"name": "workspace_search", "description": "Search files", "source": "builtin"}
                ],
                "skill_reference_index": index,
            },
        ),
    )

    assert result["executable_skills"][0]["name"] == "workspace_search"
    assert result["external_references"][0]["name"] == "external-one"
    assert result["external_reference_count"] == 1
