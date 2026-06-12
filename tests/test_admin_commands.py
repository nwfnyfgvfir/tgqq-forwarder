from __future__ import annotations

from app.telegram_admin.commands import parse_add_rule_args


def test_parse_add_rule_args_allows_spaces_in_name() -> None:
    parsed = parse_add_rule_args(
        [
            "LINUX",
            "DO",
            "Channel",
            "-1002035446470",
            "*",
            "c2c",
            "3BAABA13021BB09F7298EC3EBC7185AF",
            "gpt,注册机,公益",
        ]
    )

    assert parsed is not None
    assert parsed.name == "LINUX DO Channel"
    assert parsed.source_chat_id == -1002035446470
    assert parsed.source_sender_id is None
    assert parsed.target_type == "c2c"
    assert parsed.target_id == "3BAABA13021BB09F7298EC3EBC7185AF"
    assert parsed.keywords == ["gpt", "注册机", "公益"]


def test_parse_add_rule_args_rejects_invalid_shape() -> None:
    assert parse_add_rule_args(["LINUX", "DO", "Channel", "not-int", "*", "c2c", "target"]) is None
