from __future__ import annotations

from dataclasses import dataclass

from telethon import TelegramClient


@dataclass(slots=True)
class TelegramDialogInfo:
    id: int
    name: str
    type: str


class DialogCache:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def list_dialogs(
        self,
        *,
        limit: int = 50,
        query: str | None = None,
    ) -> list[TelegramDialogInfo]:
        items: list[TelegramDialogInfo] = []
        lowered = query.lower() if query else None
        async for dialog in self.client.iter_dialogs(limit=limit):
            name = dialog.name or ""
            if lowered and lowered not in name.lower() and lowered not in str(dialog.id):
                continue
            if dialog.is_channel:
                dtype = "channel"
            elif dialog.is_group:
                dtype = "group"
            elif dialog.is_user:
                dtype = "private"
            else:
                dtype = "unknown"
            items.append(TelegramDialogInfo(id=dialog.id, name=name, type=dtype))
        return items
