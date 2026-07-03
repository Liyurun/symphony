"""Tests for the artifact domain module."""

from symphony.sop.artifact import (
    Artifact,
    ArtifactType,
    extract_artifact,
    validate_artifact_format,
)


def test_validate_feishu_ok():
    ok, err = validate_artifact_format(
        ArtifactType.FEISHU_DOC, "https://bytedance.feishu.cn/docx/abc123"
    )
    assert ok and err == ""


def test_validate_feishu_bad():
    ok, err = validate_artifact_format(ArtifactType.FEISHU_DOC, "http://example.com")
    assert not ok and err


def test_validate_empty():
    ok, err = validate_artifact_format(ArtifactType.SQL, "   ")
    assert not ok and err == "产物值不能为空"


def test_validate_link_ok_and_bad():
    assert validate_artifact_format(ArtifactType.LINK, "https://x.com/a")[0]
    assert not validate_artifact_format(ArtifactType.LINK, "not-a-url")[0]


def test_validate_sql_task_id_nonempty():
    assert validate_artifact_format(ArtifactType.SQL, "SELECT 1")[0]
    assert validate_artifact_format(ArtifactType.TASK_ID, "12345")[0]


def test_extract_fenced_json():
    text = 'done.\n```json\n{"artifact": {"type": "sql", "value": "SELECT 1", "label": "q"}}\n```'
    art = extract_artifact({"output": text}, ArtifactType.SQL)
    assert isinstance(art, Artifact)
    assert art.type == ArtifactType.SQL
    assert art.value == "SELECT 1"
    assert art.label == "q"


def test_extract_bare_json():
    text = '{"artifact": {"type": "task_id", "value": "T-99"}}'
    art = extract_artifact({"output": text}, ArtifactType.TASK_ID)
    assert art.type == ArtifactType.TASK_ID
    assert art.value == "T-99"


def test_extract_url_fallback():
    text = "文档已生成：https://bytedance.feishu.cn/docx/xyz789 请查收"
    art = extract_artifact({"output": text}, ArtifactType.FEISHU_DOC)
    assert art is not None
    assert art.value == "https://bytedance.feishu.cn/docx/xyz789"


def test_extract_text_takes_body():
    art = extract_artifact({"output": "  hello world  "}, ArtifactType.TEXT)
    assert art.type == ArtifactType.TEXT
    assert art.value == "hello world"


def test_extract_none_when_no_url_for_feishu():
    art = extract_artifact({"output": "no link here"}, ArtifactType.FEISHU_DOC)
    assert art is None


# ── needs_user_input extraction ──

from symphony.sop.artifact import extract_needs_user_input


def test_needs_input_fenced():
    text = ('缺少目标库名。\n```json\n{"needs_user_input": {"questions": '
            '[{"key": "db", "question": "正式库名?", "type": "text"}], "reason": "缺少库名"}}\n```')
    q = extract_needs_user_input({"output": text})
    assert q is not None
    assert q["questions"][0]["key"] == "db"
    assert q["questions"][0]["question"] == "正式库名?"
    assert q["reason"] == "缺少库名"


def test_needs_input_after_artifact_block():
    # A needs_user_input block appearing after another JSON object is still found.
    text = ('```json\n{"artifact": {"type": "text", "value": "x"}}\n```\n'
            '{"needs_user_input": {"questions": [{"question": "TTL?"}]}}')
    q = extract_needs_user_input({"output": text})
    assert q is not None
    assert q["questions"][0]["question"] == "TTL?"
    assert q["questions"][0]["key"]  # auto-filled


def test_needs_input_absent():
    assert extract_needs_user_input({"output": "just a normal answer"}) is None


def test_needs_input_empty_questions_ignored():
    text = '{"needs_user_input": {"questions": [], "reason": "x"}}'
    assert extract_needs_user_input({"output": text}) is None
