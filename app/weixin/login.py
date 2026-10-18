"""个人微信扫码登录 CLI。

    python -m app.weixin.login
    python -m app.weixin.login --account default --timeout 300

登录态写入 ``WEIXIN_STATE_DIR/<account>/credentials.json``（0600），
之后 ``python -m app.main`` 会直接复用，无需再次扫码。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from app.config import get_settings
from app.logging_config import configure_logging
from app.weixin.service import WeixinService

logger = logging.getLogger(__name__)


async def _noop_on_message(_message) -> None:  # pragma: no cover - CLI 只关心登录
    return None


async def run_login(account_id: str, timeout_seconds: float) -> int:
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level)
    if not settings.weixin_enabled:
        print("WEIXIN_ENABLED 为 false，请先在 .env 中开启后再登录。", file=sys.stderr)
        return 2

    service = WeixinService(settings, _noop_on_message, account_id=account_id)
    await service.start()
    printed_qrcode: str | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if service.logged_in:
                credentials = service.state.credentials
                print("微信登录成功。")
                if credentials:
                    print(f"  ilink_bot_id: {credentials.account_id}")
                    print(f"  ilink_user_id: {credentials.user_id}")
                print(f"  凭证文件: {service.store.path}")
                return 0

            login = service.login
            if login is not None and login.qrcode != printed_qrcode:
                printed_qrcode = login.qrcode
                print("\n请用微信扫描下面的二维码（手机微信 → 扫一扫）：")
                if login.image_path:
                    print(f"  二维码图片: {login.image_path}")
                if login.image_url:
                    print(f"  二维码链接: {login.image_url}")
                if not login.image_path and not login.image_url:
                    print(f"  二维码 token: {login.qrcode}")
            elif login is not None and login.status == "scaned":
                print("已扫码，请在手机上点击确认…")

            await asyncio.sleep(1.0)
    finally:
        await service.stop()

    print("等待扫码超时，未完成登录。", file=sys.stderr)
    return 1


def cli() -> None:
    parser = argparse.ArgumentParser(description="个人微信扫码登录")
    parser.add_argument("--account", default=None, help="微信账号 id，默认取 WEIXIN_ACCOUNT_ID")
    parser.add_argument("--timeout", type=float, default=300.0, help="等待扫码的秒数")
    args = parser.parse_args()
    account_id = args.account or get_settings().weixin_account_id
    raise SystemExit(asyncio.run(run_login(account_id, args.timeout)))


if __name__ == "__main__":
    cli()
