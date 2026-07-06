from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.qq_official.client import QQTargetInfo
from app.rules.keywords import keywords_from_text_include_regex
from app.rules.models import TelegramForwardMessage, TelegramLink
from app.rules.service import ForwardRuleService
from app.storage.models import ForwardStatus, QQTargetType
from app.telegram_user.client import TelegramUserListener
from app.web.auth import MiniAppAuthError, MiniAppSession, validate_init_data
from app.web.errors import api_error, auth_http_exception
from app.web.schemas import (
    DialogResponse,
    ForwardLogResponse,
    MeResponse,
    MiniAppUserResponse,
    OptionsResponse,
    PauseRequest,
    PauseResponse,
    QQTargetResponse,
    RuleCreateRequest,
    RuleCreateResponse,
    RuleDuplicateRequest,
    RuleEnabledRequest,
    RulePreviewRequest,
    RulePreviewResponse,
    RuleResponse,
    RuleUpdateRequest,
    StatusResponse,
)

STATIC_DIR = Path(__file__).with_name("static")
TEMPLATE_VARIABLES = [
    "message_id",
    "chat_id",
    "chat_title",
    "chat_type",
    "sender_id",
    "sender_username",
    "sender_name",
    "sender_is_bot",
    "text",
    "media_type",
    "media_path",
    "media_note",
    "links_note",
    "plain_links_note",
    "raw_url",
]
MEDIA_TYPES = ["text", "photo", "video", "voice", "audio", "document", "animation", "sticker"]


def create_mini_app(
    *,
    settings: Settings,
    service: ForwardRuleService,
    telegram_listener_getter: Callable[[], TelegramUserListener | None],
    qq_status_getter: Callable[[], str],
    qq_targets_getter: Callable[[], list[QQTargetInfo]],
    queue_size_getter: Callable[[], int],
) -> FastAPI:
    app = FastAPI(title="TGQQ Forwarder Mini App", docs_url=None, redoc_url=None)
    if settings.mini_app_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.mini_app_allowed_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["X-Telegram-Init-Data", "Content-Type"],
        )

    async def require_admin_session(
        init_data: Annotated[str | None, Header(alias="X-Telegram-Init-Data")] = None,
    ) -> MiniAppSession:
        try:
            return validate_init_data(init_data or "", settings)
        except MiniAppAuthError as exc:
            raise auth_http_exception(exc) from exc

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/me", response_model=MeResponse)
    async def me(session: Annotated[MiniAppSession, Depends(require_admin_session)]) -> MeResponse:
        return MeResponse(
            user=MiniAppUserResponse(
                id=session.user.id,
                first_name=session.user.first_name,
                last_name=session.user.last_name,
                username=session.user.username,
                display_name=session.user.display_name,
            ),
            auth_date=session.auth_date,
        )

    @app.get("/api/status", response_model=StatusResponse)
    async def app_status(
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> StatusResponse:
        _ = session
        listener = telegram_listener_getter()
        total_rules, enabled_rules, total_logs = await service.counts()
        return StatusResponse(
            telegram_connected=bool(listener and listener.is_connected),
            qq_status=qq_status_getter(),
            forwarding_paused=await service.is_paused(),
            total_rules=total_rules,
            enabled_rules=enabled_rules,
            total_logs=total_logs,
            queue_size=queue_size_getter(),
            mini_app_enabled=settings.mini_app_enabled,
            mini_app_public_url=settings.mini_app_public_url,
        )

    @app.patch("/api/settings/paused", response_model=PauseResponse)
    async def set_paused(
        payload: PauseRequest,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> PauseResponse:
        _ = session
        await service.set_paused(payload.paused)
        return PauseResponse(paused=payload.paused)

    @app.get("/api/dialogs", response_model=list[DialogResponse])
    async def dialogs(
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
        query: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 80,
    ) -> list[DialogResponse]:
        _ = session
        listener = telegram_listener_getter()
        if listener is None:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "telegram_listener_not_ready",
                "Telegram 用户监听器尚未就绪",
            )
        items = await listener.dialogs.list_dialogs(limit=limit, query=query)
        return [DialogResponse(id=item.id, name=item.name, type=item.type) for item in items]

    @app.get("/api/qq-targets", response_model=list[QQTargetResponse])
    async def qq_targets(
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> list[QQTargetResponse]:
        _ = session
        return [QQTargetResponse.from_target(target) for target in qq_targets_getter()]

    @app.get("/api/rules", response_model=list[RuleResponse])
    async def rules(
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
        enabled_only: bool = False,
        limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    ) -> list[RuleResponse]:
        _ = session
        rows = await service.list_rules(enabled_only=enabled_only, limit=limit)
        return [RuleResponse.from_rule(rule) for rule in rows]

    @app.post("/api/rules", response_model=RuleCreateResponse, status_code=status.HTTP_201_CREATED)
    async def create_rule(
        payload: RuleCreateRequest,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> RuleCreateResponse:
        _ = session
        result = await service.create_or_merge_rule(payload.to_rule())
        return RuleCreateResponse(
            rule=RuleResponse.from_rule(result.rule),
            created=result.created,
            updated=result.updated,
            keywords=result.keywords,
            removed_duplicate_count=result.removed_duplicate_count,
        )

    @app.post("/api/rules/preview", response_model=RulePreviewResponse)
    async def preview_rule(
        payload: RulePreviewRequest,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> RulePreviewResponse:
        _ = session
        rule = payload.rule.to_rule()
        warnings: list[str] = []
        if not rule.enabled:
            rule.enabled = True
            warnings.append("规则当前设置为禁用；预览按启用状态计算匹配结果。")
        try:
            _compile_rule_regexes(rule.text_include_regex, rule.text_exclude_regex)
        except re.error as exc:
            warnings.append(f"正则表达式无效：{exc}")
        message = TelegramForwardMessage(
            message_id=1,
            chat_id=payload.message.chat_id,
            chat_title=payload.message.chat_title,
            chat_type=payload.message.chat_type,
            sender_id=payload.message.sender_id,
            sender_username=payload.message.sender_username,
            sender_display_name=payload.message.sender_display_name,
            sender_is_bot=payload.message.sender_is_bot,
            text=payload.message.text,
            media_type=payload.message.media_type,
            media_path=None,
            date=None,
            links=[
                TelegramLink(
                    text=str(item.get("text") or item.get("url") or ""),
                    url=str(item.get("url") or ""),
                    source=str(item.get("source") or "manual"),
                )
                for item in payload.message.links
                if item.get("url")
            ],
        )
        matches = service.matcher.matches(rule, message)
        rendered = service.formatter.format(rule, message)
        return RulePreviewResponse(
            matches=matches,
            rendered_text=rendered,
            detected_keywords=keywords_from_text_include_regex(rule.text_include_regex),
            warnings=warnings,
        )

    @app.get("/api/rules/{rule_id}", response_model=RuleResponse)
    async def get_rule(
        rule_id: int,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> RuleResponse:
        _ = session
        rule = await service.get_rule(rule_id)
        if rule is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "rule_not_found", "规则不存在")
        return RuleResponse.from_rule(rule)

    @app.patch("/api/rules/{rule_id}", response_model=RuleResponse)
    async def update_rule(
        rule_id: int,
        payload: RuleUpdateRequest,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> RuleResponse:
        _ = session
        rule = await service.update_rule(rule_id, payload.to_rule_values())
        if rule is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "rule_not_found", "规则不存在")
        return RuleResponse.from_rule(rule)

    @app.post("/api/rules/{rule_id}/duplicate", response_model=RuleResponse)
    async def duplicate_rule(
        rule_id: int,
        payload: RuleDuplicateRequest,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> RuleResponse:
        _ = session
        rule = await service.duplicate_rule(rule_id, name=payload.name, enabled=payload.enabled)
        if rule is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "rule_not_found", "规则不存在")
        return RuleResponse.from_rule(rule)

    @app.patch("/api/rules/{rule_id}/enabled", response_model=RuleResponse)
    async def set_rule_enabled(
        rule_id: int,
        payload: RuleEnabledRequest,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> RuleResponse:
        _ = session
        changed = await service.set_rule_enabled(rule_id, payload.enabled)
        if not changed:
            raise api_error(status.HTTP_404_NOT_FOUND, "rule_not_found", "规则不存在")
        rule = await service.get_rule(rule_id)
        if rule is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "rule_not_found", "规则不存在")
        return RuleResponse.from_rule(rule)

    @app.delete("/api/rules/{rule_id}")
    async def delete_rule(
        rule_id: int,
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> dict[str, bool]:
        _ = session
        deleted = await service.delete_rule(rule_id)
        if not deleted:
            raise api_error(status.HTTP_404_NOT_FOUND, "rule_not_found", "规则不存在")
        return {"deleted": True}

    @app.get("/api/logs", response_model=list[ForwardLogResponse])
    async def logs(
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
        status_filter: str | None = Query(default=None, alias="status"),
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[ForwardLogResponse]:
        _ = session
        if status_filter == "":
            status_filter = None
        valid_statuses = {item.value for item in ForwardStatus}
        if status_filter is not None and status_filter not in valid_statuses:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_status",
                "日志状态无效",
            )
        rows = await service.recent_logs(limit=limit, status=status_filter)
        return [ForwardLogResponse.from_log(row) for row in rows]

    @app.get("/api/options", response_model=OptionsResponse)
    async def options(
        session: Annotated[MiniAppSession, Depends(require_admin_session)],
    ) -> OptionsResponse:
        _ = session
        return OptionsResponse(
            qq_target_types=[item.value for item in QQTargetType],
            chat_types=["private", "group", "channel", "unknown"],
            media_types=MEDIA_TYPES,
            template_variables=TEMPLATE_VARIABLES,
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _compile_rule_regexes(include_regex: str | None, exclude_regex: str | None) -> None:
    if include_regex:
        re.compile(include_regex)
    if exclude_regex:
        re.compile(exclude_regex)
