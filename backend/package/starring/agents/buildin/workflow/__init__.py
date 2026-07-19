"""工作流引擎 backend。

P1-B 硬编排工作流引擎：基于 LangGraph StateGraph 实现确定性流程编排。
适用于合规审查、标准化报告、流水线数据处理等确定性流程场景。

与 ChatbotAgent（Orchestrator-Worker，LLM 自主路由）和 SupervisorAgent
（软编排，强制委派）形成三种 backend 范式，被 auto_discover_agents 自动发现。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md
"""
from .backend import WorkflowBackend

__all__ = ["WorkflowBackend"]
