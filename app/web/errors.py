from __future__ import annotations

from fastapi import HTTPException, status

from app.web.auth import MiniAppAuthError

_ERROR_STATUS = {
    "missing_init_data": status.HTTP_401_UNAUTHORIZED,
    "invalid_init_data": status.HTTP_401_UNAUTHORIZED,
    "init_data_expired": status.HTTP_401_UNAUTHORIZED,
    "mini_app_not_configured": status.HTTP_503_SERVICE_UNAVAILABLE,
    "admin_required": status.HTTP_403_FORBIDDEN,
}


def auth_http_exception(exc: MiniAppAuthError) -> HTTPException:
    return HTTPException(
        status_code=_ERROR_STATUS.get(exc.code, status.HTTP_401_UNAUTHORIZED),
        detail={"code": exc.code, "message": exc.message},
    )


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
