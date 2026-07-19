"""触发器系统包：cron / webhook 两种触发器类型。"""

from starring.services.trigger.base import BaseTrigger
from starring.services.trigger.service import (
    execute_trigger,
    execute_webhook_trigger,
)

__all__ = [
    "BaseTrigger",
    "execute_trigger",
    "execute_webhook_trigger",
]
