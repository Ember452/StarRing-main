# 版本变更记录

本页用于记录各版本发布说明（新增、修复与破坏性变更）。

同一版本的多次功能更新时，应以功能为单位进行更新，比如之前添加了 A 功能的更新，在后续的更新中修复了因 A 功能引入的 bug，那么这个修复说明应该和 A 功能描述放在一起，而不是新增一条修复记录，功能更新同理。

## v0.7.1 (current)

### 开发记录

- 新增知识库定时同步（kb_sync 触发器）：triggers 表新增 `trigger_type="kb_sync"`，config 结构为 `{cron_expr, timezone, kb_id}`，`agent_id` 改为可空并通过 `ensure_business_schema` 追加 `DROP NOT NULL` 运行时迁移；调度复用 P1-C 触发器系统——`list_active_cron_triggers` 过滤放宽为 `IN ('cron','kb_sync')`，`scan_triggers` 到点后按类型分流入队新 ARQ 任务 `execute_kb_sync`（幂等 job id 格式不变）；新增执行服务 `services/trigger/kb_sync.py`：worker 内强制 `_load_metadata()` 重载知识库元数据，筛选 `processing_params.original_source` 为 http(s) URL 的文档逐个重新抓取，SHA-256 内容 hash 未变则跳过，变化则重传 MinIO 并直接 await `parse_file` + `index_file` 重建索引，单文件失败不中断，结果仅落 Trigger 的 last_run_status/run_count 不建 AgentRun；`trigger_router` 新增 kb_sync 类型校验（cron 表达式/时区 + kb_id 存在性 + 管理员或 KB owner 权限）；前端 TriggerManagementView 支持 kb_sync 类型（知识库下拉、隐藏 agent/query 字段与执行历史入口）。配套单测 `test_kb_sync.py` 11 例。
- 新增 OpenAI 兼容 API 出口：新增 `POST /v1/chat/completions`（`openai_compat_router.py`，单独挂载 /v1 前缀不进 /api），`model` 参数映射 Agent slug（`get_visible_by_slug` 校验可见性），认证复用 `get_required_user`（支持 `Bearer yxkey_` API Key）；执行链路复用 `create_thread_view`（每请求一次性 thread，metadata.source=openai_compat）→ `create_run` → `await_agent_run_result`，成功返回标准 `chat.completion` 结构（`id=chatcmpl-{run_id}`，usage 置 0）；messages 取最后一条 user 消息，content 支持字符串与 parts 数组（拼接 text 段）；所有错误统一 OpenAI `{"error": {...}}` 格式，stream=true 返回 400（首版仅非流式，不含 /v1/models 与会话复用）。配套单测 `test_openai_compat_router.py` 10 例。
- 放开知识库 owner 管理权限：知识库管理类端点（更新/删除库、统计修复、文件夹与文档增删/解析/入库/移动、上传/URL 抓取/工作区导入、图谱构建配置/索引/重置、导图生成、示例问题生成、检索参数更新、导出）从 admin-only 放宽为「管理员或知识库 owner（created_by 本人）」，查看类端点（文档详情/内容/下载、检索与检索测试、检索参数查询、图谱构建状态、导图查询/diff、示例问题查询）放宽为「管理员或按 share_config 可访问的用户」，`/types`、`/files/supported-types`、`/generate-description` 放宽为任意登录用户；`knowledge_router` 新增 `_require_kb_manager` / `_require_kb_access` 两个鉴权 helper 统一校验。共享设置（share_config）仍仅管理员可修改：后端对非管理员传入 share_config 的更新请求直接 403，`get_database_info` 详情响应补充 `created_by` 字段供前端 owner 判断。前端 `knowledge_api.js` 同步将放宽端点从 `apiAdmin*` 改为 `api*` 去除客户端管理员拦截；知识库详情页 `canManageDatabase` 增加 owner 判断，编辑提交仅在管理员时携带 share_config，RAG 评估/评估基准 tab 对非管理员隐藏（evaluation 接口仍为 admin-only）。
- 新增 StarRing Python CLI 首版底座：新增独立 `packages/StarRing-cli` 包，提供 `remote add/use/list/ping`、`login --browser`、`login --api-key`、`whoami`、`status`、`logout`；配置统一写入 `~/.StarRing/config.toml`，remote URL 只保留实例入口并派生 `/api` 请求路径。后端新增 `/api/auth/cli/sessions` device flow 授权接口与 `cli_auth_sessions` 持久表，浏览器确认后为当前用户创建一次性返回的 API Key；新增公开 `/api/system/discovery` 声明服务端版本、API 前缀、CLI 能力和关键端点，CLI 登录前校验服务端版本至少为 `0.7.1`（`0.7.1.dev*` 按 release tuple 兼容）及对应能力；前端新增 `/auth/cli/authorize` 授权确认页。补充 CLI 本地单测与后端服务/路由单测。
- 安全与健壮性加固：token 兑换接口改为 `POST /api/auth/cli/sessions/token`，`device_code` 改走请求体，避免凭据出现在访问日志的 URL 路径中；兑换与批准会话时对会话行加 `with_for_update` 行锁，防止并发/重试导致重复签发 API Key；CLI 浏览器登录轮询区分瞬时错误（网络层错误、5xx）与终止错误，瞬时错误继续重试而非中断整个登录；`config.toml` 以 `0600` 原子创建并对名称等写入值做引号/反斜杠转义，避免明文凭据短暂可读及特殊字符破坏配置；用户软删除脱敏名改用用户主键生成，避免短哈希碰撞触发唯一索引冲突；前端授权页新增确认提示与对结构化错误 `detail` 的兼容渲染。
- 收敛 API Key 生成逻辑：移除独立 API Key 生成服务，统一通过 `AuthUtils.generate_api_key()` 生成 CLI 授权与用户管理中的 API Key。
- 收敛认证模块命名：CLI 浏览器授权路由合并到 `auth_router.py`，授权会话服务迁移到 `auth_service.py`。
- 为 CLI 知识库上传补齐后端接口边界：discovery 新增 `cli.kb_upload` 能力声明；普通文件上传接口在传入 `kb_id` 时先校验知识库存在且支持文档，校验通过后才读取文件或写 MinIO；新增同步 `POST /api/knowledge/databases/{kb_id}/documents/add`，用于把已上传的 MinIO 文件添加为知识库文档记录但不解析、不入库、不进入 Tasker；旧 `/documents` ingest 入口保留兼容，但在 enqueue 前补充空 items、非 MinIO URL 与缺失 content hash 的请求级校验。
- 新增 `StarRing kb upload` 上传命令：默认仅包含 `.md/.txt/.docx/.html/.htm`，省略 `--kb-id` 时会从 remote 拉取并只展示支持文档上传的知识库，支持非全屏的方向键单选知识库与多选文件类型；支持 `--include-ext/--exclude-ext` 与 `--concurrency` 控制本地并发队列；交互终端上传阶段显示进度条，非交互输出保留文本进度；每个并发单元在单文件上传成功后立即调用 `/documents/add` 添加该文件记录，不触发解析/OCR/入库；目录上传通过 `source_paths` 保留相对路径，后端创建文件记录时使用该路径作为展示文件名以保持前端目录层级。
- 发布 `StarRing-cli` 到 PyPI，并新增 GitHub Release 触发的 PyPI Trusted Publishing 工作流；文档新增命令行工具使用说明。
- 优化知识库文件列表状态流转与文件预览边界：`uploaded/parsed/error_parsing/error_indexing` 状态分别展示解析、入库或重试操作；源文件预览与解析后的 Markdown 查看分离，txt/图片/Markdown/HTML/PDF/代码类按源文件类型预览；Office 源文件仅支持 `.docx/.pptx`，点击预览时按需生成并缓存 PDF 预览内容，由同一个预览接口直接返回，不再把解析 Markdown 产物当作源文件预览。
- 优化思维导图构建接口设计，支持增量构建和更新：新增 GET /mindmap/diff 接口检测文件变更，POST /mindmap/generate 新增 incremental 参数支持增量更新；纯删除场景无需 AI 调用（递归树手术），新增文件时 AI 整合进现有分类结构；前端导图 Tab 新增"增量更新"按钮和变更数量 badge
- 优化文档结构与智能体运行说明：项目简介去除对 LangGraph 具体版本的强调；中间件文档按当前内置 Agent 链路重写，补充知识库工具、Skills 激活、附件/文件系统、子智能体 task、Summary 上下文压缩与工具结果卸载机制；知识库文档补充知识导图与示例问题生成机制；Langfuse 集成文档从“智能体开发”移动到“高级配置”分组。
- 移除知识库普通上传接口遗留的 `allow_jsonl` 参数，上传类型判断统一依赖 `SUPPORTED_FILE_EXTENSIONS`；评估数据集 JSONL 继续通过独立评估接口上传。
- 修复 Dependabot esbuild 告警：web 与 docs 统一锁定 `esbuild@0.28.1`，docs 同步升级 Vite/Vue 插件 override 并固定 pnpm 版本，避免旧锁文件继续解析到存在漏洞的 esbuild 版本。
- 修复 CORS 与依赖安全告警：后端 CORS 改为通过 `StarRing_CORS_ORIGINS` 配置允许来源，开发环境默认仅允许本机前端端口，生产环境未配置时不开放跨域，显式使用 `*` 时会关闭 credentials；同步刷新前后端锁文件，将 `aiohttp`、`cryptography`、`langchain`、`langchain-anthropic`、`pypdf`、`python-multipart`、`starlette`、`pyjwt`、`torch`、`torchvision`、`dompurify`、`js-yaml`、`markdown-it`、`vite` 升级到安全版本。
- 修复添加/编辑 MCP 弹窗中环境变量无法新增的问题：环境变量编辑器存在 rows -> object -> rows 的双向同步回环，`modelValue` 变化时会完全根据已有 key 重建行，导致只填了 key 的行（含刚点击「添加变量」生成的空行）被过滤掉而无法新增；现在仅当传入值与组件自身 emit 的内容不一致时才重建行，避免回声覆盖未填 key 的行。
- 修复模型与知识库后端导入循环：`StarRing.models` 改为惰性导出模型选择函数，知识库可见范围和知识库工具延迟读取全局 `knowledge_base` 实例，避免单测、热重载或轻量导入知识库包时因模块尚未完成初始化而失败。
- 修复知识库创建权限持久化一致性：创建知识库时由 Manager 归一化 `share_config/created_by` 后作为受控记录字段随首次知识库元数据插入写入数据库，避免先插入基础记录再二次更新权限字段产生短暂不一致。
- 修复 HTML 预览 iframe 高度问题：侧边预览模式改为 `height: 100%` 适应父容器，避免底部内容裁切；全屏预览模式移除 `min-height: calc(80vh - 40px)`，避免短内容下方白边；iframe 设为 `display: block` 消除行内基线间隙导致的底部白边；全屏渲染改用独立 `srcdoc`（不注入 `zoom`）按 100% 显示，侧边预览仍保持 0.75 缩放。
- 对话消息图片支持点击全屏预览：对话中用户上传的图片支持点击放大查看，复用文件预览的全屏蒙层交互（Teleport 蒙层，点击图片/空白处或按 Esc 关闭），不引入额外依赖。
- 新增 Agent token usage 状态快照，在状态面板中作为普通可折叠分组展示完整 `messages`、当前传给 LLM 的 `messages`、system/tools 构成、输入构成堆叠条和上下文窗口占用估算。
- 优化 Agent 上下文压缩：StarRing 的 DeepAgents summary adapter 在生成 summary 与写入 conversation history 时，不再改写 `AIMessage.tool_calls` 或 provider tool metadata，只逐条替换被摘要掉的旧 `ToolMessage.content`；`summary_keep_messages` 保留窗口原样传给模型，不再额外清洗最近消息；完整工具输出写入 `outputs/large_tool_results`，文件名使用工具名与内容 hash 生成，上下文只保留完整路径和最多 `summary_tool_result_token_limit` tokens 的预览，未触发 summary 的常规模型调用不做额外 ToolMessage 清洗；Summary 阈值判断沿用 DeepAgents/LangChain 默认近似 token counter，并保留其 usage metadata scaling；首次写入 `conversation_history` 前读取旧文件的 sandbox 404 会按 `file_not_found` 处理，不再产生误导性 warning；`present_artifacts` 会拒绝展示 `large_tool_results` 与 `conversation_history` 等工具调用阶段文件。新增管理员可配置项 `summary_keep_messages`、`summary_prompt`、`summary_tool_result_token_limit` 与 `max_execution_steps`，分别控制摘要后保留消息数、摘要提示词、summary 阶段工具结果预览上限和 LangGraph `recursion_limit`。
- 收敛普通聊天模型加载链路：`select_model` 保留旧 `.call()` 调用契约，内部改为通过 LangChain chat model adapter 复用 Agent 侧模型加载器，统一 OpenAI-compatible、Anthropic 与 Gemini 等 provider 的运行时适配；移除旧 `OpenAIBase` wrapper，默认重试策略迁移为 LangChain provider 参数。
- 优化 FastAPI 请求链路并发能力：Milvus 知识库检索中的同步 embedding、向量/BM25/混合检索调用，以及图谱查询中的同步 Milvus/Neo4j 读操作（含连接建立）统一通过有界 `asyncio.to_thread` 在线程中执行，避免阻塞 API 事件循环；并发上限按事件循环懒加载信号量控制，不改变检索默认行为与参数上限。
- 改进 OpenAI 兼容提供商流式工具调用兼容（替代 v0.7.0 的按 provider 禁流式处理）：根因是 LangGraph v3 流式累积对 tool_call 字段“后值覆盖”，SiliconFlow、阿里云百炼等在参数续片里把 `name`/`id` 下发为空字符串覆盖首片真实值。改为 `_ToolCallChunkFixChatOpenAI` 把续片空串 `name`/`id` 归一化为 `None`，对所有 OpenAI 兼容 provider 通用生效且保留流式，移除原 `_NON_STREAMING_TOOL_CALL_PROVIDERS` 名单。
- 新增 Agent 评估运行入口：`POST /api/agent/eval/runs` 会创建正常对话与 AgentRun，复用 worker 执行链路，并以 `agent_evaluation` 标记写入 conversation、AgentRun 与 Langfuse trace；接口阻塞至运行结束后直接返回最终结果（状态、最终 assistant 输出、Langfuse trace id）。`StarRing-cli` 新增 `StarRing agent eval` 命令，用于从 Langfuse 数据集读取输入并回传实验输出
- 下沉 AgentRun 基础能力：将「读取某个 run 的最终结果」（`get_agent_run_result`/`load_agent_run_result`，含状态、最终 assistant 输出、Langfuse trace id 与错误）与「阻塞至 run 终结再取结果」（`await_agent_run_result`，复用有限事件流、无额外轮询）提升进 `agent_run_service`，供 chat/eval 及未来定时任务统一复用；eval 运行入口改为非流式复用该能力（不再做 SSE 封装），移除其私有结果构建逻辑（结果不变）。
- 落地子智能体 Orchestrator-Worker 结构化交付物：新增 `SubAgentDeliverable` Pydantic 模型与 `subagent_deliverable.py` 模块，定义子智能体交付物的结构化 schema（type/format/path/content/metadata）；`subagent_task.py` 增加交付物解析与 prompt 注入函数，子智能体上下文 `SubAgentContext` 新增 `output_format` 字段以支持 `text`/`structured` 两种输出模式；`subagent/graph.py` 在 `output_format="structured"` 时追加结构化输出 prompt 后缀，引导子智能体在末尾产出可解析的 `<deliverable>...</deliverable>` 块。配套新增 `test_subagent_deliverable.py`（14 个单测覆盖 schema 校验、解析与边界场景）与 `test_subagent_task_prompt.py`（覆盖 prompt 注入字段与结构化后缀拼接）。
- 抽取 `agent_run_service.create_run` 作为触发器系统共用基础：将原 HTTP view 层 `create_agent_run_view` 重命名为 service 层 `create_run`，保留 `create_agent_run_view` 作为薄包装入口供 HTTP 调用方继续使用，业务逻辑全部下沉到 `create_run`；触发器（cron/webhook）等非 HTTP 调用方应直接调用 `create_run`，避免 view 层参数解析与 HTTP 上下文耦合。配合 `input_payload.source` 字段（`manual`/`cron`/`webhook`/`ask_user_question_resume`）区分触发来源，为后续 P1-C 触发器系统接入与 P1-A/P1-B 多智能体编排铺平共用基础。
- 新增 P1-C 触发器系统（cron 定时 + webhook 入口）：抽象触发器配置表 `triggers`（id/name/trigger_type/agent_id/uid/config/input_query/is_active/last_run_*），借鉴 MaxKB 简化为「触发器直接映射 AgentRun」模型，不再引入 TriggerTask 中间层。触发器归属创建者 `uid`，执行时以其身份调 `agent_run_service.create_run`，`input_payload.trigger_id/trigger_name` 关联到对应 `AgentRun`，执行历史通过 `AgentRun.input_payload->>'trigger_id'` 查询，不单独建表。采用「非阻塞设计」核心原则：触发器 enqueue run 后立即返回 `{"status":"queued","run_id":...}`，**不调用 `await_agent_run_result`**，避免占满 ARQ worker 并发槽阻塞普通 chat run；run 终结后由 `mark_run_terminal` 末尾钩子 `_update_trigger_status_if_any` 异步更新 `Trigger.last_run_status`，并通过 `mark_finished_if_current` 用 `WHERE last_run_id=?` 实现幂等保护，避免旧 run 终结覆盖新 run 状态。cron 实现采用「方案 C 元任务扫描」：ARQ `WorkerSettings.cron_jobs` 注册 `scan_triggers` 每分钟扫描 `triggers` 表，用 `croniter + pytz` 按用户配置时区判断是否到点，到点的触发器 `enqueue_job("execute_trigger_run", trigger_id, scheduled_time_iso, _job_id=f"trigger:{id}:{scheduled}")` 实现幂等入队；webhook 触发器通过 `POST /api/triggers/{id}/invoke` 入口，HMAC-SHA256 签名（`X-Trigger-Signature` + `X-Trigger-Timestamp` 5 分钟防重放）鉴权，无需 user token。后端新增 `trigger_router.py`（8 个端点：CRUD + rotate-secret + runs 历史 + invoke）、`trigger_repository.py`（CRUD + `mark_running/mark_finished/mark_finished_if_current`）、`trigger/{base,service,webhook,cron_scan}.py` 4 个服务模块；`pyproject.toml` 新增 `croniter>=2.0.0` 与 `pytz>=2024.1` 依赖。前端新增 `apis/trigger.js`（9 个 API）与 `TriggerManagementView.vue`（列表 + 创建/编辑弹窗 + 执行历史抽屉 + 轮换密钥 + 删除），路由注册 `/triggers`，侧边栏新增「触发器」菜单（AlarmClock 图标）。配套单测 4 个文件（`test_trigger_service.py` 11 用例 + `test_webhook_signature.py` 11 用例 + `test_cron_scan.py` 13 用例 + `test_run_worker_trigger_hook.py` 5 用例）与端到端集成测试 1 个文件（11 用例覆盖 cron/webhook 创建、签名校验、执行历史、权限隔离、删除）。
- 新增 P1-A Supervisor 软编排智能体：新增内置 `SupervisorAgent` backend，作为多角色协作编排器（如「写作+审稿+翻译」显式角色协作场景）。与 ChatbotAgent（Orchestrator-Worker，本地工具+task 工具，LLM 自主决定是否委派）形成角色边界：SupervisorAgent **不挂载本地工具**（`tools=[]`，KB / Skills / 文件系统工具全部禁用），**仅挂载 task 工具**强制委派，**无可用子 agent 时 graph 构建抛 ValueError**（fail-fast 设计，遵循 AGENTS.md「良好的软件应该在预设的条件下运行」原则，替代原设计的 `_EmptySubagentMiddleware` 兜底）。H1 兼容性 spike 调研 `langgraph-supervisor` 库（v0.0.31），结论**不引入**：官方 README 明确推荐「supervisor pattern directly via tools rather than this library」，库本质是 tool-calling 封装与 P0-1 `task` 工具机制同构，未暴露 middleware 参数，子 agent checkpointer 集成未文档化。改为**复用 P0-1 `StarRingSubAgentMiddleware`**，通过 prompt + middleware 配置差异实现 supervisor 语义。后端新增 `buildin/supervisor/{__init__,context,backend,prompt}.py` 4 个文件：`SupervisorContext` 沿用 `ChatBotContext` 字段不新增（保持 context schema 兼容，前端复用 ChatbotAgent 配置 UI），`SUPERVISOR_SYSTEM_PROMPT` 明确「必须委派/不可直答/不可调用本地工具/合成是推理」语义（不包含 `### Available subagent types` 段落，该段落由 `StarRingSubAgentMiddleware` 通过 `wrap_model_call` 注入 `TASK_SYSTEM_PROMPT` 提供，避免重复），`SupervisorAgent.get_graph` 复用 `prepare_agent_runtime_context` + `create_agent`，middleware 栈移除 `KnowledgeBaseMiddleware`/`SkillsMiddleware`，保留 summary/TodoList/PatchToolCalls/ModelRetry/TokenUsage，强制挂载 `StarRingSubAgentMiddleware`（返回 None 时抛 ValueError）。配套单测 `test_supervisor_backend.py`（10 用例覆盖 context 继承、类属性、auto_discover、prompt 关键约束、system_prompt 合并、空子 agent fail-fast、不挂载 KB/Skills、强制挂载 task middleware、get_graph 正常返回与 tools=[] 验证）。
- 新增 P1-B 工作流引擎硬编排 backend（Phase 1 后端引擎）：新增内置 `WorkflowBackend`，基于 LangGraph `StateGraph` 实现确定性流程编排，适用于合规审查、标准化报告、流水线数据处理等流程化任务。与 ChatbotAgent（Orchestrator-Worker）/ SupervisorAgent（软编排强制委派）形成三种 backend 范式（被 `auto_discover_agents` 自动发现）。**范围裁剪**：节点从 6 个精简到 4 个（`start-end` / `llm` / `condition` / `application-call`，原 `kb-search` / `tool` 节点能力已被 `llm-node` 通过挂载工具覆盖）；前端可视化编辑器推迟到 Phase 2（Phase 1 仅 JSON 定义）；condition 表达式不引入 JSONPath/JMESPath 新 DSL，改为 Python 受限 AST 求值。**节点间数据契约**复用 P0-1 `SubAgentDeliverable`（H4 决策），每个节点执行后写入 `state.node_outputs[node_id]`，下游节点从 `node_outputs[upstream_id]` 读取 summary/key_findings/confidence/artifacts，保证 P0-1 / P1-B 数据契约一致。后端新增 `buildin/workflow/` 11 个文件：`definition.py`（`WorkflowDefinition` / `Node` / `Edge` Pydantic 模型 + fail-fast 校验：必填 start/end 节点、边指向存在性、节点数 ≤50、DFS 环路检测、各节点 config 必填字段校验）、`state.py`（`WorkflowState` 扩展 `BaseState` 含 `node_outputs` 字段）、`context.py`（`WorkflowContext` 含 `workflow_id` / `workflow_version`）、`backend.py`（`WorkflowBackend.get_graph` 从 DB 加载定义 + 编译为 StateGraph + 注册节点 + 注册边）、`nodes/safe_eval.py`（受限 AST 求值器：白名单节点 + 禁止 `Call`/`Lambda`/`import`/赋值/属性写 + 长度限制 500 字符 + 危险 dunder 属性黑名单）、`nodes/{start_end,llm,condition,application_call}.py` 4 个节点执行器（condition 节点用 LangGraph `Command(goto=...)` 实现条件跳转，避免 `__branch__` 状态污染）。仓储层新增 `WorkflowRepository`（CRUD + `get_by_slug` + `get_for_user` 权限隔离）与 `workflows` 表（id/name/slug/owner_uid/definition/version/is_active，运行实例复用 `agent_runs` 表 run_type=`"workflow"`，不单独建 `workflow_runs` 表）。新增 `workflow_router.py`（7 个端点：CRUD + `validate` 工作流定义合法性校验不执行），注册到 `/api/workflows/*`。配套单测 5 个文件（`test_safe_eval.py` 25 用例覆盖合法表达式/非法语法/危险访问/边界场景、`test_workflow_definition.py` 19 用例覆盖 fail-fast 校验、`test_workflow_backend.py` 11 用例覆盖 graph 编译与 DB 加载、`test_workflow_nodes.py` 11 用例覆盖 4 节点执行器、`test_workflow_repository.py` 10 用例覆盖 CRUD 与权限隔离），共 76 测试用例。
- P1-B 工作流引擎补全修复：完成 P1-B 与现有代码的运行时集成闭环与逻辑加固。**chat_service 集成 gap 修复**：原 `chat_service._resolve_agent_runtime` 未注入 `workflow_id`，导致 WorkflowBackend 在主对话路径无法运行；新增逻辑在 `agent_config` 字典中检测 `context_schema` 含 `workflow_id` 字段且未显式配置时，用 `agent_item.slug` 作为 `workflow_id` 注入（约定 `workflows.slug == agents.slug`），与 `agent_runtime_service.resolve_agent_runtime_context` 中已有的同模式注入对齐，覆盖 chat 与 viewer_filesystem 两条调用路径。**`_load_definition` 双路径查找**：原仅支持 `repo.get(workflow_id)`（UUID），改为先 `repo.get_by_slug(workflow_id)`（slug 路径，最常见），未命中再 fallback `repo.get(workflow_id)`（UUID 路径，用户显式配置场景）。**`_build_state_graph` fail-fast 加固**：原普通节点多出边静默取第一条，违反 AGENTS.md「不掩盖设计缺陷」原则；改为多出边时抛 `ValueError` 提示「普通节点只允许 1 条出边；如需多路分支请使用 condition 节点」。**`WorkflowDefinition` 校验增强**：新增边数上限 100 防止定义过大；新增 condition 节点 `cases[i].then` 与 `default` 目标存在性校验（指向不存在的节点时 fail-fast）。**`application_call.py` 精确匹配**：原 `target_slug in agent_id` 模糊子串匹配会误匹配（如 slug="research" 误匹配 "deep-research"），改为按类名精确匹配 `target_slug in agent_manager._classes`。**`start_end.py` 清理**：移除无用 `upstream_node = context` 变量与 `try/except Exception: continue` 静默吞异常。**`workflow_router.py` 接口规范**：`list_workflows` 返回结构统一为 `{"workflows": [...]}` 与其他端点风格一致；新增 `POST /api/workflows/validate` 端点支持前端编辑器实时校验（不执行、不需要先保存）。**配套测试补全**：`test_workflow_definition.py` 新增 3 用例（边数超限、condition cases.then/default 指向不存在节点）、`test_workflow_backend.py` 新增 3 用例并修订 3 个原有用例适配 slug 优先路径（slug 加载成功、slug 未命中 fallback UUID、普通节点多出边 fail-fast）、新增 `test_workflow_chat_integration.py` 8 用例覆盖 chat_service 注入闭环（workflow_id 注入、显式配置保留、非 WorkflowBackend 跳过、agent_runtime_service 注入、slug 路径端到端加载）。所有修改文件通过 `ast.parse` 语法验证，Docker 容器未启动故未执行运行时 pytest。
- 新增 P1-B 工作流可视化编辑器（Phase 2 前端）：基于 Vue Flow（`@vue-flow/core@1.48` + background/controls/minimap）实现 Dify 风格拖拽式工作流编排界面，**后端零改动**（利用 Pydantic 默认忽略额外字段 + router 持久化原始 dict 的机制，在节点 JSON 中附加 `position`、顶层附加 `viewport` 实现画布布局持久化）。前端新增 6 个文件：`apis/workflow_api.js`（list/create/detail/update/remove/validateDefinition 6 方法）、`views/WorkflowListView.vue`（卡片列表 + 新建弹窗，创建后直接跳转编辑器 + 启停/删除）、`views/WorkflowEditorView.vue`（顶栏返回/重命名/校验徽标/JSON 抽屉/保存 + 左侧节点面板拖拽或点击添加 + 点阵画布 Background/Controls/MiniMap + 右侧 360px 配置面板）、`components/workflow/{nodeTypes.js,serialize.js,WorkflowNodeCard.vue}`（节点元数据、Vue Flow ↔ 后端 definition 双向序列化、紧凑节点卡片）。**与后端校验规则前置对齐**：start/end 节点面板限一个、禁自环连线、普通节点仅允许 1 条出边、condition 每个分支 handle 仅 1 条出边、节点 ≤50 / 边 ≤100；空工作流自动放置 start+end 节点。**condition 分支同步机制**：case 行内嵌 source handle（`case-${i}`/`default`），序列化时根据连线同步写 `config.cases[i].then`/`config.default` 与 `edge.branch`（=目标节点 id），删除 case 时同步清理并重排分支连线。四种节点配置表单（llm: system_prompt/model/input_template；condition: cases 动态列表 monospace 表达式；application-call: 智能体下拉（复用 `agentApi.getAgents`）；start/end: kind 只读）；JSON 抽屉支持双向同步，应用前经 `POST /api/workflows/validate` 校验；保存前同样先过校验接口再 PUT。路由新增 `/workflows`（列表）与 `/workflows/:workflowId`（编辑器），侧边栏「触发器」下新增「工作流」入口（Workflow 图标）。样式遵循 design.md 规范（CSS 变量 token、8px 圆角、无阴影，色系：start/end=灰、llm=主色、condition=警告色、application-call=信息色）。验证：`pnpm lint` 与 `pnpm build` 均通过；Docker 未启动，端到端验证待后续补做。
- 代码质量审查修复（异常吞咽/重复代码/依赖导入/回退控制）：对后端核心模块进行代码质量审查并修复 5 处问题，所有修改默认行为完全保持一致，通过 `ast.parse` 语法验证。**`subagent_task._parse_deliverable`** 的 `except Exception` 兜底分支补充 `logger.warning` 日志，保留「永远不抛异常」设计原则的同时留下 LLM 输出格式问题的排查线索。**`chat_service.py`** 抽取 `_safe_extract_agent_state(agent, langgraph_config, context=None)` 辅助函数，消除同步 chat / 流式 chat / 流式 resume 三处完全相同的 `try: get_graph + aget_state except Exception: agent_state = {}` 重复代码块，失败路径补充 warning 日志；同步/流式 chat 仍调用 `get_graph()`，resume 仍调用 `get_graph(context=context)`，行为完全一致。**`workflow_router.py`** 抽取 `_build_validation_response(definition_dict)` 辅助函数，让 `POST /workflows/validate` 与 `POST /workflows/{id}/validate` 两个端点共享校验与响应构造逻辑，消除 copy-paste 代码。**`cron_scan.py`** 把 `croniter`/`pytz` 导入从 `_is_due` 函数内部移到模块顶层（一次性 `try/except ImportError` + `_CRON_DEPS_AVAILABLE` 标志），避免每分钟扫描时对每个触发器都反复尝试导入已安装模块；依赖未安装时 `_is_due` 直接返回 False（保持原行为），日志从每次调用改为模块加载时记录一次。**`agents/base.py`** SQLite checkpointer 构建失败回退 `InMemorySaver` 的日志级别从 `error` 提升为 `critical`（更符合「生产环境可能导致状态丢失」的严重程度），并新增 `ALLOW_INMEMORY_CHECKPOINTER_FALLBACK` 环境变量（默认 `true` 保持现有降级行为，设为 `false` 时强制 fail-fast 抛 `RuntimeError` 拒绝降级，供对状态持久性有强要求的生产环境使用）。
- 新增知识库独立一级导航与普通用户创建权限：知识库从原「智能体扩展」页的 tab 子项升级为侧边栏一级导航「知识库」，所有登录用户可见，普通用户基于 `share_config` 权限模型在同一列表中查看个人创建（private）与组织内共享（global/department/user）的知识库。**前端路由迁移**：新增 `/knowledge`（列表 `KnowledgeList`）与 `/knowledge/:kbId`（详情 `KnowledgeDetail`）两条路由（移除 `requiresAdmin: true`，仅保留 `requiresAuth: true`），并从 `/extensions` 移除 `ExtensionKnowledgeBaseDetail` 子路由；`AppLayout` 侧边栏 mainList 在「工作区」与「智能体扩展」之间插入「知识库」导航项（`Database` 图标，activePaths=`['/knowledge']`）；`ExtensionsView` 移除 `<DataBaseView embedded />`、`knowledgeRef` 与 `adminExtensionTabs` 中的 knowledge 项（仅保留 tools/mcp/skills 三个 tab）、`isDetailPage` 移除 `/extensions/knowledgebase/` 检测、`activeChildLoading` refMap 移除 knowledge 项；`DataBaseView` 内部跳转路径从 `/extensions?tab=knowledge` 与 `/extensions/knowledgebase/${kb_id}` 统一改为 `/knowledge` 与 `/knowledge/${kb_id}`，watch 路由检测同步调整；`DataBaseInfoView` 返回按钮 `backToDatabase` 跳转 `/knowledge`，新增 `canManageDatabase` computed 控制顶部「编辑」按钮 `v-if`；`stores/database.js` 的 `deleteDatabase` 跳转路径与 `AgentRuntimeConfigForm.vue` 中 `case 'knowledges'` 跳转路径同步改为 `/knowledge`。**后端权限放宽**：`knowledge_router.create_database` 依赖从 `get_admin_user` 改为 `get_required_user`，普通用户创建时强制 `share_config={"access_level":"private","department_ids":[],"user_uids":[]}`（管理员仍尊重传入值），与既有 `get_databases_by_user` 的 share_config 过滤模型对齐；`get_accessible_databases` 返回字段补齐 `created_at` / `share_config` / `row_count` / `is_owner`（与 `get_database_info()` 返回的 `row_count` 字段对齐，前端 `DataBaseView.vue` 实际消费的就是 `row_count`）。**mock 模式补全**：`mock-server.js` 新增 3 个示例知识库（产品使用手册 private / 公司制度库 global / 技术文档库 department）与 5 个 mock 接口（`GET /api/knowledge/databases`、`/databases/accessible`、`/types`、`/databases/:kbId`、`/stats`），让 `MOCK_MODE=true pnpm dev` 启动时可完整验证列表/详情/类型加载全链路。
- 新增技术选型文档：在「项目亮点」分组下新增技术选型文档，详细说明 Web 框架选型（FastAPI vs Flask vs Django）、智能体框架选型（LangGraph v1 vs LangChain）、数据库技术选型（PostgreSQL / Neo4j / Milvus）与三层存储分离架构，并基于项目实际代码展示选型理由与应用示例。
- 修复知识库独立导航引入的权限与字段对齐 bug：代码审查发现普通用户进入 `/knowledge/:kbId` 详情页会被前后端双重拒绝、`get_accessible_databases` 返回字段名与前端消费字段不一致、详情页管理类按钮对普通用户未隐藏三个问题。**Bug A 修复（普通用户无法访问详情页）**：后端 `GET /api/knowledge/databases/{kb_id}` 端点依赖从 `get_admin_user` 放宽到 `get_required_user`，并新增 `knowledge_base.check_accessible(user_info, kb_id)` 权限校验（基于 share_config 模型：superadmin 全部可见、created_by 本人、global/department/user 按共享规则），无权限返回 403；前端 `knowledge_api.getDatabaseInfo` 从 `apiAdminGet` 改为 `apiGet` 避免 `checkAdminPermission` 在前端直接抛错，`createDatabase` 同步从 `apiAdminPost` 改为 `apiPost`。**Bug B 修复（字段对齐）**：`get_accessible_databases` 删除永远为 `None` 的 `updated_at` 字段（`databases_meta` 没有该字段），把 `file_count` 改为 `row_count`（与 `base.py:1196-1197` 的 `row_count` 字段对齐），前端 `DataBaseView.vue` 与 `DataBaseInfoView.vue` 实际消费的就是 `row_count`。**Bug C 修复（详情页管理按钮权限）**：`canManageDatabase` computed 收紧为仅管理员可见（移除 owner 分支，因后端 update/delete/upload/parse 等接口仍为 admin-only，普通用户即使为 owner 点击管理按钮也会 403）；`DataBaseInfoView` 文件管理 tab 的「上传」「新建文件夹」「待解析」「待入库」「Chunks 修复」「Tokens 修复」按钮与查询配置 tab 的「保存」按钮全部加 `v-if="canManageDatabase"`，普通用户进详情页为只读浏览模式，避免点击后报错。**已知限制**：普通用户 owner 管理自己知识库（编辑/上传/删除）需要后续放宽后端 `update_database_info`/`delete_database`/文件上传等十几个端点权限到 owner，作为下一阶段任务。
- 新增 P0 长期记忆/跨会话记忆功能：独立轻量记忆模块（不复用 KB 基建），PostgreSQL `user_memories` 表存明细（真源，`UserMemory` 模型 + `memory_repository.py`），Milvus 单一 `starring_memory` 集合存向量（id/uid/embedding 三字段，按 uid 过滤召回，IVF_FLAT + COSINE，embedding 用全局默认 embedding 模型，模型变更导致维度不匹配时 fail-fast 抛错提示重建集合，不做静默降级）。**写入双通道**：① run 终结钩子自动抽取——`mark_run_terminal` 后对 status=completed 且非触发器/非评估的 run，在所属 Agent 开启 `use_memory` 时 enqueue ARQ 任务 `extract_run_memories`（`_job_id=f"memory:{run_id}"` 幂等），任务内取对话最近 20 条 user/assistant 消息经 LLM（`config.default_model`）按抽取 prompt 输出 JSON 数组（明确排除密码/密钥/token 等敏感信息与一次性任务细节），逐条写入 `source="auto"`；② Agent `remember` 工具——用户明确说「记住 XX」时模型即时调用写入 `source="manual"`。写入统一走 `memory/service.add_memory`：向量查重（同 uid top-3 相似度 ≥0.92 视为重复跳过）+ 单用户 200 条上限 + 先 PG 后 Milvus 双写（Milvus 失败回滚 PG）。**注入**：新增 `MemoryMiddleware`，首次模型调用按最新用户消息向量召回 top-5 记忆，以「## 用户长期记忆」段落追加到 system prompt（每 run 只检索一次结果缓存，无记忆不注入，检索失败不阻断对话）；`ChatBotContext` 新增 `use_memory` 开关（默认关闭，经 Context metadata 机制自动出现在 Agent 配置 UI），LITE 模式整体不启用（middleware 不挂载、路由不注册）。**隐私边界**：记忆仅本人可见（管理员也不能看他人），新增 `memory_router`（`GET /api/memory` 列表、`DELETE /api/memory/{id}` 单删、`DELETE /api/memory` 清空），删除同步清理 PG + Milvus；前端新增 `apis/memory_api.js` 与 SettingsModal「记忆管理」tab（`MemoryManagementComponent.vue`：列表 + 来源标签 auto/manual + 单删 + 清空确认）。配套单测 2 个文件 17 用例（`test_memory_service.py` 覆盖 LLM 抽取输出解析：合法 JSON/代码块包裹/非法输出/空数组/非字符串项过滤/超长截断；`test_memory_middleware.py` 覆盖记忆注入、每 run 单次检索、无记忆不注入、无用户消息跳过检索、多模态消息取文本、检索异常不阻断、remember 工具 uid/source 传递与重复提示），全部通过；后端改动文件 py_compile 通过，`pnpm lint` + `pnpm build` 通过。Docker 未启动，涉及 PG/Milvus/ARQ 的运行时链路待后续联调验证。

## v0.7.0 (2026-06-13)

### 破坏性变更

- Provider 与模型配置收敛：移除旧版 v1 模型配置与 Ollama 支持，运行时模型统一使用 `provider_id:model_id` 与独立 provider 模块；自定义 provider 实现逻辑从文件移动到数据库，并从 config 文件迁移到 provider 模块。
- 智能体运行时语义收敛：用户可见的 `AgentConfig` 收敛为数据库持久化的一级 `Agent`，内置 Python Agent 改为智能体后端；聊天、运行任务、恢复审批和文件预览均从线程绑定的 Agent 解析运行时上下文，前端只提交 `agent_id`。
- 知识库能力边界收敛：移除 Upload 与 LightRAG 知识库/图谱能力，知识库类型收敛为 Milvus 与只读连接器；知识库 API 统一使用 `/databases/{kb_id}/xxx` 形式，并整合 mindmap / eval 等子接口。
- Agent 资源默认选择与权限过滤：未显式配置工具、知识库、MCP、Skills、子智能体时默认启用当前用户可访问/可用的全部资源，显式选择后按允许列表过滤；Agent 创建前统一完成最终资源权限过滤、知识库 `kb_id` 可见范围派生和 Skill prompt/readable 依赖闭包派生。
- Skill 安装与权限模型收敛：Skill 元数据使用 `source_type/share_config/enabled` 表达来源、生效范围与启用状态；内置 Skill 启动或同步时自动写入数据库并默认全局启用，上传和远程添加统一改为解析草稿后确认安装，不保留旧直接安装兼容路径。
- 历史兼容层精简：移除 sandbox provisioner `local` 后端别名、ask_user_question 单问题旧协议、JWT 历史默认密钥特殊判断、内置 Skill `SKILLS.md` 文件名回退、运行事件数字 seq 兼容和前端旧字段回退。
- 用户身份命名收敛：原业务登录标识统一改为 `uid`，Agent/LangGraph runtime、conversation、agent_run、sandbox 路径和前端用户态均使用字符串 `uid`；`user_id` 仅保留给外部响应中的数值 `users.id` 或真实外键场景。

### 开发记录

- 发布版本号更新至 `0.7.0`，同步 package、Docker 镜像标签与快速开始分支引用。
- 新增内置「深度研究」多智能体：编排器 Agent（`deep-research`，ChatbotAgent 后端）负责澄清、拆解、并行调度子智能体与综合成稿，配套两个子智能体 `research-explorer`（围绕单个子问题多轮检索网页/知识库并返回带引用发现）和 `fact-verifier`（对抗式核验关键论断、标注冲突与置信度）；完整研究方法论沉淀为新增内置 Skill `deep-research`（依赖 `tavily_search`），编排器运行时读取并据此调度。三者随 `lifespan` 启动通过 `AgentRepository.ensure_deep_research_agents` 幂等落库（已存在不覆盖管理员修改）。
- 新增内置 `general-purpose` 通用任务子智能体：使用 `SubAgentBackend` 与空运行配置，作为 `task` 工具的通用委派目标，由启动初始化自动写入数据库。
- 收敛 MCP 创建与编辑入口：前端移除整段配置文本入口和模式切换器，仅保留表单字段提交；后端 MCP 创建/更新请求拒绝额外配置字段，避免绕过表单约束。
- 调整内置 MCP 默认项：移除 `sequentialthinking` 的系统内置同步，启动同步时清理历史系统内置记录，保留用户手动创建的同名 MCP。
- 图片生成能力迁移为 Skill：Qwen-Image 从内置 Python 生成工具迁移到内置 Skill `image-gen`，模型调用与图片下载在 Agent 沙盒中完成，生成结果保存到 outputs 并通过 `present_artifacts` 展示，为多图片生成模型接入复用同一产物展示链路。
- 优化前端头像加载兜底：用户与智能体头像优先展示已配置图片，加载失败后回退到基于 ID 的 DiceBear 默认头像；离线或默认头像不可达时显示名称前两个字和稳定背景色。
- 降低知识库路由与工具模块复杂度：示例问题生成迁移到知识库 utils，文件上传统一 100 MB 限制，URL 预处理入库路径与旧 `content_type=url` 行为收敛，并修复 uid、导出 MIME 与异常透传等路由问题。
- 重构智能体配置语义：用户可见的 `AgentConfig` 收敛为数据库持久化的一级 `Agent`，内置 Python Agent 改为智能体后端；新增 `/api/agent` 管理与运行接口，聊天、运行任务、恢复审批和文件预览均从线程绑定的 Agent 解析运行时上下文，前端只提交 `agent_id`，并在模型配置页新增“智能体”管理页签。
- 删除 Upload 与 LightRAG 图谱/知识库能力：知识库类型收敛为 Milvus 与 Dify，只保留 Milvus 知识库内图谱构建/展示/检索，移除独立 `/graph` 页面和默认上传图谱工具。
- 收敛只读知识源连接器：新增 `ReadOnlyConnectors` 基类，Dify 改为声明自身创建参数与校验规则，新增 Notion Data Source 只读知识库并支持 Search/Find/Open；知识库类型接口返回创建参数 schema，前端新建表单按类型动态渲染非 Milvus 配置并统一保存到 `additional_params`。
- 新增知识库 Chunk 持久化：Milvus 知识库索引/更新流程会将 chunks 双写到 PostgreSQL `knowledge_chunks` 表与 Milvus，文件内容查看优先查询 PostgreSQL，并为位置信息、图谱实体关联、标签和抽取结果预留结构化字段；chunk 入库改为分批 embedding 与分批写入，避免大文件一次性写入触发 gRPC 消息大小限制；入库成功后将单文件 chunk 数与 token 数写入文件元数据，并将知识库级总 chunk 与总 token 汇总保存到 metadata，前端文件管理页展示该统计并支持一键修复历史文件缺失的统计值。
- 完善 Milvus 知识库图谱构建：修复 Chunk 图谱写入返回值、Neo4j 同步写入阻塞事件循环、重复构建任务竞态、图谱查询提前终止、Neo4j 连接复用、LLM 抽取超时重试和前端错误详情展示等问题；图谱构建会将 entity/triple 本体与 chunk 引用写入 PostgreSQL，并为唯一 entity/triple 建立 Milvus 语义索引，单文件删除时同步清理图谱引用和孤儿向量。
- 优化图谱抽取器配置：未配置时在图谱中心展示配置入口，抽取方案收敛为 LLM，前端仅保留“更多拓展中”占位；LLM 抽取器使用固定 Prompt + 自定义 Schema，并支持模型参数与并发队列数；已配置后允许修改参数并提示重置重抽风险。修复上传并入库新文件时旧内存 metadata 覆盖数据库图谱配置的问题。
- 新增 Milvus 图谱检索链路：Query 可召回图谱实体和三元组，结合 Chunk 命中实体构造 seed entity，读取 Neo4j 2-hop 子图后用 igraph 执行 PPR，最终以 Chunk 为产物并通过 RRF 与原 Chunk 召回融合；检索配置改为 dataclass 元数据生成，支持 `depend_on` 控制重排序和图检索参数展示。
- 收紧用户管理部门隔离：普通管理员创建用户时固定归属本部门，用户列表、访问选项、详情、更新和删除接口均限制在本部门范围内。
- 修复用户管理列表超过 100 人时被默认分页截断的问题：前端按 `skip/limit` 分批加载用户，并在用户卡片列表中补充分页渲染。
- 调整 Agent 资源默认选择与运行时上下文：未显式配置工具、知识库、MCP、Skills、子智能体时默认启用当前用户可访问/可用的全部资源，显式选择后按允许列表过滤；Agent 创建前统一完成最终资源权限过滤、知识库 `kb_id` 可见范围派生和 Skill prompt/readable 依赖闭包派生，聊天运行时与文件系统预览复用同一结果。
- 重构 Skills 权限与安装流程：Skill 增加 `source_type/share_config/enabled`，内置 Skill 作为启动同步入库的全局资源，不再保留前端安装/更新状态，支持启停但不允许删除；上传和远程添加统一为解析草稿后确认生效范围，安装 slug 优先读取 `SKILL.md` 的 `slug` 字段并保留 `name` 展示名，压缩包名称不参与 slug 校验；管理端支持编辑生效范围与启停；Agent 运行时按当前用户可访问 Skills 派生 prompt/readable 依赖闭包并限制挂载/激活，Skills prompt 改为模型请求级注入以避免污染 runtime context；主智能体恢复 `install_skill` 工具，允许当前用户安装私有 Skill 并激活当前会话，子智能体配置和运行态均禁用该工具。
- 精简历史兼容层：移除 sandbox provisioner `local` 后端别名、ask_user_question 单问题旧协议、JWT 历史默认密钥特殊判断、内置 Skill `SKILLS.md` 文件名回退、运行事件数字 seq 兼容和前端若干旧字段回退。
- 重构知识库共享权限：`share_config` 改为全局共享、部门共享、指定人可访问三档，部门共享必须包含当前用户部门，指定人可访问必须包含当前用户，并补充权限过滤测试。
- 移除知识库沙盒文件系统映射：不再通过 `/home/gem/kbs` 暴露知识库文件树，Agent 继续使用 `query_kb` 与 `open_kb_document` 访问知识库内容。
- 修复 MinerU 文档解析配置说明：文档处理指南原先指引启动 `openai-server`（30000 端口，仅提供 `/v1/chat/completions`），与解析器实际调用的 `/file_parse` 接口不匹配导致 `mineru_ocr` 不可用；更正为使用项目内置的 `mineru-api` 服务（30001 端口），并补充镜像构建与显存调优说明。
- 规范 Agent 知识库 Search/Find/Open 工具协议：`resource_id` 统一表示知识库 `kb_id`，Search 返回结构化 `resource_id/file_id/chunk` 结果，新增 `find_kb_document` 在已知文件内做关键词或正则定位，Open 默认窗口扩大到 1800 行。
- 收敛知识库分块配置：分块预设仅表达策略选择，通用分块参数统一通过 `chunk_parser_config` 传递；移除 `chunk_size`、`chunk_overlap`、`qa_separator` 等旧 root 字段兼容。
- 收敛知识库文件解析参数：文件级 `processing_params` 统一保存 `ocr_engine` 与 `ocr_engine_config`，解析阶段直接使用该结构并保留分块参数快照。
- 修复知识库文件大小显示为 0 的问题：文件上传时 `file_sizes` 参数未正确传播或历史数据缺失导致 DB 中 `file_size` 为 `None`；新增 `MinIOClient.stat_file/astat_file` 获取文件大小方法，`add_file_record` 在 `size` 缺失时从 MinIO 回补，`_load_metadata` 加载元数据后自动为缺少 `size` 的文件从 MinIO 补全并持久化。
- 优化评估基准自动生成：生成任务支持配置队列并发数，默认 10，范围 1-20。
- 完善模型供应商类型：普通聊天模型运行时新增 Anthropic provider type 适配，并清理不再支持的旧 provider type 入口。
- 重梳理知识库评估存储：评估数据集、题目、评估运行和逐题结果统一入库，JSONL 仅作为导入/导出格式；后端和前端 API 统一使用 dataset/run 语义；评估运行支持用户命名，历史记录按名称展示，综合评分只聚合检索指标。
- 扩展知识库上传来源：添加“从工作区上传”模式，后端将当前用户工作区文件预处理上传到 MinIO，前端沿用现有 `addDocuments` 入库链路提交 MinIO URL、内容哈希和文件大小。
- 重构知识库详情页布局：`DatabaseInfo` 改为顶部详情 header + 左侧功能 tab 侧边栏 + 右侧内容区，Milvus 默认进入文件管理，并将检索测试、知识图谱、知识导图、检索配置、RAG 评估和评估基准统一纳入侧边栏导航；只读连接器保留检索测试与检索配置。
- 整合知识导图接口：移除独立 mindmap router 与前端 API 模块，思维导图生成、查询和文件列表接口统一收敛到知识库 API 下。
- 收敛独立模型配置模块运行时：运行时 chat / embedding / rerank 均统一从 provider 模块与模型缓存读取 `provider_id:model_id`；旧版静态模型配置、v1 slash spec、旧模型列表接口和 Ollama 适配已移除；内置 provider 模板补充 XiaomiMiMo、XiaomiMiMo Token Plan CN 与 Kimi Code（`kimi-for-coding`）。
- 调整智能体模型配置默认值：`BaseContext.model` 默认保持为空，运行时按“请求模型 > 智能体配置模型 > 系统默认模型”解析；子智能体未配置模型时继承主智能体当前运行模型，避免把系统默认模型固化进每个智能体配置。
- 调整智能体配置归属与字段权限：`AgentConfig` 从部门共享改为按 `uid` 隔离，所有登录用户可管理自己的配置；`BaseContext` 支持字段级 `auth` 元数据，后端按用户角色过滤可见与可保存的配置项。
- 新增用户级沙盒环境变量：增加 `agent_envs` 表与 `/api/user/agent-env` 接口，设置面板支持当前用户维护 Agent 沙盒环境变量；创建新沙盒时与全局 `sandbox.env` 合并注入，用户变量优先。
- 收敛用户身份命名：原业务登录标识统一改为 `uid`，Agent/LangGraph runtime、conversation、agent_run、sandbox 路径和前端用户态均使用字符串 `uid`；`user_id` 仅保留给外部响应中的数值 `users.id` 或真实外键场景。
- 工作区知识库分类显示：知识库侧边栏按创建者分组为“我的知识库”和“共享知识库”，自己创建的知识库显示在“我的知识库”下，非自己创建的显示在“共享知识库”下；`knowledge_bases` 表新增 `created_by` 字段记录创建者 uid。
- 工作区文件上传支持多选：`/workspace/upload` 与 Viewer 工作区上传统一使用 `files` 多文件字段，一次最多上传 50 个文件，批量上传失败时清理本次已写入文件。
- 聊天附件新增 MinIO tmp 临时上传、可选 PDF/图片解析、确认后加入线程附件的流程；前端改为弹窗内上传、解析与确认。
- 修复智能体对话上传透明 PNG 后图片失真的问题：多模态图片处理在导出 RGB 前会先按白底合成 alpha 通道，避免透明像素中的隐藏颜色被直接转为可见像素；交付物预览优先按文件头识别 MIME，避免 `.jpg` 文件名包裹 PNG 内容时前端按错误格式加载；Agent run 输入消息会持久化为 `multimodal_image`，刷新历史后仍能显示用户上传图片。
- 优化智能体对话页细节：状态面板隐藏空 section，待办名称限制为 20 个中文汉字以内，模型选择器展示供应商名称，并收紧附件状态标签与文件编辑浮动操作样式；
- 标准化 Agent run/SSE 执行链路：run 创建时持久化输入消息并提交后入队，worker 统一写入 Redis Stream envelope，SSE 输出 `event/data/id`、心跳注释、`Last-Event-ID` 回放和终止 `end` 事件；前端强制使用 run API 并支持 ask_user_question 中断后以 resume run 恢复；事件 envelope 构造收敛到统一 helper，前端优先使用 envelope 一级 `thread_id` 路由。
- Agent run SSE 新增 `verbose=false` 精简模式：默认仍返回完整事件载荷；精简模式仅在 SSE 输出前重建最小 payload，跳过 `metadata` 和空 `StarRing.agent_state`，将同一 data 内的 `request_id` 外提为单个字段，移除 chunk 中重复的 `meta`、`metadata`、`thread_id`、`response`、空 `namespace` 和图片 base64 等调试字段，保留消息增量、工具调用、工具结果、非空 Agent state、终止状态和 SSE 游标，前端订阅默认使用精简模式。
- 修复 SiliconFlow MiniMax 与阿里云百炼工具调用流式兼容：二者的 OpenAI 兼容流经 LangGraph v3 event stream 累积工具调用时会丢失关键字段（MiniMax 在参数增量 chunk 返回空 `function.name`，百炼丢失 `tool_call.id`），空值被写入 checkpoint 后会导致工具执行失败或工具结果无法按 `tool_call_id` 关联、工具状态永远停留在“进行中”；这两类提供商默认对工具调用禁用流式模型响应（正文回答仍流式），保留 LangGraph v3 运行事件并拿到完整 tool_call。该缺陷属 LangChain v3 流式协议上游问题（参见 langchain#37420、langchainjs#10937、langgraphjs#2496），截至 langchain-core 1.4.4 仍未修复，待上游修复后可移除对应提供商的禁流式处理。
- 收敛后端模块边界：文档解析从 `plugins.parser` 移动到 `knowledge.parser`，内容审查从 `plugins.guard` 移动到 `services.guard`。
- 收敛文件服务边界：文件预览判断抽为独立服务，Viewer 文件系统的 workspace 分支复用用户 workspace 服务，线程运行时上下文解析从泛化 `filesystem_service` 拆出为 agent runtime helper。
- 升级 DeepAgents 到 0.6.7 并适配新版文件系统协议：SubAgentMiddleware 改为显式 subagent spec，Skills prompt 补齐新版占位符；sandbox/skills backend 复用新版 `ReadResult`、`GlobResult`、`GrepResult` 等协议类型，文件权限在 backend 层明确区分 skills、uploads、outputs 与 workspace，保留最小 `CustomCompositeBackend` 以避免非 route glob 误扫其他 route；Agent 上下文压缩改为复用 DeepAgents SummarizationMiddleware，历史摘要与大工具结果统一 offload 到 outputs。
- 优化聊天输入 @ 文件提及：未创建 Thread 时可搜索用户 workspace，创建 Thread 后按当前对话文件优先、workspace 兜底的来源顺序搜索，并拆分 workspace/thread 缓存避免假 thread 与跨用户缓存污染；输入框与用户消息支持将 raw mention 渲染为带类型图标的引用单元，文件仅显示文件名且保留原始沙盒路径文本。
- 重构子智能体为 Agent-backed 形态：移除旧 `subagents` 表与 `/api/system/subagents` 管理链路，子智能体改为 `agents.is_subagent=true` 且使用 `SubAgentBackend`，创建/编辑统一走 Agent 管理入口；内置后端收敛为 `ChatbotAgent` 与 `SubAgentBackend`，Context 分为 `BaseContext`、`ChatBotContext` 与 `SubAgentContext`；主 Agent 通过 StarRing task middleware 启动真实子 Agent graph，子智能体不再嵌套调用子智能体。沙盒挂载同步拆分为 child checkpoint thread、父对话 uploads/outputs、用户级 workspace 与子 Agent skills scope；主线程状态记录 `subagent_runs` 并在前端 task 工具中展示子智能体名称、执行状态、child thread 和产物，task 工具结果会暴露 child thread ID 且支持传回 `thread_id` 继续既有子智能体线程；子智能体执行复用 `agent_runs(run_type=subagent)` 记录父 run、child thread 与状态，child thread state 查询以 `agent_runs` 关系为准，不再解析 thread ID 反推父线程；真实流式 E2E 覆盖子智能体输出文件可由父线程文件/Viewer API 读取。流式链路参考 DeepAgents event streaming，后端将 LangGraph v3 raw event 归一化为 StarRing semantic stream event，按父/子线程归属隔离 run SSE chunk，并支持通过 child thread state 拉取子智能体中间过程。
- 修正评估综合得分计算：`overall_score` 改为有答案准确率时取各题准确率平均，否则取各题 `recall@10` 平均，不再把 recall/f1/各 k 检索指标混合平均；历史已存运行不回填。
- 清理无效鉴权中间件：移除启动时未实际校验令牌的 `AuthMiddleware` 和公开路径残留判断，后端认证边界明确收敛到路由依赖；`/api/auth/me` 改为强制登录并补充未登录访问返回 401 的集成测试。

## v0.6.2 (2026-05-22)

### 新增

- 新增个人工作区预览与管理：提供独立于对话 thread 的用户级 workspace API，并增加“工作区”页面，用于浏览、预览、编辑、上传、下载、删除个人 workspace 文件；默认创建 `agents/AGENTS.md`，并在 Agent 执行时将其内容追加到系统提示词。
- 新增独立模型配置模块：增加 `model_providers` 表、独立管理接口和“模型配置”页面，支持 provider 基础信息、远端候选模型、enabled models 配置和手动添加模型能力。
- 新增远程 Skill 批量安装能力：后端新增 `install_remote_skills_batch()` 与 `POST /remote/install-batch`，前端补充批处理安装 API 和 UI 逻辑。

### 优化

- 下放扩展管理权限：普通管理员现在可进入扩展管理并完整管理 Tools、MCP、SubAgent、Skills；同步放开 Skill 管理接口权限并补充权限测试。
- 调整 Agent 知识库默认选择：未显式配置知识库时默认启用当前用户可访问的全部知识库，显式保存空列表仍表示不启用知识库。
- 优化评估基准自动生成：仅支持 commonrag/Milvus 知识库，默认参考 chunks 数量改为 1；多 chunk 场景复用知识库向量检索选择相似 chunks，不再对全量 chunks 重新计算 embedding。
- 优化 Agent 输入框文件 mention：用户级 workspace 文件候选改为从独立 workspace API 递归加载，不再依赖 active thread；插入时仍转换为 `/home/gem/user-data/workspace/` 沙盒虚拟路径。
- 调整知识库思维导图后端结构：将思维导图路由文件重命名为知识库语义更明确的 router，并把文件列表整理、提示词构建、AI JSON 解析等纯逻辑下沉到知识库 utils。
- 收敛知识库评估后端结构：将评估指标、单题评估、答案生成提示词和自动基准生成算法下沉到 `knowledge/eval`，`EvaluationService` 保留任务、文件和持久化编排职责。
- 扩展管理界面交互逻辑重构：MCP / Subagents / Skills 从“左侧边栏 + 右侧详情面板”调整为“卡片式网格布局 + 路由跳转二级页面”，工具标签页改为卡片网格布局 + 弹窗详情。
- 统一卡片样式：`ExtensionCard` 新增 `tags` prop 并复用于知识库列表页，知识库列表改用 `ExtensionCard` + `ExtensionCardGrid` 替代原有自定义卡片。
- 调整应用主导航：`AppLayout` 升级为默认展开的侧边栏，保留折叠态图标导航，并统一导航项、任务中心、GitHub、用户信息的图标与文字对齐。
- 合并智能体对话导航：移除 `AgentChatComponent` 内部聊天侧边栏，将新建对话入口和对话历史移动到 `AppLayout` 主侧边栏，并通过共享线程 store 统一管理。
- 统一前端 Markdown 预览渲染：新增共享 `MarkdownPreview` 组件与 `markdown_preview` 渲染工具，替换 Agent 消息、文件预览、知识库 chunk、任务工具结果、聊天导出等场景中的旧预览实现。

### 修复

- 修复聊天中普通用户 `@` 提及出不来技能和 MCP 列表的问题：放宽技能列表与 MCP 服务器列表读取接口至已登录用户，并对普通用户请求的 MCP 列表进行敏感连接参数脱敏。
- 修复知识库文档入库状态回退：当已解析文件缺失 `markdown_file` 解析产物时，索引流程会将文件状态恢复为未解析，便于重新解析。
- 修复附件上传后未立即刷新 mention 候选的问题。
- 加固 JWT 鉴权安全：移除历史默认密钥回退，初始化脚本支持生成并持久化 `JWT_SECRET_KEY` 与 `StarRing_INSTANCE_ID`，签发和验证令牌时校验 `iss/aud`，并拒绝已删除或登录锁定用户继续使用旧令牌访问系统。
- 修复模型配置路由请求模型未接收 `embedding_base_url` / `rerank_base_url` 导致前端已填写仍被后端校验拦截的问题。
- 修复知识库文档处理任务状态不一致问题：文件解析失败时任务中心正确显示"失败"而非"已完成"。

## v0.6.1 (2026-04-24)

### 新增

- 合并知识库导航入口：左侧导航仅保留"知识库"，文档知识库与图知识库在页面 header 中通过同一组轻量切换入口切换
- 抽象页面轻量切换 header：知识库与扩展管理页直接共用 `ViewSwitchHeader`，收敛文档知识库、知识图谱、Tools、MCP、Subagents、Skills 等入口的信息层级
- 调整任务中心交互：入口移动到 GitHub 按钮下方，并将右侧抽屉展示改为居中弹窗
- 将 `StarRing` 从 uv workspace 成员调整为 `backend/package` 下可独立构建的本地 Python 包，backend 通过 path dependency 以已安装包形式发现依赖
- 新增 Skills 远程安装能力：Skills 管理页支持填写 `owner/repo` 或 GitHub URL，后端通过隔离的临时 `HOME` 调用 `npx skills add` 下载指定 skill
- 调整部门删除语义：删除部门时不再要求用户数为 0，而是将部门下用户迁移到默认部门
- 扩展 viewer 工作区文件操作：`/home/gem/user-data/workspace` 支持从文件系统面板新建文件夹和上传文件
- 为历史线程补充前端本地配置变更提示：当已有历史消息的对话中切换 Agent、切换配置或编辑配置项时，插入非持久化的信息提示
- 调整 Worker run 模式下的消息首屏反馈：前端发送消息时先乐观渲染用户消息，再将前端生成的 `request_id` 透传给 `/api/chat/runs` 与服务端 `init` 对账
- 调整聊天首页的智能体切换入口：当智能体数量 `>= 4` 或内容区宽度小于 `380px` 时自动收敛为"当前智能体 + 下拉按钮"形式
- 调整智能体对话中的工具调用展示：连续工具调用默认折叠为"调用了 N 个工具"的轻量摘要
- 调整输入框配置入口与侧边栏头尾交互：输入区配置按钮改为轻量 dropdown 触发器

### 修复

- 修复沙盒 `workspace` 隔离粒度：宿主机目录从共享 `saves/threads/shared/workspace` 收敛为用户级 `saves/threads/shared/<user_id>/workspace`
- 收紧文件系统安全边界：viewer/chat 下载与删除路径统一基于解析后的真实路径做允许目录校验，阻止通过软链接逃逸工作区/线程目录
- 修复 OIDC 原始用户名绑定中的占位用户解析：解析目标用户 ID 时改为从右侧拆分，避免 `sub` 中包含冒号时把已绑定账号误判成冲突账号
- 修复 DOCX 解析中的图片回插顺序：Docling 导出的多个 `<!-- image -->` 占位符现在按文档图片顺序替换
- 修复前端依赖安全告警：通过 `pnpm.overrides` 将传递依赖 `flatted` 锁定到 `3.4.2`、`lodash-es` 锁定到 `4.18.1`
- 修复对话摘要中间件的工具结果卸载链路：摘要触发时改为将大体积 `ToolMessage` 写入当前 agent 可见的 sandbox outputs 路径
- 修复 agents 页对话侧边栏在 `keep-alive` 路由切换后的误关闭问题
- 调整 Milvus 混合检索实现：集合 schema 增加 BM25 稀疏向量字段、BM25 函数和中文 analyzer 配置
- 重构 MCP 运行时配置加载模型：移除 `MCP_SERVERS` 作为运行正确性前提的设计，改为每次直接从数据库读取最新 MCP 配置
- 为知识库检索工具补充 `metadata.filepath` 注入：在 `query_kb` 统一出口基于会话可见知识库构建 `file_id -> /home/gem/kbs/...` 映射并回填 Milvus 检索结果
- 移除知识库沙盒文件系统映射：Agent 不再通过 `/home/gem/kbs` 遍历知识库文件，继续通过 `query_kb` 和 `open_kb_document` 检索与打开文档。

## v0.6.0 (2026-04-01)


### 新增
- 重构后端代码 src -> backend/package/StarRing
- 重构文档解析，统一文档解析体验，并新增 Parser 类
- 新增 LITE 模式启动，启动时不加载知识库、知识图谱相关模块，可以使用 make up-lite 快捷启动
- 新增沙盒环境，详见后续文档更新，统一沙盒虚拟路径前缀默认值为 `/home/gem/user-data`
- 新增基于沙盒的文件系统，前端工作台可以查看文件系统，支持预览（文本、图片、PDF、HTML）、下载文件
- 新增 `present_artifacts` 内置工具：Agent 可将 `/home/gem/user-data/outputs/` 下的结果文件显式写入 LangGraph state 的 `artifacts` 字段，前端支持在输入框顶部以默认折叠的堆叠卡片展示本轮交付物文件，并保持可下载、可预览能力
- 交付物卡片新增“保存到工作区”能力：支持将单个交付物复制到共享目录 `workspace/saved_artifacts/`，并复用现有文件树/预览/mention 体系立即可见
- 新增基于沙盒的知识库只读映射，按“用户可访问知识库 ∩ 当前 Agent 已启用知识库”暴露原始文件与解析后的 Markdown
- 重构附件系统，直接集成在了沙盒文件系统中，附件上传后直接落盘到沙盒挂载目录
- 优化前端流式消息体验：新增通用 `useStreamSmoother` 调度层，统一平滑 Agent runs SSE、普通聊天流与审批恢复流中的 `loading` chunk
- 优化项目文档说明，并添加贡献指南
- 重构前端 Agent 路由结构，体验更加顺畅，切换更加自然（类 chatgpt 体验）
- 新增 API Key 认证功能，支持外部系统通过 API Key 调用系统服务
- 新增 subagents 的支持，支持在 web 中添加 subagents，以及两个内置的子智能体
- 新增内置Skills reporter，并移除内置 Agent reporter，数据库报表将由 Skills 完成
- 新增内置 Skills `deep-reporter`，用于指导生成科研报告、行业调研和其他深度分析类长报告
- 重构内置 Skills/MCP/Subagents 安装/添加/移除机制：内置 skill 支持按需安装、基于 `version + content_hash` 的更新提示与覆盖确认，不再使用服务器级开关切换
- 新增知识库 PDF、图片的预览功能
- 重构后端测试目录结构：按 `unit / integration / e2e` 分层迁移现有测试，拆分全局 `conftest.py`，统一测试入口为 `uv run --group test pytest`，并新增独立测试规范文档 `docs/develop-guides/testing-guidelines.md`
- 新增工具元数据 `config_guide` 字段：后端工具列表接口现在可返回“给人看的配置说明”，前端工具详情页会展示该说明，用于提示工具使用前需要配置的环境变量或入口；首批为 MySQL 工具和 `Qwen-Image` 补充了配置指引
- 补充 Langfuse 集成方案文档：明确采用“云端优先、先 tracing 后 feedback”的接入路径，并约定 StarRing 的 `user/thread` 到 Langfuse `user_id/session_id` 的映射关系
- 新增面向用户的 Langfuse 集成文档：在“高级配置”分组中说明 Langfuse 的定位、能力、配置方式与查看路径，并与当前 `LANGFUSE_BASE_URL` 配置保持一致

<!-- 添加到这里 -->

### 修复

- 调整聊天首页的智能体切换入口：在无历史对话时，智能体数量 `<= 3` 且 `chat-main` 宽度不小于 `380px` 时继续使用横向 segmented；当智能体数量 `>= 4` 或内容区宽度小于 `380px` 时自动收敛为“当前智能体 + 下拉按钮”形式，避免多智能体或窄屏场景下入口被截断
- 发布前一致性修复：统一 0.6.0 版本号（backend/package/web）、更新 dev/prod 镜像标签语义（`0.6.0.dev` / `0.6.0`），并为 `/api/system/health` 补充 `version` 字段，提升部署可观测性与发版追溯能力
- 收敛“状态工作台”自动弹出规则：前端不再因为共享 `workspace` 或文件系统天然存在内容而默认展开，改为仅在 `/home/gem/user-data/uploads` 或 `/home/gem/user-data/outputs` 下检测到实际文件时自动弹出；手动打开、关闭、刷新和伸缩交互保持不变
- 调整智能体 todo 展示语义：待办状态不再作为 `capabilities` 前端开关，而是直接根据运行态 `agent_state.todos` 渲染；同时将 todo 入口从 Agent Panel 移到输入框内的轻量浮层，并让右侧“状态工作台”收敛为文件系统视图，输入框按钮文案同步由“状态”调整为“文件”
- 优化 Agent 输入框 mention 行为：在保留附件 mention 的同时，将共享 `workspace` 文件纳入候选范围；并将 `@` 空查询时的候选列表改为空，仅在继续输入后再执行筛选，避免工作区文件过多时直接铺满下拉面板
- 为前端工作台文件树补齐文件删除能力：`/api/viewer/filesystem/file` 新增删除接口，`AgentPanel` 文件节点新增删除按钮与确认交互，删除后会同步刷新树与预览状态
- 扩展 Agent Panel 状态工作台删除能力：继续复用 `DELETE /api/viewer/filesystem/file`，在保持接口不变的前提下支持删除文件夹；空目录与非空目录现在都会递归删除，`workspace` 下目录也可直接清理，前端目录节点同步新增删除入口与对应确认文案
- 调整前端工作台文件预览交互：恢复默认侧边/弹窗预览，并新增显式“全屏预览”入口；全屏模式下由预览内容直接覆盖整页，仅保留右上角悬浮关闭按钮；同时修复 HTML 文件首次在弹窗中预览偶现白屏的问题，改为在内容更新后强制重建 `iframe`
- 统一 Agent Panel 文件预览与消息区交付物预览组件：两处改为复用同一套 `AgentFilePreview` 预览实现，并为交付物预览补齐与工作台一致的“全屏预览”入口
- 修复交付物卡片展开后的长列表展示：当单轮交付物文件超过面板可见高度时，卡片内容区改为显示纵向滚动条，避免超过约 10 项后底部文件与操作按钮被裁切
- 兼容旧版已安装的内置 `reporter` 技能记录：`update_builtin_skill` 现在会识别由 `system` 或 `builtin-system` 管理的历史记录，避免更新时误报“技能 `reporter` 不是内置 skill”
- 调整沙盒 user-data 目录隔离策略：`workspace` 改为共享目录 `saves/threads/shared/workspace`，`uploads/outputs` 继续保持 thread 级隔离；同时更新 thread artifact 权限校验、viewer 文件系统列举逻辑，以及对应的 router/E2E 测试
- 重构聊天接口请求模型：流式与非流式聊天统一使用 `query + agent_config_id` 请求体，并移除路径中的 `agent_id`；同时修复非流式接口实际误走流式执行链路的问题，改为调用 `invoke_messages` 一次性执行，并补充对应测试
- 修复对话线程与 Agent 配置错位的问题：发送消息时将当前 `agent_config_id` 绑定到 thread 的 `extra_metadata`，线程列表接口返回该绑定值，前端切换历史 thread 时会自动恢复对应配置
- 为沙盒与 viewer 文件系统补齐知识库只读映射：新增 `/home/gem/kbs` 命名空间，按“用户可访问知识库 ∩ 当前 Agent 已启用知识库”暴露原始文件与解析后的 Markdown，并补充对应后端与 viewer 路由测试
- 优化 viewer 文件系统目录树加载：根目录与 `/home/gem/user-data` 改为直接读取本地线程挂载目录，不再为只读树视图触发 sandbox 冷启动，并补充对应后端测试
- 修复 `/home/gem/user-data` 根目录文件不可见的问题：根目录现在会同时展示 thread 目录下的真实文件和 `workspace` 入口，不再只保留固定命名空间目录
- 修复前端工具图标与渲染匹配不准确的问题：工具管理列表与工具调用结果统一改为基于工具 `id` 的精确映射，避免模糊匹配导致的误渲染，未命中的工具不再显示默认扳手图标
- 修复 GitHub Pages 文档部署工作流失败：移除 `actions/setup-node@v4` 对不存在 `docs/package-lock.json` 的缓存依赖，并将 `docs` 目录安装命令从 `npm ci` 调整为 `npm install`，避免因未提交锁文件导致 CI 在依赖缓存和安装阶段直接失败
- 修正沙盒 provisioner backend 命名与配置说明：统一对外使用 `docker` / `kubernetes`，保留 `local` 作为兼容别名；同步清理 compose 中未生效的 provisioner 环境变量、补齐 K8s 相关变量注释，并更新沙盒架构文档中的默认模式与 backend 描述
- 修复智能体配置列表接口在“无配置自动创建默认配置”路径下的参数缺失：补齐 `get_or_create_default` 的 `agent_id` 入参，避免 `/api/chat/agent/{agent_id}/configs` 返回 500
- 修复 LightRAG 同库写入并发导致的入库失败：为 `index_file` / `update_content` 增加按知识库维度的串行锁，并补齐 `documents` 接口 `auto_index` 阶段对最新解析状态的回写与回归测试，避免长时间入库任务进行中再次选择同库文件时直接并发写入报错

<!-- 添加到这里 -->


---


## v0.5

### 新增

- 优化 OCR 体验并新增对 Deepseek OCR 的支持
- 优化 RAG 检索，支持根据文件 pattern 来检索（Agentic Mode）
- 重构智能体对于“工具变更/模型变更”的处理逻辑，无需导入更复杂的中间件
- 重构知识库的 Agentic 配置逻辑，与 Tools 解耦
- 将工具与知识库解耦，在 context 中就完成解耦，虽然最终都是在 Agent 中的 get_tools 中获取
- 优化chunk逻辑，移除 QA 分割，集成到普通分块中，并优化可视化逻辑
- 重构知识库处理逻辑，分为 上传—解析—入库 三个阶段
- 重构 MCP 相关配置，使用数据库来控制 [#469](https://github.com/xerrors/StarRing/pull/469)
- 使用 docling 解析 office 文件（docx/xlsx/pptx）
- 优化后端的依赖，减少镜像体积 [#428](https://github.com/xerrors/StarRing/issues/428)
- 优化 liaghtrag 的知识库调用结果，提供 content/graph/both 多个选项
- 优化数据库查询工具，可通过设计环境变量添加描述，让模型更好的调用
- 优化任务组件，改用 postgresql 存储，并新增删除任务的接口
- 支持更多类型的文档源的导入功能（支持后端配置的白名单的 URL 导入）

### 修复

- 修复文件上传弹窗中 OCR 下拉选项展开时不会自动检查服务状态的问题
- 修复知识图谱上传的向量配置错误，并新增模型选择以及 batch size 选择
- 修复部分场景下获取工具列表报错 [#470](https://github.com/xerrors/StarRing/pull/470)
- 修改方法备注信息 [#478](https://github.com/xerrors/StarRing/pull/478)
- 修复多次 human-in-the-loop 的渲染解析问题 [#453](https://github.com/xerrors/StarRing/issues/453) [#475](https://github.com/xerrors/StarRing/pull/475)
- 修复沙盒后端接入回归：补齐 composite backend 的 `sandbox_backend` 参数、限制 `/api/sandbox/prepare` 仅允许访问当前用户线程、确保 `release()` 之后的 `destroy()` 会真正停止热池容器，并恢复 docker-compose 的完整模式默认值
- 重构沙盒为 deer-flow 风格的 AIO provider：切换为 thread-local sandbox、统一 `/home/gem/user-data/{workspace,uploads,outputs}` 固定路径、移除公开 `/api/sandbox/*` 生命周期接口，并补充 lite 模式下的 provider 生命周期、filesystem API 与 sandbox 复用/隔离 E2E 验证
- 调整聊天附件存储链路：线程附件改为直接落盘到 `saves/threads/<thread_id>/user-data/uploads`，解析成功后额外生成 `uploads/attachments/*.md`，不再依赖 MinIO 或显式上传到 sandbox
- 修复知识库文件列表包体异常膨胀：上传阶段不再把批次级 `content_hashes` 写入每个文件的 `processing_params`，并从数据库详情列表接口中移除该字段，改为按需读取单文件详情

## v0.4

### 新增
- 新增对于上传附件的智能体中间件，详见[文档](https://xerrors.github.io/StarRing/advanced/agents-config.html#%E6%96%87%E4%BB%B6%E4%B8%8A%E4%BC%A0%E4%B8%AD%E9%97%B4%E4%BB%B6)
- 新增多模态模型支持（当前仅支持图片），详见[文档](https://xerrors.github.io/StarRing/advanced/agents-config.html#%E5%A4%9A%E6%A8%A1%E6%80%81%E5%9B%BE%E7%89%87%E6%94%AF%E6%8C%81)
- 新建 DeepAgents 智能体（深度分析智能体），支持 todo，files 等渲染，支持文件的下载。
- 新增基于知识库文件生成思维导图功能（[#335](https://github.com/xerrors/StarRing/pull/335#issuecomment-3530976425)）
- 新增基于知识库文件生成示例问题功能（[#335](https://github.com/xerrors/StarRing/pull/335#issuecomment-3530976425)）
- 新增知识库支持文件夹/压缩包上传的功能（[#335](https://github.com/xerrors/StarRing/pull/335#issuecomment-3530976425)）
- 新增自定义模型支持、新增 dashscope rerank/embeddings 模型的支持
- 新增文档解析的图片支持，已支持 MinerU Officical、Docs、Markdown Zip格式
- 新增暗色模式支持并调整整体 UI（[#343](https://github.com/xerrors/StarRing/pull/343)）
- 新增知识库评估功能，支持导入评估基准或者自动构建评估基准（目前仅支持Milvus类型知识库）详见[文档](https://xerrors.github.io/StarRing/intro/evaluation.html)
- 新增同名文件处理逻辑：遇到同名文件则在上传区域提示，是否删除旧文件
- 新增生产环境部署脚本，固定 python 依赖版本，提升部署稳定性
- 优化图谱可视化方式，统一图谱数据结构，统一使用基于 G6 的可视化方式，同时支持上传带属性的图谱文件，详见[文档](https://xerrors.github.io/StarRing/intro/knowledge-base.html#_1-%E4%BB%A5%E4%B8%89%E5%85%83%E7%BB%84%E5%BD%A2%E5%BC%8F%E5%AF%BC%E5%85%A5)
- 优化 DBManager / ConversationManager，支持异步操作
- 优化 知识库详情页面，更加简洁清晰，增强文件下载功能

### 修复
- 修复 GitHub Actions 的 Ruff CI 在仓库根目录执行 `uv sync` 导致找不到 `backend/pyproject.toml` 的问题，同时统一检查路径为 `backend/package`
- 修复重排序模型实际未生效的问题
- 修复消息中断后消息消失的问题，并改善异常效果
- 修复当前版本如果调用结果为空的时候，工具调用状态会一直处于调用状态，尽管调用是成功的
- 修复检索配置实际未生效的问题
- 修复 sandbox 文件系统 `ls` 在异常输出下触发 `KeyError: 'path'` 的问题，并将工具调用异常降级为错误消息，避免直接中断聊天 stream
- 修复智能体状态面板中文件树仍依赖 `agent_state.files` 的问题，改为通过真实 `/api/filesystem/*` 接口按层懒加载后端可见文件系统，并让输入框下方状态按钮常态化打开工作区视图
- 为工作台新增 viewer-oriented filesystem service 与 `/api/viewer/filesystem/*` 接口，解耦 agent backend 语义，支持真实目录浏览、原始文件读取与下载
- 重写沙盒技术文档，明确 thread-local sandbox、viewer-oriented filesystem service、`/mnt` 命名空间、skills 可见性与当前实现边界，替换过时的 `/api/sandbox/*` 与 user-level 设计描述
- 收紧沙盒遗留代码：修复未注册 `sandbox_router` 中残留的 user/thread 参数错位，改进宿主机挂载路径映射逻辑，并为 remote sandbox provisioner 增加基础 URL 校验与销毁失败日志
- 修复 builtin skill 内容哈希计算对单文件使用 `read_bytes()` 的无上限内存读取问题，改为分块计算并补充回归测试

### 破坏性更新

- 移除 Chroma 的支持，当前版本标记为移除
- 移除模型配置预设的 TogetherAI


## v0.3
### Added
- 添加测试脚本，覆盖最常见的功能（已覆盖API）
- 新建 tasker 模块，用来管理所有的后台任务，UI 上使用侧边栏管理。Tasker 中获取历史任务的时候，仅获取 top100 个 task。
- 优化对文档信息的检索展示（检索结果页、详情页）
- 优化全局配置的管理模型，优化配置管理
- 支持 MinerU 2.5 的解析方法 <Badge type="info" text="0.3.5" />
- 修改现有的智能体Demo，并尽量将默认助手的特性兼容到 LangGraph 的 [`create_agent`](https://docs.langchain.com/oss/python/langchain/agents) 中
- 基于 create_agent 创建 SQL Viewer 智能体 <Badge type="info" text="0.3.5" />
- 优化 MCP 逻辑，支持 common + special 创建方式 <Badge type="info" text="0.3.5" />
- LightRAG 知识库应该可以支持修改 LLM

### Fixed
- 修复本地知识库的 metadata 和 向量数据库中不一致的情况。
- v1 版本的 LangGraph 的工具渲染有问题
- upload 接口会阻塞主进程
- LightRAG 知识库查看不了解析后的文本，偶然出现，未复现
- 智能体的加载状态有问题：（1）智能体加载没有动画；（2）切换对话和加载中，使用同一个loading状态。
- 前端工具调用渲染出现问题
- 当前 ReAct 智能体有消息顺序错乱的 bug，且不会默认调用工具
- 修复文件管理：（1）文件选择的时候会跨数据库；（2）文件校验会算上失败的文件；
