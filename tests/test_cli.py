"""cli 模块的单元测试。

通过 monkeypatch 打桩 create_app 与 _serve，避免真正启动服务或调用 LLM，
覆盖：无子命令、tui 桩、sop list、server 参数解析与覆盖、--var 解析等场景。
"""

from pathlib import Path

from symphony import cli
from symphony.workflow.models import Node, SOPTemplate
from symphony.workflow.template import TemplateLoader

# 临时 config.yaml 模板；storage 各目录在测试中指向临时目录以隔离副作用
_CONFIG_TEMPLATE = """
llm:
  provider: doubao
  api_key: test
  model: doubao-pro-32k
  base_url: http://x
  temperature: 0.7
  max_tokens: 4096
{llm_extra}server:
  host: 127.0.0.1
  port: 8899
  auto_open_browser: false
{server_extra}storage:
  workspace_dir: {ws}
  templates_dir: {tpl}
  custom_skills_dir: {sk}
{runtime_extra}
"""


def _write_config(
    tmp_path: Path,
    templates_dir: Path | None = None,
    llm_extra: str = "",
    server_extra: str = "",
    runtime_extra: str = "",
) -> Path:
    """在临时目录写入一个可用的 config.yaml，返回其路径。"""
    # 未显式指定模板目录时，默认使用 tmp_path/tpl
    tpl = templates_dir if templates_dir is not None else (tmp_path / "tpl")
    # 渲染配置文本并落盘
    content = _CONFIG_TEMPLATE.format(
        ws=tmp_path / "ws",
        tpl=tpl,
        sk=tmp_path / "sk",
        llm_extra=llm_extra,
        server_extra=server_extra,
        runtime_extra=runtime_extra,
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


def test_cli_no_command(capsys):
    """无子命令时应打印帮助并返回非 0 退出码。"""
    # 空参数列表：main 应返回非 0
    assert cli.main([]) != 0


def test_cli_tui_launches(monkeypatch):
    """tui 子命令应调用 run_tui（用拼出的 base_url）并返回 0，不真正启动 App。"""
    # 捕获 run_tui 收到的参数
    captured = {}

    def fake_run_tui(
        base_url,
        http_timeout=30.0,
        input_history_limit=100,
        chat_context_history_limit=24,
    ):
        """记录传入的 base_url，不启动 Textual 应用。"""
        captured["base_url"] = base_url
        captured["http_timeout"] = http_timeout
        captured["input_history_limit"] = input_history_limit
        captured["chat_context_history_limit"] = chat_context_history_limit

    # cmd_tui 内部是 `from symphony.tui import run_tui`，故打桩 symphony.tui.run_tui
    import symphony.tui

    monkeypatch.setattr(symphony.tui, "run_tui", fake_run_tui)

    # 使用默认 host/port 调用 tui 子命令
    rc = cli.main(["tui"])

    # 退出码为 0，且 run_tui 被以默认地址调用
    assert rc == 0
    assert captured["base_url"] == "http://127.0.0.1:8899"


def test_cli_tui_uses_configured_client_limits(tmp_path, monkeypatch):
    """tui 子命令应从配置读取 TUI 客户端超时与历史保留数量。"""
    captured = {}

    def fake_run_tui(
        base_url,
        http_timeout=30.0,
        input_history_limit=100,
        chat_context_history_limit=24,
    ):
        """记录 run_tui 参数，不启动 Textual 应用。"""
        captured["base_url"] = base_url
        captured["http_timeout"] = http_timeout
        captured["input_history_limit"] = input_history_limit
        captured["chat_context_history_limit"] = chat_context_history_limit

    import symphony.tui

    cfg = _write_config(
        tmp_path,
        runtime_extra=(
            "client:\n"
            "  http_timeout_seconds: 4.5\n"
            "  tui_input_history_limit: 6\n"
            "  chat_context_history_limit: 7\n"
        ),
    )
    monkeypatch.setattr(symphony.tui, "run_tui", fake_run_tui)

    rc = cli.main(
        [
            "tui",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--config",
            str(cfg),
        ]
    )

    assert rc == 0
    assert captured["base_url"] == "http://0.0.0.0:9001"
    assert captured["http_timeout"] == 4.5
    assert captured["input_history_limit"] == 6
    assert captured["chat_context_history_limit"] == 7


def test_cli_sop_list(tmp_path, capsys):
    """sop list 应列出模板目录中的 SOP（含 id 与 name）。"""
    # 在临时模板目录保存一个 SOP
    tpl_dir = tmp_path / "tpl"
    loader = TemplateLoader(tpl_dir)
    template = SOPTemplate(
        id="demo", name="演示流程", nodes=[Node(id="n1", name="节点一")], entry_node="n1"
    )
    loader.save(template)
    # 写入指向该模板目录的临时 config.yaml
    cfg = _write_config(tmp_path, templates_dir=tpl_dir)

    # 执行 sop list
    rc = cli.main(["sop", "list", "--config", str(cfg)])

    # 退出码为 0，输出包含 id 与 name
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out
    assert "演示流程" in out


def test_cli_server_parses(tmp_path, monkeypatch):
    """server：--host/--port 覆盖生效，create_app 与 _serve 被正确调用。"""
    # 捕获桩函数收到的参数
    captured = {}

    def fake_create_app(config):
        """记录传入的 config 并返回占位 app。"""
        captured["config"] = config
        return object()

    def fake_serve(app, host, port):
        """记录启动时的 host/port，不真正起服务。"""
        captured["serve"] = (host, port)

    # 打桩 create_app 与 _serve
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "_serve", fake_serve)
    cfg = _write_config(tmp_path)

    # --port 9999 覆盖 config 的 8899
    rc = cli.main(["server", "--port", "9999", "--no-browser", "--config", str(cfg)])

    # 退出码为 0
    assert rc == 0
    # create_app 收到的 config.server.port 应被覆盖为 9999
    assert captured["config"].server.port == 9999
    # _serve 收到的端口也应为 9999
    assert captured["serve"][1] == 9999


def test_cli_server_auto_increments_port(tmp_path, monkeypatch, capsys):
    """server 启动时应在目标端口被占用后自动向上寻找可用端口。"""
    captured = {}

    def fake_create_app(config):
        """记录最终写入 app 的端口。"""
        captured["config_port"] = config.server.port
        return object()

    def fake_serve(app, host, port):
        """记录真正启动时使用的端口。"""
        captured["serve"] = (host, port)

    def fake_port_available(host, port):
        """模拟 8899 被占用，8900 可用。"""
        return port != 8899

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "_serve", fake_serve)
    monkeypatch.setattr(cli, "_port_available", fake_port_available)
    cfg = _write_config(tmp_path)

    rc = cli.main(["server", "--no-browser", "--config", str(cfg)])

    assert rc == 0
    assert captured["config_port"] == 8900
    assert captured["serve"] == ("127.0.0.1", 8900)
    assert "端口 8899 已被占用，自动切换到 8900" in capsys.readouterr().out


def test_cli_server_auto_increments_explicit_port(tmp_path, monkeypatch, capsys):
    """即使显式指定 --port，端口被占用时也应继续递增。"""
    captured = {}

    def fake_create_app(config):
        """记录最终写入 app 的端口。"""
        captured["config_port"] = config.server.port
        return object()

    def fake_serve(app, host, port):
        """记录真正启动时使用的端口。"""
        captured["serve"] = (host, port)

    def fake_port_available(host, port):
        """模拟 8899、8900 被占用，8901 可用。"""
        return port >= 8901

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "_serve", fake_serve)
    monkeypatch.setattr(cli, "_port_available", fake_port_available)
    cfg = _write_config(tmp_path)

    rc = cli.main(["server", "--port", "8899", "--no-browser", "--config", str(cfg)])

    assert rc == 0
    assert captured["config_port"] == 8901
    assert captured["serve"] == ("127.0.0.1", 8901)
    assert "端口 8899 已被占用，自动切换到 8901" in capsys.readouterr().out


def test_cli_server_uses_configured_port_scan_limit(tmp_path, monkeypatch):
    """server 启动端口探测次数应来自 config.server.port_scan_max_attempts。"""
    captured = {}

    def fake_create_app(config):
        """返回占位 app。"""
        captured["config_port"] = config.server.port
        return object()

    def fake_next_available_port(host, start_port, max_attempts=100):
        """记录端口探测上限并直接返回请求端口。"""
        captured["scan"] = (host, start_port, max_attempts)
        return start_port

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "_serve", lambda app, host, port: None)
    monkeypatch.setattr(cli, "_next_available_port", fake_next_available_port)
    cfg = _write_config(
        tmp_path,
        server_extra="  port_scan_max_attempts: 7\n",
    )

    rc = cli.main(["server", "--no-browser", "--config", str(cfg)])

    assert rc == 0
    assert captured["scan"] == ("127.0.0.1", 8899, 7)
    assert captured["config_port"] == 8899


def test_symphony_no_args_starts_server_and_tui_with_local_config(tmp_path, monkeypatch):
    """Symphony 无参数时应后台启动 server，并用实际端口进入 TUI。"""
    captured = {}

    def fake_create_app(config):
        """记录最终 server 配置并返回占位 app。"""
        captured["config_port"] = config.server.port
        return object()

    def fake_serve(app, host, port):
        """记录后台 server 的 host/port，不真正启动 uvicorn。"""
        captured["serve"] = (host, port)

    def fake_run_tui(
        base_url,
        http_timeout=30.0,
        input_history_limit=100,
        chat_context_history_limit=24,
    ):
        """记录 TUI 连接地址，不真正启动 Textual 应用。"""
        captured["base_url"] = base_url
        captured["http_timeout"] = http_timeout
        captured["input_history_limit"] = input_history_limit
        captured["chat_context_history_limit"] = chat_context_history_limit

    def fake_port_available(host, port):
        """模拟 8899 被占用，8900 可用。"""
        return port != 8899

    import symphony.tui

    cfg = _write_config(tmp_path)
    cfg.rename(tmp_path / "config.local.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "_serve", fake_serve)
    def fake_wait_for_server(host, port, timeout=5.0):
        """记录后台 server 就绪等待参数。"""
        captured["wait"] = (host, port, timeout)

    monkeypatch.setattr(cli, "_wait_for_server", fake_wait_for_server)
    monkeypatch.setattr(cli, "_port_available", fake_port_available)
    monkeypatch.setattr(symphony.tui, "run_tui", fake_run_tui)

    rc = cli.symphony([])

    assert rc == 0
    assert captured["config_port"] == 8900
    assert captured["serve"] == ("127.0.0.1", 8900)
    assert captured["wait"] == ("127.0.0.1", 8900, 5.0)
    assert captured["base_url"] == "http://127.0.0.1:8900"
    assert captured["http_timeout"] == 30.0
    assert captured["input_history_limit"] == 100
    assert captured["chat_context_history_limit"] == 24


def test_symphony_no_args_uses_configured_startup_wait(tmp_path, monkeypatch):
    """无参数交互入口等待 server 就绪的超时时间应来自配置。"""
    captured = {}

    def fake_create_app(config):
        """返回占位 app。"""
        return object()

    def fake_wait_for_server(host, port, timeout=5.0):
        """记录等待参数。"""
        captured["wait"] = (host, port, timeout)

    def fake_run_tui(
        base_url,
        http_timeout=30.0,
        input_history_limit=100,
        chat_context_history_limit=24,
    ):
        """记录 TUI 地址。"""
        captured["base_url"] = base_url
        captured["http_timeout"] = http_timeout
        captured["input_history_limit"] = input_history_limit
        captured["chat_context_history_limit"] = chat_context_history_limit

    import symphony.tui

    cfg = _write_config(
        tmp_path,
        server_extra="  startup_wait_timeout_seconds: 1.25\n",
        runtime_extra=(
            "client:\n"
            "  http_timeout_seconds: 3.5\n"
            "  tui_input_history_limit: 8\n"
            "  chat_context_history_limit: 9\n"
        ),
    )
    cfg.rename(tmp_path / "config.local.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "_serve", lambda app, host, port: None)
    monkeypatch.setattr(cli, "_wait_for_server", fake_wait_for_server)
    monkeypatch.setattr(cli, "_port_available", lambda host, port: True)
    monkeypatch.setattr(symphony.tui, "run_tui", fake_run_tui)

    rc = cli.symphony([])

    assert rc == 0
    assert captured["wait"] == ("127.0.0.1", 8899, 1.25)
    assert captured["base_url"] == "http://127.0.0.1:8899"
    assert captured["http_timeout"] == 3.5
    assert captured["input_history_limit"] == 8
    assert captured["chat_context_history_limit"] == 9


def test_cli_run_uses_configured_llm_and_agent_runtime(tmp_path, monkeypatch):
    """run 子命令应把 LLM timeout 和 Agent runtime 配置传给后端执行链。"""
    captured = {}

    class FakeProvider:
        """记录 DoubaoProvider 构造参数。"""

        def __init__(self, **kwargs):
            captured["provider_kwargs"] = kwargs

        async def close(self):
            """兼容 cmd_run 的资源释放。"""

    class FakeTaskManager:
        """截获 TaskManager 构造参数并模拟启动任务。"""

        def __init__(self, **kwargs):
            captured["task_manager_kwargs"] = kwargs
            self._tasks = {}

        async def start_task(self, sop_id, variables):
            captured["start_task"] = (sop_id, variables)
            return "task-1"

    def fake_register_builtins(registry, skills=None):
        """记录注册内置技能时收到的配置对象。"""
        captured["skills_config"] = skills

    tpl_dir = tmp_path / "tpl"
    loader = TemplateLoader(tpl_dir)
    loader.save(
        SOPTemplate(
            id="demo",
            name="演示流程",
            nodes=[Node(id="n1", name="节点一")],
            entry_node="n1",
        )
    )
    cfg = _write_config(
        tmp_path,
        templates_dir=tpl_dir,
        llm_extra="  timeout_seconds: 12.5\n",
        runtime_extra=(
            "runtime:\n"
            "  agent:\n"
            "    max_iterations: 6\n"
            "    max_retries: 2\n"
            "  context_compression:\n"
            "    enabled: false\n"
            "    max_prompt_chars: 1234\n"
            "skills:\n"
            "  http_request:\n"
            "    timeout_seconds: 10.5\n"
            "  workspace:\n"
            "    bash_timeout_seconds: 4\n"
            "    max_output_chars: 1800\n"
            "    list_files_max_results: 19\n"
            "    search_max_results: 20\n"
            "  python_execute:\n"
            "    timeout_seconds: 8\n"
        ),
    )
    monkeypatch.setattr(cli, "DoubaoProvider", FakeProvider)
    monkeypatch.setattr(cli, "TaskManager", FakeTaskManager)
    monkeypatch.setattr(cli, "register_builtins", fake_register_builtins)

    rc = cli.main(["run", "demo", "--var", "x=1", "--config", str(cfg)])

    assert rc == 0
    assert captured["provider_kwargs"]["timeout"] == 12.5
    task_manager_kwargs = captured["task_manager_kwargs"]
    assert task_manager_kwargs["agent_max_iterations"] == 6
    assert task_manager_kwargs["agent_max_retries"] == 2
    assert task_manager_kwargs["context_compression_config"].enabled is False
    assert task_manager_kwargs["context_compression_config"].max_prompt_chars == 1234
    assert captured["skills_config"].http_request.timeout_seconds == 10.5
    assert captured["skills_config"].workspace.bash_timeout_seconds == 4
    assert captured["skills_config"].workspace.max_output_chars == 1800
    assert captured["skills_config"].workspace.list_files_max_results == 19
    assert captured["skills_config"].workspace.search_max_results == 20
    assert captured["skills_config"].python_execute.timeout_seconds == 8
    assert captured["start_task"] == ("demo", {"x": 1})


def test_symphony_with_args_delegates_to_main(monkeypatch):
    """Symphony 带参数时应保持与 symphony CLI 一致。"""
    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "main", fake_main)

    rc = cli.symphony(["tui"])

    assert rc == 0
    assert captured["argv"] == ["tui"]


def test_symphony_code_starts_default_tui(monkeypatch):
    """symphony code 作为 adseek 外层包装兼容路径时也应进入 TUI。"""
    captured = {}

    def fake_default(config_path):
        """记录默认入口使用的配置文件路径。"""
        captured["config_path"] = config_path
        return 0

    monkeypatch.setattr(cli, "_preferred_local_config", lambda: "config.local.yaml")
    monkeypatch.setattr(cli, "cmd_symphony_default", fake_default)

    rc = cli.symphony(["code"])

    assert rc == 0
    assert captured["config_path"] == "config.local.yaml"


def test_adseek_no_args_starts_default_tui(monkeypatch):
    """adseek 无参数时应进入和 symphony 无参数一致的 TUI 入口。"""
    captured = {}

    def fake_default(config_path):
        """记录默认入口使用的配置文件路径。"""
        captured["config_path"] = config_path
        return 0

    monkeypatch.setattr(cli, "_preferred_local_config", lambda: "config.local.yaml")
    monkeypatch.setattr(cli, "cmd_symphony_default", fake_default)

    rc = cli.adseek([])

    assert rc == 0
    assert captured["config_path"] == "config.local.yaml"


def test_adseek_code_starts_default_tui(monkeypatch):
    """adseek code 应作为兼容快捷方式进入 TUI。"""
    captured = {}

    def fake_default(config_path):
        """记录默认入口使用的配置文件路径。"""
        captured["config_path"] = config_path
        return 0

    monkeypatch.setattr(cli, "_preferred_local_config", lambda: "config.local.yaml")
    monkeypatch.setattr(cli, "cmd_symphony_default", fake_default)

    rc = cli.adseek(["code"])

    assert rc == 0
    assert captured["config_path"] == "config.local.yaml"


def test_adseek_other_args_delegates_to_main(monkeypatch):
    """adseek 的非 code 参数继续透传给标准 CLI。"""
    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "main", fake_main)

    rc = cli.adseek(["tui"])

    assert rc == 0
    assert captured["argv"] == ["tui"]


def test_parse_var():
    """--var 解析：json.loads 成功取原值，失败退化为字符串。"""
    # "a=1" 应解析出整数 1
    assert cli.parse_var("a=1") == ("a", 1)
    # "b=hello" 无法 json 解析，退化为字符串
    assert cli.parse_var("b=hello") == ("b", "hello")
    # 批量解析成字典
    assert cli.parse_vars(["a=1", "b=hello"]) == {"a": 1, "b": "hello"}
