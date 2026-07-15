"""Symphony 配置模块。

负责从 YAML 文件加载配置，并对形如 ``${ENV_VAR}`` 的字符串做环境变量插值。
使用 pydantic 定义强类型的嵌套配置模型，保证配置结构清晰、校验完善。
"""

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# 匹配形如 ${ENV_VAR} 的占位符，捕获其中的环境变量名
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


class LLMConfig(BaseModel):
    """大模型（LLM）相关配置。"""

    # 模型服务提供方，例如 doubao / openai 等
    provider: str
    # 访问模型服务所需的 API Key
    api_key: str
    # 使用的模型名称
    model: str
    # 模型服务的基础 URL
    base_url: str
    # 采样温度，取值越大输出越随机，默认 0.7
    temperature: float = 0.7
    # 单次生成的最大 token 数，默认 4096
    max_tokens: int = 4096
    # LLM HTTP 请求超时时间（秒），默认沿用 DoubaoProvider 现有行为
    timeout_seconds: float = 120.0


class ChatRuntimeConfig(BaseModel):
    """默认对话运行时相关配置。"""

    # Chat ReAct 循环最大轮数，默认沿用现有 ChatRuntime 行为
    max_iterations: int = 999
    # 外部 Skill 参考检索条数上限，默认沿用现有 ChatRuntime 行为
    skill_reference_limit: int = 999


class AgentRuntimeConfig(BaseModel):
    """SOP Agent 运行时相关配置。"""

    # SOP Agent ReAct 循环最大轮数，默认沿用 AgentRuntime 现有行为
    max_iterations: int = 20
    # 输出 JSON 解析或 schema 校验失败后的最大重试次数
    max_retries: int = 3


class ContextCompressionConfig(BaseModel):
    """LLM 上下文压缩相关配置。"""

    # 是否启用上下文压缩
    enabled: bool = True
    # 发给模型的 prompt 字符数软上限
    max_prompt_chars: int = 120_000
    # 压缩时尽量保留的最近消息数
    keep_recent_messages: int = 24
    # 压缩时至少保留的最近消息数
    min_recent_messages: int = 6
    # 较早上下文摘要的最大字符数
    summary_max_chars: int = 4_000
    # 单条消息进入模型上下文前的最大字符数
    max_message_chars: int = 16_000


class RuntimeConfig(BaseModel):
    """运行时策略配置，覆盖 Chat、SOP Agent 与上下文压缩。"""

    # 默认对话运行时配置
    chat: ChatRuntimeConfig = Field(default_factory=ChatRuntimeConfig)
    # SOP Agent 运行时配置
    agent: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)
    # LLM 上下文压缩配置
    context_compression: ContextCompressionConfig = Field(default_factory=ContextCompressionConfig)


class ServerConfig(BaseModel):
    """本地服务（FastAPI）相关配置。"""

    # 服务监听地址，默认本机回环地址
    host: str = "127.0.0.1"
    # 服务监听端口，默认 8899
    port: int = 8899
    # 启动后是否自动打开浏览器，默认开启
    auto_open_browser: bool = True
    # CLI 启动 server 时最多向后探测的端口数量
    port_scan_max_attempts: int = 100
    # 后台 server 启动后等待端口就绪的最长时间（秒）
    startup_wait_timeout_seconds: float = 5.0


class StorageConfig(BaseModel):
    """本地存储路径相关配置。"""

    # 工作区目录，存放运行时产生的工作流数据
    workspace_dir: str
    # 会话目录，存放统一 session 日志；旧配置未声明时使用兼容默认值
    sessions_dir: str | None = None
    # 模板目录，存放 SOP 模板
    templates_dir: str
    # 自定义技能目录，存放用户扩展的技能
    custom_skills_dir: str

    @field_validator("workspace_dir", "sessions_dir", "templates_dir", "custom_skills_dir")
    @classmethod
    def _expand_user_path(cls, value: str | None) -> str | None:
        """将路径中的 ``~`` 展开为用户主目录的绝对路径。"""
        if value is None:
            return None
        return str(Path(value).expanduser())

    def resolved_sessions_dir(self) -> str:
        """返回 session 根目录；旧配置未声明时使用 workspace 同级目录。"""
        if self.sessions_dir:
            return self.sessions_dir
        return str(Path(self.workspace_dir).expanduser().parent / "sessions")


class HttpRequestSkillConfig(BaseModel):
    """HTTP 请求技能默认配置。"""

    # http_request 技能默认请求超时时间（秒）
    timeout_seconds: float = 30.0


class SkillReferenceApiConfig(BaseModel):
    """外部 Skill 参考查询 API 默认配置。"""

    # /api/skills/references 未传 limit 时使用的默认返回数量
    default_limit: int = Field(default=20, ge=1)
    # /api/skills/references 允许的最大返回数量
    max_limit: int = Field(default=50, ge=1)


class WorkspaceSkillConfig(BaseModel):
    """工作区技能默认配置。"""

    # bash_execute 技能默认命令超时时间（秒）
    bash_timeout_seconds: int = 30
    # bash_execute 技能默认 stdout/stderr 最大返回字符数
    max_output_chars: int = 12_000
    # workspace_list_files 技能默认返回结果数量上限
    list_files_max_results: int = 200
    # workspace_search 技能默认返回结果数量上限
    search_max_results: int = 80


class PythonExecuteSkillConfig(BaseModel):
    """Python 执行技能默认配置。"""

    # python_execute 技能默认执行超时时间（秒）
    timeout_seconds: int = 30


class SkillsConfig(BaseModel):
    """内置技能默认参数配置。"""

    # HTTP 请求技能默认参数
    http_request: HttpRequestSkillConfig = Field(default_factory=HttpRequestSkillConfig)
    # 外部 Skill 参考查询 API 默认参数
    skill_references: SkillReferenceApiConfig = Field(
        default_factory=SkillReferenceApiConfig
    )
    # 工作区相关技能默认参数
    workspace: WorkspaceSkillConfig = Field(default_factory=WorkspaceSkillConfig)
    # Python 执行技能默认参数
    python_execute: PythonExecuteSkillConfig = Field(default_factory=PythonExecuteSkillConfig)


class ClientConfig(BaseModel):
    """客户端请求配置。"""

    # TUI/本地客户端 HTTP 请求超时时间（秒）
    http_timeout_seconds: float = 30.0
    # TUI 输入框历史最多保留的已提交条数，0 表示不保留
    tui_input_history_limit: int = Field(default=100, ge=0)
    # TUI 发起 Chat WebSocket 请求时最多携带的历史消息数，0 表示不携带历史
    chat_context_history_limit: int = Field(default=24, ge=0)


class SymphonyConfig(BaseModel):
    """Symphony 顶层配置，聚合各子模块配置。"""

    # 大模型配置
    llm: LLMConfig
    # 服务配置
    server: ServerConfig
    # 存储配置
    storage: StorageConfig
    # Chat/SOP 运行时策略配置；旧配置缺失时自动使用默认值
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    # 内置技能默认参数；旧配置缺失时自动使用默认值
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    # TUI/本地客户端配置；旧配置缺失时自动使用默认值
    client: ClientConfig = Field(default_factory=ClientConfig)


def _interpolate_env_vars(value):
    """递归地对配置数据做环境变量插值。

    - 对字符串：将其中所有 ``${ENV_VAR}`` 替换为对应环境变量的值；
      找不到环境变量时保留空字符串。
    - 对字典 / 列表：递归处理每一个元素。
    - 其他类型：原样返回。
    """
    # 字符串：执行占位符替换
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda match: os.environ.get(match.group(1), ""), value)
    # 字典：逐键递归
    if isinstance(value, dict):
        return {key: _interpolate_env_vars(item) for key, item in value.items()}
    # 列表：逐项递归
    if isinstance(value, list):
        return [_interpolate_env_vars(item) for item in value]
    # 其余类型（int/float/bool/None 等）：原样返回
    return value


def load_config(path: str | Path = "config.yaml") -> SymphonyConfig:
    """从 YAML 文件加载并构建 Symphony 配置实例。

    加载流程：读取 YAML → 做环境变量插值 → 交由 pydantic 校验并实例化。

    :param path: 配置文件路径，默认当前目录下的 ``config.yaml``。
    :return: 构建完成的 :class:`SymphonyConfig` 实例。
    """
    # 统一转成 Path，便于后续读取
    config_path = Path(path)
    # 读取 YAML 原始文本并解析为 Python 数据结构
    raw_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # 对整个配置数据做环境变量插值
    interpolated = _interpolate_env_vars(raw_data)
    # 交由 pydantic 完成类型校验与模型构建
    return SymphonyConfig(**interpolated)
