from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.rss.errors import RssError
from app.rss.schemas import SubscriptionInput
from app.rss.service import RssService
from app.rss.worker import RssWorker
from app.web.errors import api_error


class EnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def rss_error_response(_request, error: RssError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": {"code": error.code, "message": error.message}},
    )


def create_rss_router(
    service: RssService,
    worker: RssWorker | None,
    require_admin_session: Callable,
    rule_service,
    account_manager_getter: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api/rss", dependencies=[Depends(require_admin_session)])

    def running_worker() -> RssWorker:
        if worker is None or not worker.status_snapshot()["worker_running"]:
            raise api_error(503, "rss_worker_not_ready", "RSS 工作器尚未运行，请等待服务恢复")
        return worker

    def available_worker() -> RssWorker:
        # 手动试发为同步直连，不依赖调度器运行；只需工作器实例存在。
        if worker is None:
            raise api_error(503, "rss_worker_not_ready", "RSS 工作器尚未就绪")
        return worker

    @router.get("/status")
    async def rss_status() -> dict:
        manager = account_manager_getter()
        telegram_ready = bool(manager and manager.is_any_connected())
        paused = await rule_service.is_paused()
        return {
            **await service.status(),
            **(worker.status_snapshot() if worker else {
                "worker_running": False, "last_worker_error": None,
            }),
            "telegram_ready": telegram_ready,
            "publishing_paused": paused or not telegram_ready,
        }

    @router.get("/subscriptions")
    async def subscriptions() -> list[dict]:
        return await service.list_subscriptions()

    @router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
    async def create_subscription(payload: SubscriptionInput) -> dict:
        return await service.create_subscription(payload)

    @router.post("/preview")
    async def preview(payload: SubscriptionInput) -> dict:
        return await service.preview(payload)

    @router.get("/subscriptions/{subscription_id}")
    async def subscription(subscription_id: int) -> dict:
        return await service.get_subscription(subscription_id)

    @router.patch("/subscriptions/{subscription_id}")
    async def update_subscription(subscription_id: int, payload: SubscriptionInput) -> dict:
        return await service.update_subscription(subscription_id, payload)

    @router.patch("/subscriptions/{subscription_id}/enabled")
    async def enable_subscription(subscription_id: int, payload: EnabledRequest) -> dict:
        return await service.set_enabled(subscription_id, payload.enabled)

    @router.delete("/subscriptions/{subscription_id}")
    async def delete_subscription(subscription_id: int) -> dict:
        return {"deleted": await service.delete_subscription(subscription_id)}

    @router.get("/subscriptions/{subscription_id}/entries")
    async def entries(
        subscription_id: int,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[dict]:
        return await service.list_entries(subscription_id, limit)

    @router.post("/subscriptions/{subscription_id}/check", status_code=202)
    async def check(subscription_id: int) -> dict:
        return await running_worker().trigger_check(subscription_id)

    @router.post("/subscriptions/{subscription_id}/retry", status_code=202)
    async def retry(subscription_id: int) -> dict:
        return await running_worker().retry_failed(subscription_id)

    @router.post("/subscriptions/{subscription_id}/test-send")
    async def test_send(subscription_id: int) -> dict:
        # 同步执行完整链路并立即返回结果；附带 TG → QQ 前置条件提醒。
        result = await available_worker().test_send_latest(subscription_id)
        warnings = list(result.get("warnings") or [])
        manager = account_manager_getter()
        if not (manager and manager.is_any_connected()):
            warnings.append("Telegram 监听账号尚未就绪，TG → QQ 转发可能不会触发。")
        if await rule_service.is_paused():
            warnings.append("全局转发已暂停，TG → QQ 转发不会执行。")
        return {**result, "warnings": warnings}

    return router
