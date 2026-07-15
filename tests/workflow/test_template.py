"""workflow 模型与模板加载器的单元测试。

覆盖 SOPTemplate 校验、NodeType 枚举、TemplateLoader 存取往返、
Edge 的 from 别名、prompt 渲染，以及节点前后驱查询。
"""

import tempfile

from symphony.workflow import (
    Edge,
    NodeType,
    SOPTemplate,
    TemplateLoader,
    render_prompt,
)

# 一个最小可用的 SOP 字典：单个 agent 节点，无边，入口为 step1
SAMPLE_SOP = {
    "id": "test-sop",
    "name": "Test SOP",
    "version": "1.0.0",
    "description": "A test SOP",
    "variables": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
    "nodes": [
        {
            "id": "step1",
            "name": "Step 1",
            "type": "agent",
            "prompt": "Process {{input}}",
            "skills": [],
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
            "retry_policy": {"max_retries": 3, "retry_on": ["validation_error"]},
            "timeout_seconds": 60,
        }
    ],
    "edges": [],
    "entry_node": "step1",
}


def test_sop_template_validation():
    """SOPTemplate 能正确校验 SAMPLE_SOP 并暴露关键字段。"""
    template = SOPTemplate(**SAMPLE_SOP)
    # 断言：id 正确
    assert template.id == "test-sop"
    # 断言：仅有一个节点
    assert len(template.nodes) == 1
    # 断言：入口节点正确
    assert template.entry_node == "step1"


def test_node_types():
    """NodeType 枚举包含 agent / human / skill 三个取值。"""
    assert NodeType.AGENT == "agent"
    assert NodeType.HUMAN == "human"
    assert NodeType.SKILL == "skill"


def test_template_loader_save_load():
    """save 后 load 应还原出 id/name 一致的模板。"""
    template = SOPTemplate(**SAMPLE_SOP)
    # 在临时目录内完成一次存取往返
    with tempfile.TemporaryDirectory() as tmp_dir:
        loader = TemplateLoader(tmp_dir)
        loader.save(template)
        loaded = loader.load("test-sop")
        # 断言：加载结果非空且关键字段一致
        assert loaded is not None
        assert loaded.id == template.id
        assert loaded.name == template.name


def test_edge_alias():
    """Edge 支持用 from 别名构造，且 by_alias 序列化输出 from 键。"""
    edge = Edge(**{"from": "a", "to": "b"})
    # 断言：Python 侧字段名为 from_node
    assert edge.from_node == "a"
    assert edge.to == "b"
    # 断言：按别名导出时含 "from" 键
    dumped = edge.model_dump(by_alias=True)
    assert "from" in dumped
    assert dumped["from"] == "a"


def test_render_prompt():
    """render_prompt 用 jinja2 渲染变量。"""
    assert render_prompt("你好 {{name}}", {"name": "世界"}) == "你好 世界"


def test_get_next_nodes():
    """构造带边的 SOP，验证前驱/后继查询。"""
    # 在 SAMPLE_SOP 基础上追加 step2 节点与 step1->step2 的边
    sop = {
        **SAMPLE_SOP,
        "nodes": [
            *SAMPLE_SOP["nodes"],
            {"id": "step2", "name": "Step 2", "type": "agent"},
        ],
        "edges": [{"from": "step1", "to": "step2"}],
    }
    template = SOPTemplate(**sop)
    # 断言：step1 的后继为 step2
    assert template.get_next_nodes("step1") == ["step2"]
    # 断言：step2 的前驱为 step1
    assert template.get_prev_nodes("step2") == ["step1"]
