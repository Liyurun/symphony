"""config 模块的单元测试。

验证 load_config 能够正确读取 YAML、完成环境变量插值并构建配置实例。
"""

from pathlib import Path

from symphony.config import load_config

# 最小化的配置内容，用于测试环境变量插值与默认值加载
_MINIMAL_CONFIG = """
llm:
  provider: doubao
  api_key: ${DOUBAO_API_KEY}
  model: doubao-pro-32k
  base_url: https://ark.cn-beijing.volces.com/api/v3
  temperature: 0.7
  max_tokens: 4096
server:
  host: 127.0.0.1
  port: 8899
  auto_open_browser: true
storage:
  workspace_dir: ~/.symphony/workspaces
  templates_dir: ~/.symphony/templates
  custom_skills_dir: ~/.symphony/skills
"""

_OVERRIDE_CONFIG = """
llm:
  provider: doubao
  api_key: explicit-key
  model: doubao-pro-32k
  base_url: https://ark.cn-beijing.volces.com/api/v3
  temperature: 0.2
  max_tokens: 2048
  timeout_seconds: 45.5
server:
  host: 0.0.0.0
  port: 9000
  auto_open_browser: false
  port_scan_max_attempts: 7
  startup_wait_timeout_seconds: 1.5
storage:
  workspace_dir: ~/.symphony/workspaces
  templates_dir: ~/.symphony/templates
  custom_skills_dir: ~/.symphony/skills
runtime:
  chat:
    max_iterations: 8
    skill_reference_limit: 5
  agent:
    max_iterations: 9
    max_retries: 2
  context_compression:
    enabled: false
    max_prompt_chars: 1000
    keep_recent_messages: 4
    min_recent_messages: 2
    summary_max_chars: 300
    max_message_chars: 500
skills:
  http_request:
    timeout_seconds: 11.5
  skill_references:
    default_limit: 6
    max_limit: 9
  workspace:
    bash_timeout_seconds: 12
    max_output_chars: 2000
    list_files_max_results: 17
    search_max_results: 18
  python_execute:
    timeout_seconds: 13
client:
  http_timeout_seconds: 14.5
  tui_input_history_limit: 3
  chat_context_history_limit: 4
"""


def test_load_config_keeps_old_config_compatible(tmp_path: Path, monkeypatch):
    """旧配置缺少新增字段时仍能加载，并自动补齐新增默认值。"""
    # 设置测试用环境变量，供 ${DOUBAO_API_KEY} 插值使用
    monkeypatch.setenv("DOUBAO_API_KEY", "test-key")
    # 在临时目录写入最小配置文件
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_MINIMAL_CONFIG, encoding="utf-8")

    # 调用被测函数加载配置
    config = load_config(config_file)

    # 断言：api_key 已被环境变量插值替换
    assert config.llm.api_key == "test-key"
    # 断言：provider 字段正确
    assert config.llm.provider == "doubao"
    # 断言：server.port 字段正确
    assert config.server.port == 8899
    # 断言：新增 LLM、server 字段使用默认值
    assert config.llm.timeout_seconds == 120.0
    assert config.server.port_scan_max_attempts == 100
    assert config.server.startup_wait_timeout_seconds == 5.0
    # 断言：新增 runtime 配置段使用默认值
    assert config.runtime.chat.max_iterations == 999
    assert config.runtime.chat.skill_reference_limit == 999
    assert config.runtime.agent.max_iterations == 20
    assert config.runtime.agent.max_retries == 3
    assert config.runtime.context_compression.enabled is True
    assert config.runtime.context_compression.max_prompt_chars == 120_000
    assert config.runtime.context_compression.keep_recent_messages == 24
    assert config.runtime.context_compression.min_recent_messages == 6
    assert config.runtime.context_compression.summary_max_chars == 4_000
    assert config.runtime.context_compression.max_message_chars == 16_000
    # 断言：新增 skills/client 配置段使用默认值
    assert config.skills.http_request.timeout_seconds == 30.0
    assert config.skills.workspace.bash_timeout_seconds == 30
    assert config.skills.workspace.max_output_chars == 12_000
    assert config.skills.workspace.list_files_max_results == 200
    assert config.skills.workspace.search_max_results == 80
    assert config.skills.python_execute.timeout_seconds == 30
    assert config.skills.skill_references.default_limit == 20
    assert config.skills.skill_references.max_limit == 50
    assert config.client.http_timeout_seconds == 30.0
    assert config.client.tui_input_history_limit == 100
    assert config.client.chat_context_history_limit == 24


def test_load_config_allows_explicit_new_config_overrides(tmp_path: Path):
    """显式声明新增嵌套字段时，加载结果应反映 YAML 中的覆盖值。"""
    # 在临时目录写入带新增配置段的配置文件
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_OVERRIDE_CONFIG, encoding="utf-8")

    # 调用被测函数加载配置
    config = load_config(config_file)

    # 断言：LLM 与 server 新字段可覆盖
    assert config.llm.timeout_seconds == 45.5
    assert config.server.port_scan_max_attempts == 7
    assert config.server.startup_wait_timeout_seconds == 1.5
    # 断言：runtime 新字段可覆盖
    assert config.runtime.chat.max_iterations == 8
    assert config.runtime.chat.skill_reference_limit == 5
    assert config.runtime.agent.max_iterations == 9
    assert config.runtime.agent.max_retries == 2
    assert config.runtime.context_compression.enabled is False
    assert config.runtime.context_compression.max_prompt_chars == 1000
    assert config.runtime.context_compression.keep_recent_messages == 4
    assert config.runtime.context_compression.min_recent_messages == 2
    assert config.runtime.context_compression.summary_max_chars == 300
    assert config.runtime.context_compression.max_message_chars == 500
    # 断言：skills/client 新字段可覆盖
    assert config.skills.http_request.timeout_seconds == 11.5
    assert config.skills.workspace.bash_timeout_seconds == 12
    assert config.skills.workspace.max_output_chars == 2000
    assert config.skills.workspace.list_files_max_results == 17
    assert config.skills.workspace.search_max_results == 18
    assert config.skills.python_execute.timeout_seconds == 13
    assert config.skills.skill_references.default_limit == 6
    assert config.skills.skill_references.max_limit == 9
    assert config.client.http_timeout_seconds == 14.5
    assert config.client.tui_input_history_limit == 3
    assert config.client.chat_context_history_limit == 4


def test_default_config_includes_new_sections(monkeypatch):
    """缺少 config.yaml 时，CLI 内置默认配置也应包含新增配置段。"""
    # 设置默认配置读取的 LLM key，避免依赖调用环境
    monkeypatch.setenv("DOUBAO_API_KEY", "default-key")
    from symphony.cli import _default_config

    # 构造 CLI 内置默认配置
    config = _default_config()

    # 断言：旧字段保持默认值
    assert config.llm.api_key == "default-key"
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8899
    # 断言：新增字段与模型默认值对齐
    assert config.llm.timeout_seconds == 120.0
    assert config.server.port_scan_max_attempts == 100
    assert config.server.startup_wait_timeout_seconds == 5.0
    assert config.runtime.chat.max_iterations == 999
    assert config.runtime.agent.max_iterations == 20
    assert config.runtime.agent.max_retries == 3
    assert config.runtime.context_compression.max_prompt_chars == 120_000
    assert config.skills.http_request.timeout_seconds == 30.0
    assert config.skills.workspace.bash_timeout_seconds == 30
    assert config.skills.workspace.max_output_chars == 12_000
    assert config.skills.workspace.list_files_max_results == 200
    assert config.skills.workspace.search_max_results == 80
    assert config.skills.python_execute.timeout_seconds == 30
    assert config.skills.skill_references.default_limit == 20
    assert config.skills.skill_references.max_limit == 50
    assert config.client.http_timeout_seconds == 30.0
    assert config.client.tui_input_history_limit == 100
    assert config.client.chat_context_history_limit == 24
