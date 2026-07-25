"""触发器抽象基类。

参考 MaxKB apps/trigger/handler/base_trigger.py，但简化：
- 不引入 TriggerTask 中间层（starRing 一个触发器 → 一个 agent run）
- 触发器实例无状态，配置全部从 Trigger 模型读取
- 触发器职责：在到点 / 被调用时，调 trigger_service.execute_trigger 创建 run
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starring.storage.postgres.models_business import Trigger


class BaseTrigger(ABC):
    """触发器抽象基类。

    每种触发器类型（cron / webhook）实现此接口。
    """

    trigger_type: str

    @abstractmethod
    async def execute(self, trigger: Trigger, payload: dict | None = None) -> dict:
        """执行触发器，返回 {"status": "queued", "run_id": ...}。"""
