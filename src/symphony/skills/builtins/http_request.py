"""HTTP 请求技能。

基于 httpx.AsyncClient 发送 HTTP 请求，并将响应统一整理为字典返回，
支持根据 content-type 自动解析 JSON 或退化为文本。
"""

from copy import deepcopy
from typing import Any

import httpx

from symphony.skills.base import Skill, SkillContext


DEFAULT_TIMEOUT = 30.0


def _safe_float(value: Any, default: float, lower: float, upper: float) -> float:
    """把用户传入的浮点数值限制在合理范围内。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(number, upper))


class HttpRequestSkill(Skill):
    """发送 HTTP 请求并返回结构化响应的技能。"""

    # 技能名称
    name = "http_request"
    # 技能描述
    description = "Send HTTP request and return response"
    # 输入参数 schema
    input_schema = {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                "default": "GET",
            },
            "url": {"type": "string"},
            "headers": {"type": "object"},
            "json": {"type": "object"},
            "params": {"type": "object"},
            "body": {"type": "string"},
            "timeout": {"type": "number", "default": DEFAULT_TIMEOUT},
        },
        "required": ["url"],
    }
    # 输出结果 schema
    output_schema = {"type": "object"}

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        """初始化技能。

        :param timeout: HTTP 请求超时时间（秒）。
        """
        # 默认请求超时时间，注册时可由配置覆盖
        self.timeout = _safe_float(timeout, DEFAULT_TIMEOUT, 0.1, 300.0)
        # 每个实例持有独立 schema，避免配置默认值污染其它注册中心
        self.input_schema = deepcopy(type(self).input_schema)
        self.input_schema["properties"]["timeout"]["default"] = self.timeout

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """发送 HTTP 请求并整理响应为字典。"""
        # 请求方法，默认 GET
        method = args.get("method", "GET")
        # 目标地址
        url = args["url"]
        # 单次调用参数优先于注册时配置的默认值
        timeout = _safe_float(args.get("timeout"), self.timeout, 0.1, 300.0)
        # 网络边界：使用异步客户端发起请求
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method,
                url,
                headers=args.get("headers"),
                json=args.get("json"),
                params=args.get("params"),
                content=args.get("body"),
            )

        # 组装基础响应信息
        result: dict[str, Any] = {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
        }
        # 根据 content-type 决定解析为 JSON 还是文本
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            # JSON 解析边界：解析失败时退化为文本
            try:
                result["json"] = resp.json()
            except ValueError:
                result["text"] = resp.text
        else:
            result["text"] = resp.text
        return result
