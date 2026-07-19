"""Webhook 触发器签名生成与校验。

签名算法：HMAC-SHA256(secret, timestamp + "." + body)
防重放：时间戳超过 5 分钟窗口拒绝。
"""

from __future__ import annotations

import hashlib
import hmac
import time

# 防重放窗口（秒）：超过此窗口的时间戳拒绝
SIGNATURE_TOLERANCE_SECONDS = 5 * 60


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """计算 HMAC-SHA256(secret, timestamp + "." + body) 的 hex 摘要。

    Args:
        secret: 触发器配置中的 webhook secret
        timestamp: Unix 时间戳字符串（与请求头 X-Trigger-Timestamp 一致）
        body: 请求体原始字节

    Returns:
        hex 摘要字符串
    """
    payload = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_signature(secret: str, signature: str, timestamp: str, body: bytes) -> bool:
    """校验签名 + 时间戳防重放。

    Args:
        secret: 触发器配置中的 webhook secret
        signature: 请求头 X-Trigger-Signature 的 hex 摘要
        timestamp: 请求头 X-Trigger-Timestamp 的 Unix 时间戳字符串
        body: 请求体原始字节

    Returns:
        True 表示校验通过；False 表示签名错误或时间戳过期
    """
    if not secret or not signature or not timestamp:
        return False

    # 防重放：时间戳必须是数字且在 5 分钟窗口内
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False

    expected = compute_signature(secret, timestamp, body)
    # 使用常量时间比较而非 == ：防止攻击者通过响应耗时差异逐字节爆破签名（timing attack）
    return hmac.compare_digest(expected, signature)


def generate_secret() -> str:
    """生成 32 字节随机 hex secret。"""
    import secrets

    return secrets.token_hex(32)  # 32 字节 = 64 hex 字符
