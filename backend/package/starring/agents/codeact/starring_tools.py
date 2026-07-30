"""starring_tools —— CodeAct 沙盒内工具桥客户端。

本文件由 CodeActMiddleware 在每次执行前注入沙盒 workspace/.codeact/ 目录，
仅依赖 Python 标准库，不要求沙盒镜像预装任何包。
bridge 地址与凭证通过环境变量 STARRING_BRIDGE_URL / STARRING_BRIDGE_TOKEN 传入。
"""

import json
import os
import urllib.error
import urllib.request


class ToolCallError(Exception):
    """平台工具调用失败（含服务端返回的错误类型与信息）。"""

    def __init__(self, error_type, message):
        super().__init__(f"[{error_type}] {message}")
        self.error_type = error_type
        self.message = message


def call(tool, **kwargs):
    """调用平台工具，返回工具结果；失败抛 ToolCallError。

    用法示例::

        import starring_tools
        result = starring_tools.call("query_kb", kb_id="xxx", query_text="关键词")
    """
    base_url = os.environ.get("STARRING_BRIDGE_URL", "").rstrip("/")
    token = os.environ.get("STARRING_BRIDGE_TOKEN", "")
    if not base_url or not token:
        raise ToolCallError("missing_env", "缺少 STARRING_BRIDGE_URL 或 STARRING_BRIDGE_TOKEN 环境变量")

    payload = json.dumps({"tool": tool, "arguments": kwargs}).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/api/codeact/tool-call",
        data=payload,
        headers={"Content-Type": "application/json", "X-CodeAct-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:500]
        raise ToolCallError("http_error", f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise ToolCallError("connection_error", str(exc.reason))

    if not body.get("ok"):
        error = body.get("error") or {}
        raise ToolCallError(error.get("type", "unknown"), error.get("message", "未知错误"))
    return body.get("result")
