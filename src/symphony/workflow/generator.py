"""AI SOP 生成器：把自然语言描述转换为结构化 SOPTemplate。

SOPGenerator 构造一段要求严格 JSON 输出的 system prompt，调用 LLMProvider
生成 SOP 定义文本，再容错解析为字典并交由 SOPTemplate 做 pydantic 校验。
解析在 LLM 输出边界允许使用 try/except：先直接 json.loads，失败则截取首个
花括号包裹的片段再解析，仍失败则抛 ValueError。

新版生成器指导 LLM 产出**类型化的 I/O 字段**（inputs / outputs / variables_def），
每个字段含 name / label / type(text|document|json) / description / required，
这样产出的 SOP 可直接在可视化串行编辑器里二次调整，并能在运行前/后做严格校验。
"""

import json
import re
import uuid
from typing import Optional

from symphony.ai.provider import LLMProvider
from symphony.ai.schema import Message, Role
from symphony.workflow.models import SOPTemplate

# 指导 LLM 输出的 system prompt：要求严格 JSON，并给出字段与示例（typed I/O 版本）
_SYSTEM_PROMPT = """你是一个 SOP（标准作业流程）设计助手。请根据用户的自然语言描述，设计一个**严格线性串行**的可执行工作流，节点 A 执行完才到 B，B 完才到 C。请只输出一个严格的 JSON 对象（不要 markdown 代码块标记、不要额外解释文字）。

JSON 结构说明：
- id: 字符串，SOP 唯一标识（英文短横线 slug）
- name: 字符串，SOP 名称
- version: 字符串，默认 "1.0.0"
- description: 字符串，用途描述
- variables_def: 数组，工作流级输入变量（由用户在启动时填写），每个字段形如：
    {"name": "英文标识", "label": "中文名", "type": "text"|"document"|"json", "description": "字段说明", "required": true}
    - text: 短文本（标题、名称等）
    - document: 长文档/正文
    - json: 结构化对象
- nodes: 数组，按执行顺序排列，每个节点：
    {"id": "英文id", "name": "节点名", "description": "节点说明", "type": "agent", "prompt": "提示词，可通过 {{field_name}} 引用输入字段或上游节点 {{upstream_id.field_name}} 的输出", "skills": [], "inputs": [...IO字段...], "outputs": [...IO字段...]}
  - 节点 type 为 "agent"（默认）、"human"（人工审核）或 "skill"（直接调用技能）
  - inputs / outputs 字段结构与 variables_def 完全一致
  - 相邻节点的输出字段 → 下游输入字段通过 name 自动传递，无需写 edges
- edges / entry_node 可以留空，系统会按 nodes 顺序自动串接。

重要：
- 每个节点的 inputs 必须明确列出该节点要用到的字段（来自工作流输入或上游输出）；
- outputs 必须明确列出该节点产出的字段；
- 字段类型必须准确：长内容用 document，结构化用 json，短文本用 text；
- 提示词要具体、可执行，告诉 LLM 要做什么、产出什么格式。

输出示例（"文章摘要与关键词提取" SOP）：
{
  "id": "article-summary-keywords",
  "name": "文章摘要与关键词提取",
  "version": "1.0.0",
  "description": "输入长文章，先生成 200 字摘要，再提取关键词列表，最后输出结构化 JSON。",
  "variables_def": [
    {"name": "article", "label": "文章正文", "type": "document", "description": "待处理的长文章正文", "required": true}
  ],
  "nodes": [
    {
      "id": "summarize", "name": "生成摘要", "type": "agent",
      "description": "阅读文章并生成 200 字以内的精炼摘要",
      "prompt": "请阅读输入的文章（见 article 字段），生成一段 200 字以内的中文摘要，覆盖核心观点。输出必须包含 summary 字段。",
      "skills": [],
      "inputs": [{"name": "article", "label": "文章正文", "type": "document", "description": "待摘要文章", "required": true}],
      "outputs": [{"name": "summary", "label": "摘要", "type": "text", "description": "200字以内摘要", "required": true}]
    },
    {
      "id": "extract_keywords", "name": "提取关键词", "type": "agent",
      "description": "基于摘要提取 5 个关键词",
      "prompt": "基于以下摘要提取 5 个最具代表性的中文关键词，以 JSON 数组形式放在 keywords 字段中。摘要：{{summarize.summary}}",
      "skills": [],
      "inputs": [{"name": "summary", "label": "摘要", "type": "text", "description": "上一步生成的摘要", "required": true}],
      "outputs": [{"name": "keywords", "label": "关键词", "type": "json", "description": "关键词数组", "required": true, "json_schema": {"type": "array", "items": {"type": "string"}}}]
    }
  ],
  "edges": [], "entry_node": ""
}"""


def _slugify(text: str) -> str:
    """把描述文本转成简短的 slug，供缺省 id 使用。"""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()[:32]).strip("-")
    return slug or f"sop-{uuid.uuid4().hex[:8]}"


def _parse_json(content: str) -> dict:
    """把 LLM 输出文本容错解析为字典（LLM 解析边界，允许 try）。"""
    if not content:
        raise ValueError("LLM 返回内容为空，无法解析为 SOP")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("无法将 LLM 输出解析为合法的 SOP JSON")


def _normalize_legacy_io(data: dict) -> dict:
    """把 LLM 可能输出的旧版 input_schema/output_schema 形式兜底处理。

    - 若节点存在 inputs/outputs（新版）则保持不动；
    - 若仅有旧版 schema，保留它们（模型层会走 fallback），但不做转换。
    - 移除 edges/entry_node 的空字符串/null，让模型 validator 自动补全。
    """
    if not data.get("edges"):
        data["edges"] = []
    if not data.get("entry_node"):
        data.pop("entry_node", None)
    nodes = data.get("nodes") or []
    for n in nodes:
        # 保证 inputs/outputs 字段存在（缺省为 []）
        n.setdefault("inputs", [])
        n.setdefault("outputs", [])
        n.setdefault("skills", [])
        n.setdefault("description", "")
    return data


class SOPGenerator:
    """基于 LLM 的 SOP 模板生成器。"""

    def __init__(self, llm_provider: LLMProvider) -> None:
        """初始化并保存 LLM Provider 依赖。"""
        self.llm_provider = llm_provider

    async def generate(self, description: str, sop_id: Optional[str] = None) -> SOPTemplate:
        """根据自然语言描述生成并返回一个 SOPTemplate。"""
        messages = [
            Message(role=Role.SYSTEM, content=_SYSTEM_PROMPT),
            Message(role=Role.USER, content=description),
        ]
        response = await self.llm_provider.chat(messages)
        content = response.choices[0].content if response.choices else None
        data = _parse_json(content or "")
        if not data.get("id"):
            data["id"] = sop_id if sop_id else _slugify(description)
        data = _normalize_legacy_io(data)
        return SOPTemplate(**data)
