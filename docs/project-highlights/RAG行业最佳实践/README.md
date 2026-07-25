# RAG 行业最佳实践参考

> 本目录系统梳理了 RAG（Retrieval-Augmented Generation）领域的行业最佳实践，与 StarRing 当前方案进行对比分析，找出差距与优化方向。

## 目的

StarRing 作为基于大模型的智能知识库与知识图谱智能体开发平台，在文档切割、向量检索、知识图谱、Agent 推理等方面已有扎实基础。但随着 RAG 技术的快速演进，Anthropic、Microsoft、Jina AI 等公司和研究机构持续提出新方案，我们需要跟踪前沿并持续优化。

本目录的每个文档遵循统一结构：

1. **行业最佳实践介绍** —— 说明方案的提出者、核心思路和关键数据
2. **技术原理深度解析** —— 用 Mermaid 图表 + 代码示例说明工作原理
3. **对比 StarRing 现状** —— 分析当前方案的差距
4. **优化建议** —— 提出可落地的改进方向

## 文档索引

| 序号 | 文档 | 核心内容 |
|------|------|----------|
| 01 | [文档切割最佳实践](./01-文档切割最佳实践.md) | Late Chunking、Contextual Retrieval、Agentic Chunking、Sentence Window、多粒度索引 |
| 02 | [RAG 入库流程最佳实践](./02-RAG入库流程最佳实践.md) | Contextual Retrieval 入库、RAPTOR 递归摘要树、Metadata 富化 |
| 03 | [知识图谱增强 RAG 最佳实践](./03-知识图谱增强RAG最佳实践.md) | Microsoft GraphRAG、Global/Local Search、实体归一化 |
| 04 | [混合检索与排序最佳实践](./04-混合检索与排序最佳实践.md) | Contextual BM25、RRF 融合、Re-Ranker 选型、ColBERT、Azure Databricks 7 步优化 |
| 05 | [查询优化最佳实践](./05-查询优化最佳实践.md) | Query Rewriting、Query Decomposition、HyDE、Multi-hop Retrieval |
| 06 | [高级 RAG 模式最佳实践](./06-高级RAG模式最佳实践.md) | Self-RAG、CRAG、Agentic RAG、Adaptive RAG、ReflectiveRAG |
| 07 | [RAG 评估体系最佳实践](./07-RAG评估体系最佳实践.md) | RAGAS 框架、检索指标、生成指标、评估流水线 |
| 08 | [对比总结与优化路线图](./08-对比总结与优化路线图.md) | 综合对比表、优先级排序、实施路线图 |

## 使用方式

- **技术负责人**：可直接阅读 [08-对比总结与优化路线图](./08-对比总结与优化路线图.md)，快速了解优化全景
- **后端工程师**：按序号阅读各文档，深入了解每项技术的实现原理
- **产品经理**：阅读 01-04 了解 RAG 技术前沿，阅读 08 了解优化优先级

## 关键数据一览

| 优化方向 | 当前状态 | 行业最佳 | 预估收益 |
|----------|----------|----------|----------|
| 文档切割 | 6 种预设策略 | Late Chunking + Contextual Retrieval | 检索失败率 ↓67% |
| 入库流程 | 解析→切割→Embed | Contextual Retrieval + RAPTOR | 检索精度 ↑9-20% |
| 知识图谱 | LLM 抽取 + PPR 检索 | GraphRAG 社区检测 + 摘要 | 复杂问题 Recall ↑75% |
| 混合检索 | 向量 + BM25 + 图 | + Contextual BM25 + Re-Ranker | 检索精度 ↑20-30% |
| 查询优化 | Agent 多步推理 | Query Rewriting + HyDE | 召回率 ↑10-15% |
| RAG 模式 | LangGraph Agent | CRAG + Self-RAG | 劣质回答 ↓30% |
| 评估体系 | 基础测试框架 | RAGAS 自动化评估 | 可量化迭代 |
