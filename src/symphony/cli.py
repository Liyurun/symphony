"""Symphony 命令行入口（CLI）。

基于标准库 argparse 提供四个子命令：

- ``symphony server``：启动本地 FastAPI 服务（可覆盖 host/port、控制是否开浏览器）。
- ``symphony run``：无头执行一个 SOP，把事件流打印到 stdout。
- ``symphony sop``：SOP 模板管理（list/show/delete/create）。
- ``symphony tui``：终端 UI 占位（MVP 未实现）。

设计上把「构建 config」「create_app」「真正阻塞启动（_serve）」拆成可打桩的小函数，
以便测试通过 monkeypatch 替换 create_app / _serve，无需真正起服务或调用 LLM。
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from symphony.ai.doubao import DoubaoProvider
from symphony.config import (
    ClientConfig,
    LLMConfig,
    RuntimeConfig,
    ServerConfig,
    SkillsConfig,
    StorageConfig,
    SymphonyConfig,
    load_config,
)
from symphony.server.app import create_app
from symphony.server.eventbus import EventBus
from symphony.server.manager import TaskManager
from symphony.skills.builtins import register_builtins
from symphony.skills.loader import load_custom_skills
from symphony.skills.registry import SkillRegistry
from symphony.storage.workspace import WorkspaceManager
from symphony.workflow.models import SOPTemplate
from symphony.workflow.template import TemplateLoader


def _default_config() -> SymphonyConfig:
    """在缺少 config.yaml 时构造一套合理的内置默认配置。

    LLM 的 api_key 从环境变量 ``DOUBAO_API_KEY`` 读取（缺失则为空字符串），
    存储路径统一落在用户主目录下的 ``~/.symphony/`` 内。
    """
    # 用户主目录下的 Symphony 根目录
    home_root = Path.home() / ".symphony"
    return SymphonyConfig(
        llm=LLMConfig(
            provider="doubao",
            api_key=os.environ.get("DOUBAO_API_KEY", ""),
            model="doubao-pro-32k",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            temperature=0.7,
            max_tokens=4096,
            timeout_seconds=120.0,
        ),
        server=ServerConfig(
            host="127.0.0.1",
            port=8899,
            auto_open_browser=True,
            port_scan_max_attempts=100,
            startup_wait_timeout_seconds=5.0,
        ),
        storage=StorageConfig(
            workspace_dir=str(home_root / "workspaces"),
            templates_dir=str(home_root / "templates"),
            custom_skills_dir=str(home_root / "skills"),
        ),
        runtime=RuntimeConfig(),
        skills=SkillsConfig(),
        client=ClientConfig(),
    )


def build_config(config_path: str = "config.yaml") -> SymphonyConfig:
    """加载配置文件；文件不存在时打印提示并回退到内置默认配置。"""
    # 配置文件存在则正常加载
    path = Path(config_path)
    if path.exists():
        return load_config(path)
    # 文件缺失：提示用户并使用内置默认值，保证健壮性
    print(f"[symphony] 未找到配置文件 {config_path}，使用内置默认配置。")
    return _default_config()


def parse_var(item: str) -> tuple[str, object]:
    """把单个 ``k=v`` 字符串解析为 (key, value)。

    value 优先用 json.loads 解析（支持数字/布尔/数组/对象等），
    解析失败时退化为原始字符串。
    """
    # 仅按第一个 = 分割，允许 value 中包含 =
    key, _, raw = item.partition("=")
    # 尝试按 JSON 解析（解析边界，允许 try）
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return key, value


def parse_vars(items: list[str] | None) -> dict:
    """把多个 ``k=v`` 字符串解析为变量字典。"""
    # 未提供时返回空字典
    if not items:
        return {}
    # 逐项解析并汇总
    return dict(parse_var(item) for item in items)


def _serve(app, host: str, port: int) -> None:
    """真正阻塞式启动服务；抽成独立函数以便测试打桩替换。"""
    # 延迟导入 uvicorn，避免非 server 场景引入额外开销
    import uvicorn

    uvicorn.run(app, host=host, port=port)


def _port_available(host: str, port: int) -> bool:
    """检查指定 host/port 当前是否可以绑定。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _next_available_port(host: str, start_port: int, max_attempts: int = 100) -> int:
    """从 start_port 开始向上寻找第一个可用端口。"""
    for port in range(start_port, start_port + max_attempts):
        if _port_available(host, port):
            return port
    raise RuntimeError(f"未找到可用端口: {start_port}-{start_port + max_attempts - 1}")


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    """等待后台 server 端口就绪，避免 TUI 过早连接失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"server 启动超时: http://{host}:{port}")


def _prepare_server(config_path: str, host_override: str | None = None, port_override: int | None = None):
    """加载配置、应用覆盖项并完成端口自增选择。"""
    config = build_config(config_path)
    if host_override is not None:
        config.server.host = host_override
    if port_override is not None:
        config.server.port = port_override

    host = config.server.host
    requested_port = config.server.port
    port = _next_available_port(
        host,
        requested_port,
        max_attempts=config.server.port_scan_max_attempts,
    )
    if port != requested_port:
        print(f"[symphony] 端口 {requested_port} 已被占用，自动切换到 {port}。")
    config.server.port = port
    return config, host, port


def cmd_server(args: argparse.Namespace) -> int:
    """启动本地 FastAPI 服务，支持 host/port 覆盖与浏览器自动打开控制。"""
    # 加载配置、应用命令行覆盖，并自动避开已占用端口。
    config, host, port = _prepare_server(args.config, args.host, args.port)
    app = create_app(config)

    # 是否自动打开浏览器：配置开启且未显式 --no-browser
    if config.server.auto_open_browser and not args.no_browser:
        # 延迟 1 秒后在后台线程打开浏览器，避免阻塞服务启动
        url = f"http://{host}:{port}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # 阻塞启动（测试中此函数被打桩）
    _serve(app, host, port)
    return 0


def cmd_symphony_default(config_path: str) -> int:
    """Symphony 无参数入口：后台启动 server，然后进入 TUI。"""
    config, host, port = _prepare_server(config_path)
    app = create_app(config)

    print(f"[symphony] 后台启动 server: http://{host}:{port}")
    thread = threading.Thread(target=_serve, args=(app, host, port), daemon=True)
    thread.start()
    _wait_for_server(
        host,
        port,
        timeout=config.server.startup_wait_timeout_seconds,
    )

    from symphony.tui import run_tui

    run_tui(
        f"http://{host}:{port}",
        http_timeout=config.client.http_timeout_seconds,
        input_history_limit=config.client.tui_input_history_limit,
        chat_context_history_limit=config.client.chat_context_history_limit,
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """无头执行一个 SOP，把事件流简洁打印到 stdout。"""
    # 加载配置并解析 --var
    config = build_config(args.config)
    variables = parse_vars(args.var)

    # 装配运行所需依赖（与 server 那套一致，但独立构造）
    provider = DoubaoProvider(
        api_key=config.llm.api_key,
        model=config.llm.model,
        base_url=config.llm.base_url,
        temperature=config.llm.temperature,
        max_tokens=config.llm.max_tokens,
        timeout=config.llm.timeout_seconds,
    )
    template_loader = TemplateLoader(config.storage.templates_dir)
    workspace_manager = WorkspaceManager(config.storage.workspace_dir)
    event_bus = EventBus()
    skill_registry = SkillRegistry()
    register_builtins(skill_registry, skills=config.skills)
    skill_load_result = load_custom_skills(skill_registry, config.storage.custom_skills_dir)
    for error in skill_load_result.errors:
        print(f"[symphony] 自定义 Skill 加载失败: {error.path}: {error.error}", file=sys.stderr)
    task_manager = TaskManager(
        template_loader=template_loader,
        workspace_manager=workspace_manager,
        llm_provider=provider,
        event_bus=event_bus,
        skill_registry=skill_registry,
        agent_max_iterations=config.runtime.agent.max_iterations,
        agent_max_retries=config.runtime.agent.max_retries,
        context_compression_config=config.runtime.context_compression,
    )

    # SOP 不存在时直接报错返回 1（避免进入异步执行）
    if template_loader.load(args.sop_id) is None:
        print(f"[symphony] SOP 不存在: {args.sop_id}", file=sys.stderr)
        return 1

    def print_handler(event: dict) -> None:
        """把一条事件字典简洁打印为 [type] node_id。"""
        # node_id 可能缺失，用空字符串兜底
        print(f"[{event['type']}] {event.get('node_id', '')}".rstrip())

    async def _drive() -> None:
        """启动任务、订阅事件、等待后台执行完成。"""
        # 启动任务，拿到 task_id
        task_id = await task_manager.start_task(args.sop_id, variables)
        # 订阅该任务的事件流并打印
        event_bus.subscribe(task_id, print_handler)
        # 等待后台执行任务跑完
        background = task_manager._tasks.get(task_id)
        if background is not None:
            await background
        # 关闭 HTTP 客户端，释放连接资源
        await provider.close()

    # 用 asyncio.run 驱动整个异步流程
    asyncio.run(_drive())
    return 0


def cmd_sop(args: argparse.Namespace) -> int:
    """SOP 模板管理：list / show / delete / create。"""
    # 加载配置并构造模板加载器；--templates-dir 可覆盖 config 中的目录
    config = build_config(args.config)
    templates_dir = args.templates_dir or config.storage.templates_dir
    loader = TemplateLoader(templates_dir)

    # list：列出全部 SOP（id + name）
    if args.sop_command == "list":
        templates = loader.list_all()
        # 无模板时给出提示
        if not templates:
            print("(无 SOP 模板)")
            return 0
        # 逐条打印 id 与 name
        for template in templates:
            print(f"{template.id}\t{template.name}")
        return 0

    # show：打印指定 SOP 的缩进 JSON
    if args.sop_command == "show":
        template = loader.load(args.sop_id)
        # 不存在则报错返回 1
        if template is None:
            print(f"[symphony] SOP 不存在: {args.sop_id}", file=sys.stderr)
            return 1
        # 按别名导出并缩进打印
        print(json.dumps(template.model_dump(by_alias=True), indent=2, ensure_ascii=False))
        return 0

    # delete：删除指定 SOP
    if args.sop_command == "delete":
        deleted = loader.delete(args.sop_id)
        # 不存在则报错返回 1
        if not deleted:
            print(f"[symphony] SOP 不存在: {args.sop_id}", file=sys.stderr)
            return 1
        print(f"[symphony] 已删除 SOP: {args.sop_id}")
        return 0

    # create：从 JSON 文件读入 SOPTemplate 并保存
    if args.sop_command == "create":
        source = Path(args.file)
        # 文件不存在则报错返回 1
        if not source.exists():
            print(f"[symphony] 文件不存在: {args.file}", file=sys.stderr)
            return 1
        # 解析 JSON 并构造模板后保存
        data = json.loads(source.read_text(encoding="utf-8"))
        template = SOPTemplate(**data)
        loader.save(template)
        print(f"[symphony] 已保存 SOP: {template.id}")
        return 0

    # 未指定 sop 子命令时报错
    print("[symphony] 请指定 sop 子命令: list / show / delete / create", file=sys.stderr)
    return 1


def cmd_tui(args: argparse.Namespace) -> int:
    """启动 Textual 终端 UI，连接本地 server。"""
    # 延迟导入：避免未安装 textual 时影响其它子命令
    from symphony.tui import run_tui

    config = build_config(args.config)
    # 由 --host/--port 拼出后端地址并启动 TUI（阻塞式运行）
    base_url = f"http://{args.host}:{args.port}"
    run_tui(
        base_url,
        http_timeout=config.client.http_timeout_seconds,
        input_history_limit=config.client.tui_input_history_limit,
        chat_context_history_limit=config.client.chat_context_history_limit,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器与全部子命令。"""
    # 顶层解析器
    parser = argparse.ArgumentParser(prog="symphony", description="Symphony 本地 SOP 工作流编排系统")
    # 子命令分发器
    subparsers = parser.add_subparsers(dest="command")

    # server 子命令
    server_parser = subparsers.add_parser("server", help="启动本地 Web 服务")
    server_parser.add_argument("--host", default=None, help="监听地址（覆盖配置）")
    server_parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置）")
    server_parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    server_parser.add_argument("--config", default="config.yaml", help="配置文件路径")

    # run 子命令
    run_parser = subparsers.add_parser("run", help="无头执行一个 SOP")
    run_parser.add_argument("sop_id", help="要执行的 SOP id")
    run_parser.add_argument("--var", action="append", metavar="K=V", help="工作流输入变量，可多次")
    run_parser.add_argument("--config", default="config.yaml", help="配置文件路径")

    # sop 子命令（含二级子命令）
    # 共享父解析器：把 --config / --templates-dir 挂到每个叶子子命令上，
    # 这样无论选项写在 "sop" 之后还是 "list/show/..." 之后都能识别。
    sop_common = argparse.ArgumentParser(add_help=False)
    sop_common.add_argument("--config", default="config.yaml", help="配置文件路径")
    sop_common.add_argument("--templates-dir", default=None, help="模板目录（覆盖配置）")
    sop_parser = subparsers.add_parser("sop", parents=[sop_common], help="SOP 模板管理")
    sop_sub = sop_parser.add_subparsers(dest="sop_command")
    # sop list
    sop_sub.add_parser("list", parents=[sop_common], help="列出全部 SOP")
    # sop show <sop_id>
    show_parser = sop_sub.add_parser("show", parents=[sop_common], help="打印指定 SOP 的 JSON")
    show_parser.add_argument("sop_id", help="SOP id")
    # sop delete <sop_id>
    delete_parser = sop_sub.add_parser("delete", parents=[sop_common], help="删除指定 SOP")
    delete_parser.add_argument("sop_id", help="SOP id")
    # sop create --file PATH
    create_parser = sop_sub.add_parser("create", parents=[sop_common], help="从 JSON 文件创建 SOP")
    create_parser.add_argument("--file", required=True, help="SOP JSON 文件路径")

    # tui 子命令：连接本地 server 的终端 UI，支持覆盖 host/port
    tui_parser = subparsers.add_parser("tui", help="启动终端 UI（连接本地 server）")
    tui_parser.add_argument("--host", default="127.0.0.1", help="server 地址")
    tui_parser.add_argument("--port", type=int, default=8899, help="server 端口")
    tui_parser.add_argument("--config", default="config.yaml", help="配置文件路径")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口：解析参数并分派到对应子命令处理函数。

    :param argv: 参数列表（默认取 sys.argv[1:]），便于测试注入。
    :return: 进程退出码（0 成功，非 0 失败）。
    """
    # 构建解析器并解析参数
    parser = _build_parser()
    args = parser.parse_args(argv)

    # 无子命令：打印帮助并返回 1
    if args.command is None:
        parser.print_help()
        return 1

    # 按子命令分派
    if args.command == "server":
        return cmd_server(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "sop":
        return cmd_sop(args)
    if args.command == "tui":
        return cmd_tui(args)

    # 理论上不会到达（argparse 已限制 choices）
    parser.print_help()
    return 1


def _preferred_local_config() -> str:
    """返回交互式启动时优先使用的本地配置文件路径。

    ``symphony`` 是面向本项目的便捷命令，因此优先使用项目内常见的
    ``config.local.yaml``；不存在时再回退到通用 ``config.yaml``。
    """
    if Path("config.local.yaml").exists():
        return "config.local.yaml"
    return "config.yaml"


def symphony(argv: list[str] | None = None) -> int:
    """Symphony 便捷入口。

    - 无参数或 ``code``：后台启动 server 并进入 TUI；
    - 有参数：完全透传给 ``main``，例如 ``symphony tui``。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["code"]:
        return cmd_symphony_default(_preferred_local_config())
    return main(args)


def adseek(argv: list[str] | None = None) -> int:
    """AdSeek 兼容入口。

    - 无参数或 ``code``：沿用默认交互入口，后台启动 server 并进入 TUI；
    - 其他参数：透传给 ``main``，保留 server/run/sop/tui 等子命令能力。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["code"]:
        return cmd_symphony_default(_preferred_local_config())
    return main(args)


if __name__ == "__main__":
    # 作为脚本运行时把退出码交给解释器
    sys.exit(main())
