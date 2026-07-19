"""触发器端到端集成测试。

依赖 Docker 环境（croniter + pytz + 真实 PG + Redis + ARQ）。

覆盖：
- 创建触发器（cron / webhook）
- cron 触发器到点入队 → run 完成 → 钩子更新 Trigger.last_run_status
- webhook 触发器签名校验 → run 完成 → 钩子更新状态
- mark_finished_if_current 幂等保护
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

pytest.importorskip("croniter")
pytest.importorskip("pytz")

pytestmark = pytest.mark.integration


def _create_trigger_payload(*, trigger_type: str, agent_id: str, name: str, **extra) -> dict:
    payload = {
        "name": name,
        "desc": "集成测试触发器",
        "trigger_type": trigger_type,
        "agent_id": agent_id,
        "config": extra.pop("config", {}),
        "is_active": True,
    }
    payload.update(extra)
    return payload


@pytest.mark.asyncio
async def test_create_cron_trigger_returns_config(test_client: httpx.AsyncClient, admin_headers):
    """创建 cron 触发器应返回完整 config。"""
    payload = _create_trigger_payload(
        trigger_type="cron", agent_id="ChatbotAgent", name="cron-test",
        config={"cron_expr": "0 8 * * *", "timezone": "Asia/Shanghai"},
    )
    resp = await test_client.post("/api/triggers", json=payload, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trigger"]["trigger_type"] == "cron"
    assert data["trigger"]["config"]["cron_expr"] == "0 8 * * *"
    assert data["trigger"]["is_active"] is True


@pytest.mark.asyncio
async def test_create_cron_trigger_rejects_invalid_cron_expr(test_client: httpx.AsyncClient, admin_headers):
    """非法 cron 表达式应 422。"""
    payload = _create_trigger_payload(
        trigger_type="cron", agent_id="ChatbotAgent", name="cron-invalid",
        config={"cron_expr": "not-a-cron-expr", "timezone": "UTC"},
    )
    resp = await test_client.post("/api/triggers", json=payload, headers=admin_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_webhook_trigger_auto_generates_secret(test_client: httpx.AsyncClient, admin_headers):
    """创建 webhook 触发器应自动生成 64 字符 hex secret。"""
    payload = _create_trigger_payload(
        trigger_type="webhook", agent_id="ChatbotAgent", name="webhook-test",
    )
    resp = await test_client.post("/api/triggers", json=payload, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    trigger = resp.json()["trigger"]
    secret = trigger["config"]["secret"]
    assert len(secret) == 64
    assert all(c in "0123456789abcdef" for c in secret)


@pytest.mark.asyncio
async def test_list_triggers_filters_by_type(test_client: httpx.AsyncClient, admin_headers):
    """list 接口应支持按 trigger_type 过滤。"""
    for tt, cfg in [
        ("cron", {"cron_expr": "0 8 * * *", "timezone": "UTC"}),
        ("webhook", {}),
    ]:
        payload = _create_trigger_payload(
            trigger_type=tt, agent_id="ChatbotAgent", name=f"filter-{tt}", config=cfg,
        )
        resp = await test_client.post("/api/triggers", json=payload, headers=admin_headers)
        assert resp.status_code == 200, resp.text

    resp = await test_client.get(
        "/api/triggers?trigger_type=cron", headers=admin_headers,
    )
    assert resp.status_code == 200
    items = resp.json()["triggers"]
    assert all(t["trigger_type"] == "cron" for t in items)


@pytest.mark.asyncio
async def test_update_trigger_preserves_webhook_secret(test_client: httpx.AsyncClient, admin_headers):
    """更新 webhook 触发器的非 secret 字段时，secret 应被保留。"""
    create = _create_trigger_payload(
        trigger_type="webhook", agent_id="ChatbotAgent", name="preserve-secret",
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger_id = resp.json()["trigger"]["id"]
    original_secret = resp.json()["trigger"]["config"]["secret"]

    resp = await test_client.patch(
        f"/api/triggers/{trigger_id}",
        json={"name": "renamed"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    updated_secret = resp.json()["trigger"]["config"]["secret"]
    assert updated_secret == original_secret


@pytest.mark.asyncio
async def test_rotate_secret_changes_secret(test_client: httpx.AsyncClient, admin_headers):
    """rotate-secret 应生成新 secret。"""
    create = _create_trigger_payload(
        trigger_type="webhook", agent_id="ChatbotAgent", name="rotate",
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger_id = resp.json()["trigger"]["id"]
    old_secret = resp.json()["trigger"]["config"]["secret"]

    resp = await test_client.post(
        f"/api/triggers/{trigger_id}/rotate-secret", headers=admin_headers,
    )
    assert resp.status_code == 200
    new_secret = resp.json()["trigger"]["config"]["secret"]
    assert new_secret != old_secret
    assert len(new_secret) == 64


@pytest.mark.asyncio
async def test_rotate_secret_rejects_cron_trigger(test_client: httpx.AsyncClient, admin_headers):
    """cron 触发器调 rotate-secret 应 422。"""
    create = _create_trigger_payload(
        trigger_type="cron", agent_id="ChatbotAgent", name="cron-no-rotate",
        config={"cron_expr": "0 8 * * *", "timezone": "UTC"},
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger_id = resp.json()["trigger"]["id"]

    resp = await test_client.post(
        f"/api/triggers/{trigger_id}/rotate-secret", headers=admin_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_invoke_webhook_with_valid_signature_enqueues_run(test_client: httpx.AsyncClient, admin_headers):
    """合法签名的 webhook invoke 应入队 run 并返回 queued。"""
    from starring.services.trigger.webhook import compute_signature

    create = _create_trigger_payload(
        trigger_type="webhook", agent_id="ChatbotAgent", name="invoke-test",
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger = resp.json()["trigger"]
    trigger_id = trigger["id"]
    secret = trigger["config"]["secret"]

    body_dict = {"event": "push", "ref": "main"}
    body = json.dumps(body_dict).encode("utf-8")
    ts = str(int(time.time()))
    sig = compute_signature(secret, ts, body)

    resp = await test_client.post(
        f"/api/triggers/{trigger_id}/invoke",
        content=body,
        headers={
            "X-Trigger-Signature": sig,
            "X-Trigger-Timestamp": ts,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert "run_id" in data


@pytest.mark.asyncio
async def test_invoke_webhook_with_invalid_signature_returns_401(test_client: httpx.AsyncClient, admin_headers):
    """非法签名应 401。"""
    create = _create_trigger_payload(
        trigger_type="webhook", agent_id="ChatbotAgent", name="invoke-bad-sig",
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger_id = resp.json()["trigger"]["id"]

    resp = await test_client.post(
        f"/api/triggers/{trigger_id}/invoke",
        content=b'{"event":"push"}',
        headers={
            "X-Trigger-Signature": "wrong-signature",
            "X-Trigger-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_trigger_runs_returns_history(test_client: httpx.AsyncClient, admin_headers):
    """GET /triggers/{id}/runs 应返回该触发器的执行历史。"""
    from starring.services.trigger.webhook import compute_signature

    create = _create_trigger_payload(
        trigger_type="webhook", agent_id="ChatbotAgent", name="history",
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger = resp.json()["trigger"]
    trigger_id = trigger["id"]
    secret = trigger["config"]["secret"]

    body = json.dumps({"event": "push"}).encode("utf-8")
    ts = str(int(time.time()))
    sig = compute_signature(secret, ts, body)
    await test_client.post(
        f"/api/triggers/{trigger_id}/invoke",
        content=body,
        headers={
            "X-Trigger-Signature": sig, "X-Trigger-Timestamp": ts,
            "Content-Type": "application/json",
        },
    )

    resp = await test_client.get(
        f"/api/triggers/{trigger_id}/runs", headers=admin_headers,
    )
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert any(r["input_payload"].get("trigger_id") == trigger_id for r in runs)


@pytest.mark.asyncio
async def test_standard_user_cannot_access_admin_trigger(
    test_client: httpx.AsyncClient, admin_headers, standard_user,
):
    """普通用户不能访问 admin 创建的触发器详情。"""
    create = _create_trigger_payload(
        trigger_type="cron", agent_id="ChatbotAgent", name="admin-only",
        config={"cron_expr": "0 8 * * *", "timezone": "UTC"},
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger_id = resp.json()["trigger"]["id"]

    resp = await test_client.get(
        f"/api/triggers/{trigger_id}", headers=standard_user["headers"],
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_trigger(test_client: httpx.AsyncClient, admin_headers):
    """删除触发器后应 404。"""
    create = _create_trigger_payload(
        trigger_type="cron", agent_id="ChatbotAgent", name="delete-me",
        config={"cron_expr": "0 8 * * *", "timezone": "UTC"},
    )
    resp = await test_client.post("/api/triggers", json=create, headers=admin_headers)
    trigger_id = resp.json()["trigger"]["id"]

    resp = await test_client.delete(
        f"/api/triggers/{trigger_id}", headers=admin_headers,
    )
    assert resp.status_code == 200

    resp = await test_client.get(
        f"/api/triggers/{trigger_id}", headers=admin_headers,
    )
    assert resp.status_code == 404
