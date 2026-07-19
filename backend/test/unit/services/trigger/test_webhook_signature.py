"""webhook 签名生成与校验单测。

覆盖：
- compute_signature 算法（HMAC-SHA256(secret, ts + "." + body)）
- verify_signature 正确签名通过
- 签名错误拒绝
- 时间戳过期拒绝（>5 分钟）
- 时间戳非数字拒绝
- 缺少 secret / signature / timestamp 拒绝
- generate_secret 长度与唯一性
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from starring.services.trigger.webhook import (
    SIGNATURE_TOLERANCE_SECONDS,
    compute_signature,
    generate_secret,
    verify_signature,
)


SECRET = "test-secret-32-bytes-0123456789abcdef"
BODY = b'{"event": "push", "ref": "main"}'
TIMESTAMP = str(int(time.time()))


def test_compute_signature_matches_hmac_sha256():
    """compute_signature 必须等于 HMAC-SHA256(secret, ts + "." + body)。"""
    expected = hmac.new(
        SECRET.encode("utf-8"),
        TIMESTAMP.encode("utf-8") + b"." + BODY,
        hashlib.sha256,
    ).hexdigest()
    assert compute_signature(SECRET, TIMESTAMP, BODY) == expected


def test_verify_signature_accepts_valid_signature():
    """正确签名 + 有效时间戳应通过。"""
    sig = compute_signature(SECRET, TIMESTAMP, BODY)
    assert verify_signature(SECRET, sig, TIMESTAMP, BODY) is True


def test_verify_signature_rejects_wrong_secret():
    """签名 secret 不匹配应拒绝。"""
    sig = compute_signature(SECRET, TIMESTAMP, BODY)
    assert verify_signature("wrong-secret", sig, TIMESTAMP, BODY) is False


def test_verify_signature_rejects_tampered_body():
    """body 被篡改后签名应不通过。"""
    sig = compute_signature(SECRET, TIMESTAMP, BODY)
    assert verify_signature(SECRET, sig, TIMESTAMP, b'{"event": "tampered"}') is False


def test_verify_signature_rejects_expired_timestamp():
    """时间戳超过 5 分钟窗口应拒绝。"""
    old_ts = str(int(time.time()) - SIGNATURE_TOLERANCE_SECONDS - 60)
    sig = compute_signature(SECRET, old_ts, BODY)
    assert verify_signature(SECRET, sig, old_ts, BODY) is False


def test_verify_signature_rejects_future_timestamp():
    """未来时间戳超过窗口也应拒绝（防止预生成签名）。"""
    future_ts = str(int(time.time()) + SIGNATURE_TOLERANCE_SECONDS + 60)
    sig = compute_signature(SECRET, future_ts, BODY)
    assert verify_signature(SECRET, sig, future_ts, BODY) is False


def test_verify_signature_rejects_non_numeric_timestamp():
    """时间戳非数字应拒绝。"""
    sig = compute_signature(SECRET, "not-a-number", BODY)
    assert verify_signature(SECRET, sig, "not-a-number", BODY) is False


@pytest.mark.parametrize("missing", ["secret", "signature", "timestamp"])
def test_verify_signature_rejects_missing_inputs(missing):
    """secret / signature / timestamp 任一为空应拒绝。"""
    args = {"secret": SECRET, "signature": compute_signature(SECRET, TIMESTAMP, BODY),
            "timestamp": TIMESTAMP, "body": BODY}
    args[missing] = ""
    assert verify_signature(**args) is False


def test_generate_secret_returns_64_hex_chars():
    """generate_secret 应返回 32 字节 = 64 hex 字符。"""
    secret = generate_secret()
    assert len(secret) == 64
    assert all(c in "0123456789abcdef" for c in secret)


def test_generate_secret_is_unique():
    """连续调用 generate_secret 应返回不同值。"""
    s1 = generate_secret()
    s2 = generate_secret()
    assert s1 != s2
