# CodeAct 代码执行

CodeAct 是一种执行范式：让模型直接编写 Python 代码在沙盒中运行，用一段代码完成多步工具组合、批量检索与数据加工，而不是逐条发起工具调用（ReAct）。对需要循环、聚合、多次检索再汇总的任务，CodeAct 能显著减少模型调用轮次。

设计文档见 `docs/vibe/2026-07-27-codeact-execution-paradigm.md`。

## 开启方式

CodeAct 是智能体的静态配置开关。在智能体配置页把「代码执行 (CodeAct)」打开即可（对应 `ChatBotContext.use_code_act`，与「长期记忆」同一处，前端按配置项自动渲染）。

开关默认关闭，存量智能体行为零变化——只有显式开启的智能体才会挂载 `CodeActMiddleware` 并获得 `execute_python` 工具。

## 工作方式

开启后，中间件向智能体注入 `execute_python` 工具，并在系统提示中追加 CodeAct 使用说明（含本次运行可桥接的平台工具清单）。一次执行的流程：

1. 模型提交一段 Python 代码。
2. 中间件生成 run 级短时凭证（bridge token），把「用户身份 + 白名单」快照写入 Redis。
3. 代码与 `starring_tools.py` 客户端写入沙盒 `workspace/.codeact/` 目录，随后执行。
4. 执行结束吊销 token；stdout/stderr 作为结果返回给模型。

只有 `print` 到 stdout/stderr 的内容会返回，因此需要 `print` 出想让模型看到的结果。

## 在代码中调用平台工具

沙盒内代码可通过工具桥回调平台工具：

```python
import starring_tools

# 调用知识库检索工具
result = starring_tools.call("query_kb", kb_id="xxx", query_text="关键词")
print(result)
```

- 调用失败会抛出 `starring_tools.ToolCallError`，traceback 原样返回给模型。
- `starring_tools.py` 仅依赖 Python 标准库（urllib），不要求沙盒镜像预装任何包。

### 可桥接的工具范围（白名单）

工具桥只分发「本次运行实际挂载、类别属于 `buildin` / `knowledge`、且不在排除清单内」的工具，权限边界与对话内直接调用完全一致（知识库可见性等沿用发起用户）。

排除清单固定包含以下工具（即使类别匹配也不进桥）：

| 工具 | 排除原因 |
| --- | --- |
| `execute_python` | 防止沙盒内代码递归触发沙盒执行 |
| `ask_user_question` | 内部调用 LangGraph `interrupt()`，桥端点的 HTTP 上下文中无 graph 运行时 |
| `install_skill` | Skills 安装类，当前阶段不桥接 |
| `present_artifacts` | 返回 `Command` 更新 graph state，脱离 graph 无意义 |

`task`、MCP 类工具按类别天然不进桥。白名单外工具调用会被拒绝，并返回可读错误。

## 约束与安全

- **超时**：单次执行复用 `SANDBOX_EXEC_TIMEOUT_SECONDS`（默认 180s），避免死循环与长时间阻塞。
- **输出截断**：按 `SANDBOX_MAX_OUTPUT_BYTES`（默认 256KB）截断——成功保留头部（结果通常在前），失败保留尾部（traceback 在末尾）。
- **单次工具结果上限**：桥分发的单个工具结果超过 256KB 直接报错拒绝，引导模型改用分页或更精确的查询，而非基于残缺数据推断。
- **连续失败熔断**：同一次运行内连续失败 3 次后，`execute_python` 不再可用，提示模型改用常规工具完成任务；任意一次成功会清零计数。
- **鉴权**：bridge token 是 run 级短时凭证，写入 Redis（TTL = 执行超时 + 60s），执行结束显式吊销。桥端点 `POST /api/codeact/tool-call` 仅凭 `X-CodeAct-Token` 校验，不走用户 JWT。

## 路径约定

- 代码在沙盒 `workspace/.codeact/` 目录下执行，脚本保留在工作区，便于用户查看与复跑。
- 会话文件位于 `workspace/` 根目录，代码内可直接读写。
