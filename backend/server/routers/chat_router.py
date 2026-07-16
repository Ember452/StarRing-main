"""
聊天（Chat）路由模块

提供以下核心 API 分组：
- 模型调用：/call — 简单问答
- 对话流：/thread/{thread_id}/resume — 恢复被人工审批中断的对话
- 对话历史与状态：/thread/{thread_id}/history、/state — 获取历史消息和 Agent 运行状态
- 线程管理：/thread、/threads — 创建、查询、更新、删除对话线程
- 附件管理：/attachments/tmp、/thread/{thread_id}/attachments — 临时附件上传、解析、确认关联
- 线程文件/交付物：/thread/{thread_id}/files、/artifacts — 浏览、读取、下载、保存交付物
- 消息反馈：/message/{message_id}/feedback — 点赞/踩
- 多模态图片：/image/upload — 图片上传、压缩、base64 编码
"""

import traceback
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from starring.storage.postgres.models_business import User
from server.utils.auth_middleware import get_db, get_required_user
from starring import config as conf
from starring.models import select_model
from starring.services.chat_service import (
    get_agent_state_view,  # 获取 Agent 当前状态（todos、files、artifacts 等）
    stream_agent_resume,  # 流式恢复被中断的 Agent 对话
)
from starring.repositories.conversation_repository import ConversationRepository
from starring.services.conversation_service import (
    confirm_tmp_thread_attachments_view,
    create_thread_view,
    delete_thread_attachment_view,
    delete_thread_view,
    get_thread_history_view,
    list_thread_attachments_view,
    list_threads_view,
    parse_tmp_attachment_view,
    update_thread_view,
    upload_thread_attachment_view,
    upload_tmp_attachment_view,
)
from starring.services.file_preview import detect_media_type
from starring.services.thread_files_service import (
    list_thread_files_view,
    read_thread_file_content_view,
    resolve_thread_artifact_view,
    save_thread_artifact_to_workspace_view,
)
from starring.services.feedback_service import get_message_feedback_view, submit_message_feedback_view
from starring.utils.logging_config import logger
from starring.utils.image_processor import process_uploaded_image
from starring.utils.paths import VIRTUAL_PATH_PREFIX


# TODO：当前文件的功能过于庞杂，路由标签混乱


# 图片上传响应模型
class ImageUploadResponse(BaseModel):
    success: bool
    image_content: str | None = None
    thumbnail_content: str | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    error: str | None = None


chat = APIRouter(prefix="/chat", tags=["chat"])


@chat.post("/call")
async def call(query: str = Body(...), meta: dict = Body(None), current_user: User = Depends(get_required_user)):
    """调用模型进行简单问答（需要登录）

    这是最基础的模型调用接口，不走 Agent 和 LangGraph 流程，
    直接通过 select_model() 选择模型实例并调用其 call() 方法。
    """
    meta = meta or {}

    # 确保 request_id 存在，用于追踪请求
    if "request_id" not in meta or not meta.get("request_id"):
        meta["request_id"] = str(uuid.uuid4())

    model = select_model(model_spec=meta.get("model_spec") or meta.get("model") or conf.default_model)

    response = await model.call(query)
    logger.debug({"query": query, "response": response.content})

    return {"response": response.content, "request_id": meta["request_id"]}

# TODO ：这一个router层这么多逻辑。后期尝试修复，拆分。
@chat.post("/thread/{thread_id}/resume")
async def resume_thread_chat(
    thread_id: str,
    approved: bool | None = Body(None),  # 用户决定会话是否继续
    answer: dict | None = Body(None),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """恢复被人工审批中断的对话（需要登录）

    当 Agent 运行到需要人工审批的节点（如 interrupt）时，会暂停等待用户决策。
    本接口接收用户的审批结果或答案，通过 Command(resume=...) 恢复 Agent 执行。
    底层调用 stream_agent_resume() 以 StreamingResponse 方式流式返回结果。
    """

    # 验证 thread 存在且属于当前用户
    conv_repo = ConversationRepository(db)
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if not conversation or conversation.uid != str(current_user.uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")
    agent_id = conversation.agent_id

    def normalize_resume_input(raw_answer: Any, raw_approved: bool | None) -> Any:
        """将前端传入的审批结果/答案标准化为 Agent 可识别的 resume input

        resume_input 的三种形态：
        - "approve" / "reject"：当仅传入 approved 布尔值时
        - {question_id: answer}：当传入批处理问答映射时
        - 单个值：当传入单个 answer 时（字符串、列表、dict 等）

        支持 answer 的多种类型：
        - str：普通文本答案
        - list[str]：多选/多项答案
        - dict with type="other"：自定义类型答案
        """
        def normalize_single_answer(value: Any) -> Any:
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    raise HTTPException(status_code=422, detail="answer 不能为空")
                return normalized

            if isinstance(value, list):
                if len(value) == 0:
                    raise HTTPException(status_code=422, detail="answer 不能为空")

                normalized_list: list[str] = []
                for item in value:
                    if not isinstance(item, str) or not item.strip():
                        raise HTTPException(status_code=422, detail="answer 列表必须是非空字符串")
                    normalized_list.append(item.strip())
                return normalized_list

            if isinstance(value, dict):
                if value.get("type") == "other":
                    text = value.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise HTTPException(status_code=422, detail="other 文本不能为空")
                return value

            raise HTTPException(status_code=422, detail="answer 值类型不支持")

        if raw_answer is not None:
            if isinstance(raw_answer, dict):
                if len(raw_answer) == 0:
                    raise HTTPException(status_code=422, detail="answer 不能为空")

                normalized_answers: dict[str, Any] = {}
                for question_id, value in raw_answer.items():
                    normalized_question_id = str(question_id).strip()
                    if not normalized_question_id:
                        raise HTTPException(status_code=422, detail="question_id 不能为空")
                    normalized_answers[normalized_question_id] = normalize_single_answer(value)
                return normalized_answers

            raise HTTPException(status_code=422, detail="answer 必须是对象映射 {question_id: answer}")

        if raw_approved is not None:
            return "approve" if raw_approved else "reject"

        raise HTTPException(status_code=422, detail="approved 或 answer 至少提供一个")

    resume_input = normalize_resume_input(answer, approved)

    logger.info(
        "Resuming agent_id: %s, thread_id: %s, approved: %s, answer_type: %s",
        agent_id,
        thread_id,
        approved,
        type(answer).__name__ if answer is not None else "None",
    )

    meta = {
        "agent_id": agent_id,
        "thread_id": thread_id,
        "uid": current_user.uid,
        "approved": approved,
        "answer": answer,
        "resume_input": resume_input,
    }
    if "request_id" not in meta or not meta.get("request_id"):
        meta["request_id"] = str(uuid.uuid4())
    # 以 StreamingResponse 流式返回，每行一个 JSON 对象（NDJSON 格式）
    return StreamingResponse(
        stream_agent_resume(
            thread_id=thread_id,
            resume_input=resume_input,
            meta=meta,
            current_user=current_user,
            db=db,
        ),
        media_type="application/json",
    )


@chat.get("/thread/{thread_id}/history")
async def get_thread_history(
    thread_id: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    """获取对话历史消息（需要登录）- 包含用户反馈状态"""
    try:
        return await get_thread_history_view(
            thread_id=thread_id,
            current_uid=str(current_user.uid),
            db=db,
        )

    except Exception as e:
        logger.error(f"获取对话历史消息出错: {e}, {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取对话历史消息出错: {str(e)}")


@chat.get("/thread/{thread_id}/state")
async def get_thread_state(
    thread_id: str,
    include_messages: bool = Query(False),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """获取对话当前状态（需要登录）"""
    try:
        return await get_agent_state_view(
            thread_id=thread_id,
            current_uid=str(current_user.uid),
            db=db,
            include_messages=include_messages,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取对话状态出错: {e}, {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取对话状态出错: {str(e)}")


# ==================== 线程管理 API ====================

# --- 线程管理请求/响应模型 ---

class ThreadCreate(BaseModel):
    """创建线程请求体"""
    title: str | None = None
    agent_id: str
    metadata: dict | None = None


class ThreadResponse(BaseModel):
    """线程响应体"""
    id: str
    uid: str
    agent_id: str
    title: str | None = None
    is_pinned: bool = False
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttachmentResponse(BaseModel):
    """附件响应体"""
    file_id: str
    file_name: str
    file_type: str | None = None
    file_size: int
    status: str
    uploaded_at: str
    path: str
    artifact_url: str | None = None
    original_path: str | None = None
    original_artifact_url: str | None = None
    minio_url: str | None = None
    request_id: str | None = None


class AttachmentLimits(BaseModel):
    """附件上传限制"""
    allowed_extensions: list[str]
    max_size_bytes: int


class AttachmentListResponse(BaseModel):
    """附件列表响应体"""
    attachments: list[AttachmentResponse]
    limits: AttachmentLimits


class TmpAttachmentResponse(BaseModel):
    """临时附件响应体（上传到 MinIO tmp 后返回，尚未关联线程）"""
    tmp_file_id: str
    file_name: str
    file_type: str | None = None
    file_size: int
    bucket_name: str
    object_name: str
    minio_url: str
    uploaded_at: str
    parse_supported: bool = False
    parse_methods: list[str] = Field(default_factory=list)


class TmpAttachmentParseRequest(BaseModel):
    """临时附件解析请求体"""
    object_name: str
    file_name: str
    parse_method: str | None = None
    bucket_name: str | None = None


class TmpAttachmentParseResponse(BaseModel):
    """临时附件解析响应体"""
    tmp_file_id: str
    file_name: str
    bucket_name: str
    object_name: str
    parsed_object_name: str
    parsed_minio_url: str
    parse_method: str
    status: str
    truncated: bool = False


class TmpAttachmentConfirmItem(BaseModel):
    """确认关联临时附件的单项"""
    file_name: str
    file_type: str | None = None
    bucket_name: str
    object_name: str
    parsed_object_name: str | None = None
    truncated: bool = False


class TmpAttachmentConfirmRequest(BaseModel):
    """确认关联临时附件请求体"""
    attachments: list[TmpAttachmentConfirmItem]


class TmpAttachmentConfirmResponse(BaseModel):
    """确认关联临时附件响应体"""
    attachments: list[AttachmentResponse]


class ThreadFileEntry(BaseModel):
    """线程文件目录条目"""
    path: str
    name: str
    is_dir: bool
    size: int
    modified_at: str | None = None
    artifact_url: str | None = None


class ThreadFileListResponse(BaseModel):
    """线程文件列表响应体"""
    path: str
    files: list[ThreadFileEntry]


class ThreadFileContentResponse(BaseModel):
    """线程文件内容响应体（按行分页）"""
    path: str
    content: list[str]
    offset: int
    limit: int
    total_lines: int
    artifact_url: str


class SaveThreadArtifactRequest(BaseModel):
    """保存交付物请求体"""
    path: str


class SaveThreadArtifactResponse(BaseModel):
    """保存交付物响应体"""
    name: str
    source_path: str
    saved_path: str
    saved_artifact_url: str


# =============================================================================
# > === 会话管理分组 ===
# =============================================================================


@chat.post("/thread", response_model=ThreadResponse)
async def create_thread(
    thread: ThreadCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """创建新对话线程 (使用新存储系统)

    每个线程绑定一个 agent_id，后续该线程的所有对话都使用该 Agent。
    """
    return await create_thread_view(
        agent_id=thread.agent_id,
        title=thread.title,
        metadata=thread.metadata,
        db=db,
        current_uid=str(current_user.uid),
    )


@chat.get("/threads", response_model=list[ThreadResponse])
async def list_threads(
    agent_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """获取用户的所有对话线程 (使用新存储系统)

    支持按 agent_id 过滤，分页查询。
    """
    return await list_threads_view(
        agent_id=agent_id, db=db, current_uid=str(current_user.uid), limit=limit, offset=offset
    )


@chat.delete("/thread/{thread_id}")
async def delete_thread(
    thread_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """删除对话线程 (使用新存储系统)

    软删除：将线程状态标记为 "deleted"，不会物理删除数据。
    """
    return await delete_thread_view(thread_id=thread_id, db=db, current_uid=str(current_user.uid))


class ThreadUpdate(BaseModel):
    """线程更新请求体"""
    title: str | None = None
    is_pinned: bool | None = None


@chat.put("/thread/{thread_id}", response_model=ThreadResponse)
async def update_thread(
    thread_id: str,
    thread_update: ThreadUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """更新对话线程信息 (使用新存储系统)

    支持更新标题和置顶状态。
    """
    return await update_thread_view(
        thread_id=thread_id,
        title=thread_update.title,
        is_pinned=thread_update.is_pinned,
        db=db,
        current_uid=str(current_user.uid),
    )


# ================================
# > === 附件管理分组 ===
# ================================


@chat.post("/attachments/tmp", response_model=TmpAttachmentResponse)
async def upload_tmp_attachment(file: UploadFile = File(...), current_user: User = Depends(get_required_user)):
    """上传附件到 MinIO tmp 桶，暂不关联线程。

    该接口用于在创建线程之前或对话中途上传附件。
    附件先存入临时桶，后续通过 confirm 接口正式关联到指定线程。
    """
    return await upload_tmp_attachment_view(file=file, current_uid=str(current_user.uid))


@chat.post("/attachments/tmp/parse", response_model=TmpAttachmentParseResponse)
async def parse_tmp_attachment(
    request: TmpAttachmentParseRequest,
    current_user: User = Depends(get_required_user),
):
    """解析 tmp 附件并返回解析后的 tmp URL。

    对于需要 OCR 或文档解析的附件（如 PDF、图片），调用此接口进行内容提取，
    解析结果存入新的临时对象，前端可通过 parsed_minio_url 获取。
    """
    return await parse_tmp_attachment_view(
        object_name=request.object_name,
        file_name=request.file_name,
        parse_method=request.parse_method,
        bucket_name=request.bucket_name,
        current_uid=str(current_user.uid),
    )


@chat.post("/thread/{thread_id}/attachments/confirm", response_model=TmpAttachmentConfirmResponse)
async def confirm_tmp_thread_attachments(
    thread_id: str,
    request: TmpAttachmentConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """将 tmp 附件正式加入线程附件列表。

    批量确认：将一批临时附件（及其解析结果）从 tmp 桶迁移关联到指定线程。
    """
    return await confirm_tmp_thread_attachments_view(
        thread_id=thread_id,
        attachments=[item.model_dump() for item in request.attachments],
        db=db,
        current_uid=str(current_user.uid),
    )


@chat.post("/thread/{thread_id}/attachments", response_model=AttachmentResponse)
async def upload_thread_attachment(
    thread_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """上传原始附件并直接关联到指定对话线程。

    与 tmp 上传不同，此接口将附件直接存入线程专属存储空间。
    """
    return await upload_thread_attachment_view(
        thread_id=thread_id,
        file=file,
        db=db,
        current_uid=str(current_user.uid),
    )


@chat.get("/thread/{thread_id}/attachments", response_model=AttachmentListResponse)
async def list_thread_attachments(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """列出当前对话线程的所有附件元信息，含上传限制配置。"""
    return await list_thread_attachments_view(
        thread_id=thread_id,
        db=db,
        current_uid=str(current_user.uid),
    )


@chat.delete("/thread/{thread_id}/attachments/{file_id}")
async def delete_thread_attachment(
    thread_id: str,
    file_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """移除指定附件（从线程附件列表中删除，同时清理存储）。"""
    return await delete_thread_attachment_view(
        thread_id=thread_id,
        file_id=file_id,
        db=db,
        current_uid=str(current_user.uid),
    )


@chat.get("/thread/{thread_id}/files", response_model=ThreadFileListResponse)
async def list_thread_files(
    thread_id: str,
    path: str = Query(f"{VIRTUAL_PATH_PREFIX}"),
    recursive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """列出线程文件目录。

    默认从虚拟路径根目录（VIRTUAL_PATH_PREFIX）开始浏览，
    支持递归展开子目录。
    """
    return await list_thread_files_view(
        thread_id=thread_id,
        current_uid=str(current_user.uid),
        db=db,
        path=path,
        recursive=recursive,
    )


@chat.get("/thread/{thread_id}/files/content", response_model=ThreadFileContentResponse)
async def read_thread_file_content(
    thread_id: str,
    path: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """读取线程文本文件（按行分页）。

    适用于大文件的增量读取，offset 为起始行号，limit 为最大返回行数。
    """
    return await read_thread_file_content_view(
        thread_id=thread_id,
        current_uid=str(current_user.uid),
        db=db,
        path=path,
        offset=offset,
        limit=limit,
    )


@chat.get("/thread/{thread_id}/artifacts/{path:path}")
async def get_thread_artifact(
    thread_id: str,
    path: str,
    download: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """下载或预览线程文件（交付物）。

    通过 detect_media_type() 根据文件签名自动检测 MIME 类型。
    当 download=True 时，设置 Content-Disposition 为 attachment 触发浏览器下载。
    """
    file_path = await resolve_thread_artifact_view(
        thread_id=thread_id,
        current_uid=str(current_user.uid),
        db=db,
        path=path,
    )

    media_type = detect_media_type(file_path.name, file_path.read_bytes())
    headers = {"Content-Disposition": f'attachment; filename="{file_path.name}"'} if download else None
    return FileResponse(path=file_path, media_type=media_type, headers=headers)


@chat.post("/thread/{thread_id}/artifacts/save", response_model=SaveThreadArtifactResponse)
async def save_thread_artifact_to_workspace(
    thread_id: str,
    request: SaveThreadArtifactRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """保存交付物到共享 workspace/saved_artifacts 目录。

    将 Agent 生成的交付物从线程输出目录复制到工作区，
    方便用户跨线程访问和管理。
    """
    return await save_thread_artifact_to_workspace_view(
        thread_id=thread_id,
        current_uid=str(current_user.uid),
        db=db,
        path=request.path,
    )


# =============================================================================
# > === 消息反馈分组 ===
# =============================================================================


class MessageFeedbackRequest(BaseModel):
    """消息反馈请求体"""
    rating: str  # 'like' or 'dislike'
    reason: str | None = None  # 踩（dislike）时的可选原因


class MessageFeedbackResponse(BaseModel):
    """消息反馈响应体"""
    id: int
    message_id: int
    rating: str
    reason: str | None
    created_at: str


@chat.post("/message/{message_id}/feedback", response_model=MessageFeedbackResponse)
async def submit_message_feedback(
    message_id: int,
    feedback_data: MessageFeedbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """提交消息反馈（需要登录）

    支持 like（点赞）和 dislike（踩），dislike 时可附带原因说明。
    """
    result = await submit_message_feedback_view(
        message_id=message_id,
        rating=feedback_data.rating,
        reason=feedback_data.reason,
        db=db,
        current_uid=str(current_user.uid),
    )
    return MessageFeedbackResponse(**result)


@chat.get("/message/{message_id}/feedback")
async def get_message_feedback(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """获取指定消息的用户反馈（需要登录）

    返回当前用户对指定消息的点赞/踩状态。
    """
    return await get_message_feedback_view(
        message_id=message_id,
        db=db,
        current_uid=str(current_user.uid),
    )


# =============================================================================
# > === 多模态图片支持分组 ===
# =============================================================================


@chat.post("/image/upload", response_model=ImageUploadResponse)
async def upload_image(file: UploadFile = File(...), current_user: User = Depends(get_required_user)):
    """
    上传并处理图片，返回 base64 编码的图片数据

    处理流程：
    1. 校验文件类型（仅允许 image/*）
    2. 检查文件大小（上限 10MB，超限拒绝）
    3. 调用 process_uploaded_image() 进行格式校验、EXIF 方向修正、
       缩略图生成、压缩（目标 5MB 以内）
    4. 返回处理后的 base64 图片数据及元信息

    底层使用 PIL/Pillow 进行图片处理，支持 JPEG、PNG、WebP、GIF、BMP 格式。
    """
    try:
        # 验证文件类型
        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="只支持图片文件上传")

        # 读取文件内容
        image_data = await file.read()

        # 检查文件大小（10MB限制，超过后会压缩到5MB）
        if len(image_data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="图片文件过大，请上传小于10MB的图片")

        # 处理图片：格式校验、方向修正、缩略图、压缩
        result = process_uploaded_image(image_data, file.filename)

        if not result["success"]:
            raise HTTPException(status_code=400, detail=f"图片处理失败: {result['error']}")

        logger.info(
            f"用户 {current_user.id} 成功上传图片: {file.filename}, "
            f"尺寸: {result['width']}x{result['height']}, "
            f"格式: {result['format']}, "
            f"大小: {result['size_bytes']} bytes"
        )

        return ImageUploadResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"图片上传处理失败: {str(e)}, {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"图片处理失败: {str(e)}")