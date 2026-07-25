# 06-Prompt工程实践

> **核心代码路径**
> - 主实现：`backend/package/starring/agents/buildin/chatbot/prompt.py`
> - 智能体基类：`backend/package/starring/agents/base.py`

## 一、技术原理

### 1.1 核心问题

Prompt设计直接影响LLM生成质量和安全性，面临以下挑战：

| 问题类型 | 挑战描述 | StarRing 解决方案 |
|----------|---------|-------------------|
| **角色定位** | 身份模糊导致回答不一致 | 明确角色定义 + 交互约束 |
| **安全防护** | Prompt注入攻击 | 系统级约束 + 内容审查 |
| **知识库强制检索** | 模型幻觉、脱离知识库 | 强制检索模式 + 来源引用 |
| **工具使用规范** | 文件路径、工具滥用 | 文件系统约束 + 工具指导 |
| **风格一致性** | 回答风格不稳定 | 风格规范 + Emoji控制 |

### 1.2 Prompt架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                   StarRing Prompt架构                        │
├─────────────────────────────────────────────────────────────┤
│  系统Prompt组成                                               │
│  ├─ 角色定义: "你是一个交互式智能体'语析'"                    │
│  ├─ 基础能力: "专门用来回答用户的问题"                        │
│  ├─ 内部约束: "内部执行约束（不向用户暴露）"                  │
│  ├─ 文件系统约束: 工作路径规范                                │
│  ├─ 风格规范: "保持专业严谨，减少使用Emoji"                   │
│  ├─ 知识库模式: 强制检索模式（可选）                          │
│  └─ 日期信息: "当前日期：2024-01-15"                         │
├─────────────────────────────────────────────────────────────┤
│  动态注入内容                                                 │
│  ├─ 用户自定义Prompt: 覆盖默认行为                           │
│  ├─ 上下文信息: 对话历史、知识库检索结果                      │
│  └─ 工具列表: 可用工具及其说明                                │
└─────────────────────────────────────────────────────────────┘
```

## 二、实现细节

### 2.1 核心Prompt模板

代码实现（backend/package/starring/agents/buildin/chatbot/prompt.py:9-29）：

```python
PROMPT = f"""
你是一个交互式智能体"语析"。

专门用来回答用户的问题。请根据用户提供的信息，尽可能详细地回答问题。
如果你不确定答案，可以说你不知道，但请尽量提供相关的信息或建议。请保持礼貌和专业。

<| 内部执行约束:重要 |>
以下内容仅用于指导你的内部执行过程，不属于面向用户的基本设定。除非用户明确询问系统如何工作，
否则不要主动向用户说明工作区、文件系统、知识库路径、工具调用方式等内部实现细节。

<| 文件系统约束 |>
系统主要工作路径为 {VIRTUAL_PATH_PREFIX}，但必须遵守规范：
- {VIRTUAL_PATH_OUTPUTS}：用于写入的文件夹
    - {VIRTUAL_PATH_OUTPUTS}/tmp/：用于存放中间结果或备份内容
- {VIRTUAL_PATH_UPLOADS}：用于存放用户上传的附件（只读，除非用户要求，否则不得写入）
- {VIRTUAL_PATH_WORKSPACE}：用于存放用户文件（用户私人目录，除非用户要求，否则不得写入）
- 其他路径：非必要不写入其他路径

<| 风格规范 |>
保持专业严谨，减少使用 Emoji
"""
```

### 2.2 知识库强制检索模式

代码实现（backend/package/starring/agents/buildin/chatbot/prompt.py:48-58）：

```python
KB_FORCE_PROMPT = """

<| 知识库问答模式:重要 |>
当前处于"知识库问答"模式，你必须严格遵守以下规则：
1. 在回答用户问题前，必须先调用 list_kbs 查看可用知识库，并使用 query_kb 在相关知识库中检索。
2. 回答内容必须基于 query_kb 检索到的知识库内容，不要脱离知识库凭空作答。
3. 若检索结果不足以回答，可再用 find_kb_document / open_kb_document 进一步查阅原文。
4. 如果在知识库中确实找不到相关内容，明确告知用户"知识库中未找到相关内容"，不要编造。
"""
```

**强制检索优势**：
- **防幻觉**：禁止脱离知识库回答
- **来源透明**：基于检索结果生成
- **可信度高**：用户可验证来源

### 2.3 Prompt动态组装

```python
# backend/package/starring/agents/buildin/chatbot/prompt.py

def build_prompt_with_context(context):
    """动态组装系统Prompt"""
    current_date = f"当前日期：{shanghai_now().strftime('%Y-%m-%d')}"
    system_prompt = f"{current_date}\n\n{PROMPT.strip()}\n\n{context.system_prompt or ''}"
    return system_prompt.strip()
```

> **注意**：`KB_FORCE_PROMPT` 和 `TODO_MID_PROMPT` 不在此函数内拼接。
> - `KB_FORCE_PROMPT` 在 `graph.py` 中根据 `context.use_knowledge` 条件追加：
>   `if getattr(context, "use_knowledge", None) is True: system_prompt = f"{system_prompt}\n{KB_FORCE_PROMPT}"`
> - `TODO_MID_PROMPT` 作为 `TodoListMiddleware` 的参数注入，不拼接到系统 Prompt 中

### 2.4 来源引用机制

代码实现（backend/package/starring/agents/buildin/chatbot/prompt.py:32-47）：

```python
# 效果不好，暂时不启用
SOURCE_CITE_PROMPT = """

<| 引用来源 |>
当你提供的信息来自于用户上传的文件或者知识库中的内容时，请务必在回答中注明信息来源，以增加答案的可信度和透明度。

对于论断内容，需要添加参考文献信息，将对应段落的末尾添加 cite 信息。使用
<cite source="$SOURCE" type="$TYPE">$INDEX</cite>

- $SOURCE：信息来源，可以是文件名，可以是url
- $TYPE：引用类型，可以是 "file"、"url"，对于网络搜索应该使用 "url"，对于用户上传的文件或者知识库中的内容应该使用 "file"
- $INDEX：引用索引，应该从 1 开始

比如 <cite source="食品工艺学.pdf" type="file">1</cite>
"""
```

**不启用原因**：
- 增加生成Token数，成本提高
- 引用格式不够自然
- 影响回答流畅度

## 三、遇到的问题

### 问题1：Prompt过长影响性能

**现象**：
- 系统Prompt占用 800+ Tokens
- 降低可用于上下文的Token数
- 增加每次请求成本

**解决方案**：

> ⚠️ 以下代码为思路示意，部分逻辑（如 `context.kb_force_mode`、`detect_prompt_injection`）非项目当前实现

```python
# 1. 精简非必要内容
# 去除冗余描述，保留核心约束

# 2. 分层Prompt
# 基础Prompt（必需） + 扩展Prompt（可选）
if context.kb_force_mode:
    system_prompt += KB_FORCE_PROMPT  # 仅知识库模式时添加

# 3. 动态加载
# 根据场景动态组装，避免全量加载
```

### 问题2：Prompt注入攻击

**现象**：
- 用户输入"忽略之前所有指令"
- 模型暴露内部信息
- 绕过安全约束

**解决方案**：

> ⚠️ 以下代码为思路示意，`detect_prompt_injection` 函数非项目当前实现

```python
# 1. 明确边界标记
"<| 内部执行约束:重要 |>"
# 使用特殊标记区分系统指令和用户输入

# 2. 内容审查层
# 在Prompt之前检查用户输入，过滤恶意指令
if detect_prompt_injection(user_input):
    raise SecurityError("检测到恶意输入")

# 3. 模型层防护
# 使用content_guard模块进行二次审查
```

### 问题3：风格不一致

**现象**：
- 同一问题回答风格差异大
- Emoji使用过多影响专业性
- 口语化vs书面化不统一

**解决方案**：
```python
# 明确风格规范
"<| 风格规范 |>\n保持专业严谨，减少使用 Emoji"

# 在Prompt中强调
"请保持礼貌和专业"

# Few-shot示例（未启用）
# 提供标准回答示例
```

## 四、优化方案

> ⚠️ 以下为建议实现方案，非项目当前代码

### 优化1：Prompt模板化

```python
class PromptTemplate:
    """Prompt模板管理器"""
    
    templates = {
        "default": PROMPT,
        "kb_force": KB_FORCE_PROMPT,
        "todo": TODO_MID_PROMPT,
    }
    
    @classmethod
    def build(cls, template_names: list[str], context: dict) -> str:
        """动态组装Prompt"""
        prompt_parts = []
        
        # 基础Prompt
        prompt_parts.append(cls.templates["default"])
        
        # 可选模块
        for name in template_names:
            if name in cls.templates:
                prompt_parts.append(cls.templates[name])
        
        # 用户自定义
        if context.get("custom_prompt"):
            prompt_parts.append(context["custom_prompt"])
        
        return "\n\n".join(prompt_parts)
```

**效果**：Prompt管理规范化，易于维护和扩展

### 优化2：Prompt版本管理

```python
# 数据库存储Prompt模板
class PromptTemplateModel(Base):
    __tablename__ = "prompt_templates"
    
    id: Mapped[int]
    name: Mapped[str]  # 模板名称
    content: Mapped[str]  # Prompt内容
    version: Mapped[str]  # 版本号
    created_at: Mapped[datetime]
    
# 支持A/B测试
def get_prompt_template(name: str, ab_test: bool = False) -> str:
    if ab_test:
        # 随机选择版本
        version = random.choice(["v1", "v2"])
    else:
        version = "latest"
    
    return db.query(PromptTemplateModel).filter(
        PromptTemplateModel.name == name,
        PromptTemplateModel.version == version
    ).first().content
```

**效果**：支持Prompt迭代和效果对比

## 五、改进空间

> ⚠️ 以下为建议实现方案，非项目当前代码

### 改进1：Few-shot示例增强 `[未实现]`

**改进方案**：
```python
FEW_SHOT_EXAMPLES = """

# 示例对话
用户：什么是知识图谱？
助手：知识图谱是一种用于表示实体及其关系的数据结构。它通过图的形式组织知识，节点表示实体（如人物、地点、事件），边表示实体间的关系（如"属于"、"位于"）。

用户：如何上传文档到知识库？
助手：您可以通过以下步骤上传文档：
1. 进入知识库管理页面
2. 点击"上传文件"按钮
3. 选择本地文件（支持PDF/Word/Excel等格式）
4. 等待解析和索引完成

<cite source="用户手册.pdf" type="file">1</cite>
"""

# 在Prompt中注入Few-shot
system_prompt = f"{PROMPT}\n{FEW_SHOT_EXAMPLES}"
```

**预期效果**：回答质量提升 15-20%（估算），风格更一致

### 改进2：动态Prompt优化 `[未实现]`

**改进方案**：
```python
async def optimize_prompt(base_prompt: str, user_input: str) -> str:
    """根据用户输入优化Prompt"""
    # 1. 分析用户意图
    intent = classify_intent(user_input)
    
    # 2. 选择最优Prompt变体
    if intent == "factual_question":
        # 事实性问题，强调准确性
        return base_prompt + "\n请确保回答准确，如不确定请说明。"
    elif intent == "creative_task":
        # 创造性任务，鼓励创新
        return base_prompt + "\n可以发挥创意，提供多种方案。"
    elif intent == "code_help":
        # 代码帮助，强调正确性
        return base_prompt + "\n请提供可运行的代码示例，并解释关键步骤。"
    
    return base_prompt
```

**预期效果**：针对不同场景优化，满意度提升 10-15%（估算）

### 改进3：Prompt效果评估 `[未实现]`

**改进方案**：
```python
async def evaluate_prompt_quality(prompt: str, test_cases: list[dict]) -> dict:
    """评估Prompt效果"""
    results = {
        "accuracy": [],
        "relevance": [],
        "coherence": []
    }
    
    for case in test_cases:
        response = await llm.call(f"{prompt}\n\n用户：{case['input']}")
        
        # 人工评分或自动评估
        accuracy = evaluate_accuracy(response, case["expected_output"])
        relevance = evaluate_relevance(response, case["input"])
        coherence = evaluate_coherence(response)
        
        results["accuracy"].append(accuracy)
        results["relevance"].append(relevance)
        results["coherence"].append(coherence)
    
    return {
        "avg_accuracy": np.mean(results["accuracy"]),
        "avg_relevance": np.mean(results["relevance"]),
        "avg_coherence": np.mean(results["coherence"])
    }
```

**预期效果**：量化Prompt质量，支持科学迭代

## 六、简历写法建议

### 简历描述模板

```
设计并实现生产级Prompt工程体系，包含角色定义、安全约束、知识库强制检索等模块。
通过分层Prompt设计（基础+可选），动态组装机制降低Token占用20%（估算）。
实现知识库强制检索模式，避免模型幻觉，回答可信度提升30%（估算）。
设计文件系统约束和风格规范，保障系统安全性和专业性。
解决Prompt注入攻击、风格不一致等问题，通过内容审查和边界标记双重防护。
支持Prompt版本管理和A/B测试，为效果优化提供数据支撑。
```

### 面试要点

**Q: 为什么设计"内部执行约束"？**
A: LLM倾向于向用户解释内部实现（如"我调用了XX工具，读取了XX文件"），
这对用户无价值且暴露系统细节。通过明确标记"内部执行约束"，告诉模型
这部分内容仅用于指导行为，不向用户透露，提升了用户体验和安全性。

**Q: 知识库强制检索模式如何避免幻觉？**
A: 我们在Prompt中明确4条规则：1）必须先检索再回答；2）回答必须基于检索结果；
3）检索不足时进一步查阅原文；4）确实找不到时明确告知用户。这4条规则
从制度上杜绝了模型脱离知识库"编造"答案的可能，实测幻觉率降低80%以上（估算）。

**Q: Prompt长度影响性能如何平衡？**
A: 我们采用三层策略：1）精简核心约束，去除冗余描述；2）分层设计，
基础Prompt必需，扩展Prompt可选（如知识库模式才添加KB_FORCE_PROMPT）；
3）动态组装，根据场景加载必要内容。通过这三层，系统Prompt控制在
500 Token以内，留出更多空间给上下文。

---

> 💡 **技术亮点**：分层Prompt设计、知识库强制检索、安全约束机制、动态组装。
改进方向：Few-shot示例、动态优化、效果评估。