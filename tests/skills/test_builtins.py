"""内置技能的单元测试。

覆盖 HttpRequestSkill（用 httpx_mock 拦截请求）、FileReadSkill/FileWriteSkill
（用临时目录读写）与 PythonExecuteSkill（执行受限代码并捕获 stdout）。
"""

import tempfile
from pathlib import Path

import pytest

import symphony.skills.builtins.http_request as http_request_module
from symphony.config import (
    HttpRequestSkillConfig,
    PythonExecuteSkillConfig,
    SkillsConfig,
    WorkspaceSkillConfig,
)
from symphony.skills import SkillContext
from symphony.skills.builtins import (
    BashExecuteSkill,
    FilePatchSkill,
    FileReadSkill,
    FileWriteSkill,
    HttpRequestSkill,
    PythonExecuteSkill,
    WorkspaceListFilesSkill,
    WorkspaceSearchSkill,
    register_builtins,
)
from symphony.skills.registry import SkillRegistry


def _ctx() -> SkillContext:
    """构造一个用于测试的最小 SkillContext。"""
    return SkillContext(task_id="t", node_id="n")


@pytest.mark.asyncio
async def test_http_request_skill(httpx_mock):
    """GET 请求应返回 status_code=200 且解析出的 json 与 mock 一致。"""
    httpx_mock.add_response(url="https://example.com/api", json={"ok": True})
    skill = HttpRequestSkill()
    result = await skill.execute(
        {"method": "GET", "url": "https://example.com/api"}, _ctx()
    )
    assert result["status_code"] == 200
    assert result["json"] == {"ok": True}


@pytest.mark.asyncio
async def test_http_request_timeout_arg_overrides_default(monkeypatch):
    """http_request 单次调用 timeout 应覆盖注册时的默认 timeout。"""
    captured_timeouts = []

    class FakeResponse:
        """最小响应对象，供 HttpRequestSkill 解析。"""

        status_code = 200
        headers = {"content-type": "text/plain"}
        text = "ok"

    class FakeAsyncClient:
        """记录 AsyncClient 收到的 timeout，不发真实网络请求。"""

        def __init__(self, timeout):
            captured_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(http_request_module.httpx, "AsyncClient", FakeAsyncClient)
    skill = HttpRequestSkill(timeout=7.5)

    await skill.execute({"url": "https://example.com/default"}, _ctx())
    await skill.execute({"url": "https://example.com/override", "timeout": 1.25}, _ctx())

    assert captured_timeouts == [7.5, 1.25]


@pytest.mark.asyncio
async def test_file_read_write_skill():
    """先写入再读取，读到的内容应与写入内容一致。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "note.txt")
        write_skill = FileWriteSkill()
        write_result = await write_skill.execute(
            {"path": path, "content": "hello world"}, _ctx()
        )
        assert write_result["bytes_written"] > 0

        read_skill = FileReadSkill()
        read_result = await read_skill.execute({"path": path}, _ctx())
        assert read_result["content"] == "hello world"


@pytest.mark.asyncio
async def test_python_execute_skill():
    """执行简单代码应成功且 stdout 捕获到 print 输出。"""
    skill = PythonExecuteSkill()
    result = await skill.execute({"code": "x = 1 + 1\nprint(x)"}, _ctx())
    assert result["success"] is True
    assert "2" in result["stdout"]


def test_register_builtins_applies_skill_config_defaults():
    """register_builtins 应把配置默认值写入对应内置技能实例。"""
    registry = SkillRegistry()
    config = SkillsConfig(
        http_request=HttpRequestSkillConfig(timeout_seconds=7.5),
        workspace=WorkspaceSkillConfig(
            bash_timeout_seconds=6,
            max_output_chars=1500,
            list_files_max_results=3,
            search_max_results=4,
        ),
        python_execute=PythonExecuteSkillConfig(timeout_seconds=9),
    )

    register_builtins(registry, skills=config)

    http_skill = registry.get("http_request")
    bash_skill = registry.get("bash_execute")
    list_files_skill = registry.get("workspace_list_files")
    search_skill = registry.get("workspace_search")
    python_skill = registry.get("python_execute")
    assert isinstance(http_skill, HttpRequestSkill)
    assert isinstance(bash_skill, BashExecuteSkill)
    assert isinstance(list_files_skill, WorkspaceListFilesSkill)
    assert isinstance(search_skill, WorkspaceSearchSkill)
    assert isinstance(python_skill, PythonExecuteSkill)
    assert http_skill.timeout == 7.5
    assert http_skill.input_schema["properties"]["timeout"]["default"] == 7.5
    assert bash_skill.default_timeout == 6
    assert bash_skill.default_max_output_chars == 1500
    assert bash_skill.input_schema["properties"]["timeout"]["default"] == 6
    assert bash_skill.input_schema["properties"]["max_output_chars"]["default"] == 1500
    assert list_files_skill.default_max_results == 3
    assert list_files_skill.input_schema["properties"]["max_results"]["default"] == 3
    assert search_skill.default_max_results == 4
    assert search_skill.input_schema["properties"]["max_results"]["default"] == 4
    assert python_skill.default_timeout == 9
    assert python_skill.input_schema["properties"]["timeout"]["default"] == 9


@pytest.mark.asyncio
async def test_workspace_list_files_skill(tmp_path: Path):
    """workspace_list_files 应列出工作区内相对路径。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")

    skill = WorkspaceListFilesSkill()
    result = await skill.execute({"root": str(tmp_path), "path": "src"}, _ctx())

    paths = [item["path"] for item in result["entries"]]
    assert "src/app.py" in paths
    assert result["root"] == str(tmp_path)


@pytest.mark.asyncio
async def test_workspace_list_files_args_override_configured_defaults(tmp_path: Path):
    """workspace_list_files 单次调用 max_results 应优先于配置默认值。"""
    for index in range(4):
        (tmp_path / f"file_{index}.txt").write_text(str(index), encoding="utf-8")

    skill = WorkspaceListFilesSkill(max_results=2)
    default_result = await skill.execute({"root": str(tmp_path)}, _ctx())
    override_result = await skill.execute(
        {"root": str(tmp_path), "max_results": 3},
        _ctx(),
    )

    assert len(default_result["entries"]) == 2
    assert default_result["truncated"] is True
    assert len(override_result["entries"]) == 3
    assert override_result["truncated"] is True


@pytest.mark.asyncio
async def test_workspace_search_skill_content(tmp_path: Path):
    """workspace_search 应搜索文件内容并返回行号。"""
    (tmp_path / "a.txt").write_text("alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("nothing\n", encoding="utf-8")

    skill = WorkspaceSearchSkill()
    result = await skill.execute({"root": str(tmp_path), "query": "needle"}, _ctx())

    assert result["matches"][0]["path"] == "a.txt"
    assert result["matches"][0]["line"] == 2
    assert "needle here" in result["matches"][0]["text"]


@pytest.mark.asyncio
async def test_workspace_search_args_override_configured_defaults(tmp_path: Path):
    """workspace_search 单次调用 max_results 应优先于配置默认值。"""
    for index in range(4):
        (tmp_path / f"target_{index}.txt").write_text("content", encoding="utf-8")

    skill = WorkspaceSearchSkill(max_results=1)
    default_result = await skill.execute(
        {"root": str(tmp_path), "query": "target", "mode": "filename"},
        _ctx(),
    )
    override_result = await skill.execute(
        {
            "root": str(tmp_path),
            "query": "target",
            "mode": "filename",
            "max_results": 2,
        },
        _ctx(),
    )

    assert len(default_result["matches"]) == 1
    assert default_result["truncated"] is True
    assert len(override_result["matches"]) == 2
    assert override_result["truncated"] is True


@pytest.mark.asyncio
async def test_python_execute_timeout_arg_overrides_configured_default():
    """python_execute 单次调用 timeout 应控制真实执行超时。"""
    skill = PythonExecuteSkill(timeout=5)

    result = await skill.execute(
        {"code": "while True:\n    pass", "timeout": 1},
        _ctx(),
    )

    assert result["success"] is False
    assert result["timed_out"] is True
    assert "timed out after 1 seconds" in result["stderr"]


@pytest.mark.asyncio
async def test_file_patch_skill_replaces_exact_text(tmp_path: Path):
    """file_patch 应用精确文本替换修改工作区文件。"""
    target = tmp_path / "note.txt"
    target.write_text("hello old world", encoding="utf-8")

    skill = FilePatchSkill()
    result = await skill.execute(
        {
            "root": str(tmp_path),
            "path": "note.txt",
            "old_text": "old",
            "new_text": "new",
        },
        _ctx(),
    )

    assert result["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "hello new world"


@pytest.mark.asyncio
async def test_file_patch_skill_blocks_path_escape(tmp_path: Path):
    """file_patch 不应允许修改工作区外文件。"""
    skill = FilePatchSkill()

    with pytest.raises(ValueError, match="escapes workspace root"):
        await skill.execute(
            {
                "root": str(tmp_path),
                "path": "../outside.txt",
                "old_text": "a",
                "new_text": "b",
            },
            _ctx(),
        )


@pytest.mark.asyncio
async def test_bash_execute_skill_runs_short_command(tmp_path: Path):
    """bash_execute 应执行短命令并返回 stdout。"""
    skill = BashExecuteSkill()
    result = await skill.execute(
        {"root": str(tmp_path), "command": "printf hello", "timeout": 5},
        _ctx(),
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "hello"
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_bash_execute_args_override_configured_defaults(tmp_path: Path):
    """bash_execute 单次调用参数应优先于配置默认值。"""
    skill = BashExecuteSkill(timeout=5, max_output_chars=1200)
    command = "printf '%*s' 1500 '' | tr ' ' x"

    default_result = await skill.execute(
        {"root": str(tmp_path), "command": command},
        _ctx(),
    )
    override_result = await skill.execute(
        {"root": str(tmp_path), "command": command, "max_output_chars": 1400},
        _ctx(),
    )

    assert len(default_result["stdout"]) == 1200
    assert default_result["stdout_truncated"] is True
    assert len(override_result["stdout"]) == 1400
    assert override_result["stdout_truncated"] is True


@pytest.mark.asyncio
async def test_bash_execute_skill_blocks_destructive_command(tmp_path: Path):
    """bash_execute 应拦截明显破坏性命令。"""
    skill = BashExecuteSkill()

    with pytest.raises(ValueError, match="Blocked"):
        await skill.execute(
            {"root": str(tmp_path), "command": "rm -rf ."},
            _ctx(),
        )
