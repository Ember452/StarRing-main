"""CodeAct 工具桥端点。

鉴权例外路径：本端点不走用户 JWT 鉴权链路，仅凭 Header ``X-CodeAct-Token``
（run 级短时凭证，由 CodeActMiddleware 在每次 execute_python 前生成并写入 Redis）
校验身份与白名单。这是全项目唯一 token 自带用户身份的端点，任何改动鉴权
中间件的后续工作都必须复查此路径。

设计文档：docs/vibe/2026-07-27-codeact-execution-paradigm.md
"""

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from starring.services.codeact_service import BridgeError, dispatch_tool_call

codeact = APIRouter(prefix="/codeact", tags=["codeact"])


class ToolCallRequest(BaseModel):
    """沙盒内 starring_tools.call 发起的桥调用请求体。"""

    tool: str = Field(description="平台工具名")
    arguments: dict = Field(default_factory=dict, description="工具入参")


@codeact.post("/tool-call")
async def tool_call(
    request: ToolCallRequest,
    x_codeact_token: str = Header(default="", alias="X-CodeAct-Token"),
):
    """校验 bridge token 与白名单后分发工具调用。

    错误统一返回 ``{"ok": false, "error": {"type": ..., "message": ...}}``，
    由沙盒内客户端抛出 ToolCallError，让 traceback 自然进入模型可见输出。
    """
    if not x_codeact_token:
        return {"ok": False, "error": {"type": "missing_token", "message": "缺少 X-CodeAct-Token 请求头"}}
    try:
        result = await dispatch_tool_call(x_codeact_token, request.tool, request.arguments)
    except BridgeError as exc:
        return {"ok": False, "error": {"type": exc.error_type, "message": exc.message}}
    return {"ok": True, "result": result}
