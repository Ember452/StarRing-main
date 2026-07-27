"""DeepReport 知识库报告流水线模块。

导出 DeepReportAgent 供 AgentManager.auto_discover_agents 自动发现注册。
"""

from starring.agents.buildin.deepreport.backend import DeepReportAgent

__all__ = ["DeepReportAgent"]
