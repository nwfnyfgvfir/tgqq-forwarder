from __future__ import annotations


class RssError(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(message)
