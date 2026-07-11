from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.rules.keywords import keywords_to_text_include_regex
from app.rules.templates import (
    DEFAULT_MESSAGE_TEMPLATE,
    OLD_DEFAULT_MESSAGE_TEMPLATE,
    PREVIOUS_DEFAULT_MESSAGE_TEMPLATE,
)
from app.storage.models import ForwardRule
from app.web.schemas import RuleCreateRequest, RuleResponse


def test_rule_request_converts_keywords_to_tagged_regex() -> None:
    payload = RuleCreateRequest(
        name="AI",
        match_mode="keywords",
        keywords="AI Python；机器人 量化",
        qq_target_type="group",
        qq_target_id="target",
        message_template="{text}",
    )

    values = payload.to_rule_values()

    assert payload.keywords == ["AI", "Python", "机器人", "量化"]
    assert values["text_include_regex"].startswith("(?#tgqq-keywords:")


def test_rule_request_supports_raw_regex_mode() -> None:
    payload = RuleCreateRequest(
        name="regex",
        match_mode="regex",
        text_include_regex="AI|Python",
        qq_target_type="c2c",
        qq_target_id="openid",
        message_template="{text}",
    )

    assert payload.to_rule_values()["text_include_regex"] == "AI|Python"


def test_rule_response_decodes_keyword_rule() -> None:
    response = RuleResponse.from_rule(
        ForwardRule(
            id=1,
            name="r1",
            text_include_regex=keywords_to_text_include_regex(["AI"]),
            qq_target_type="group",
            qq_target_id="target",
            message_template="{text}",
        )
    )

    assert response.match_mode == "keywords"
    assert response.keywords == ["AI"]


def test_rule_response_displays_upgraded_old_default_template() -> None:
    response = RuleResponse.from_rule(
        ForwardRule(
            id=1,
            name="r1",
            qq_target_type="group",
            qq_target_id="target",
            message_template=OLD_DEFAULT_MESSAGE_TEMPLATE,
        )
    )

    assert response.message_template == DEFAULT_MESSAGE_TEMPLATE


def test_rule_response_displays_upgraded_previous_markdown_default_template() -> None:
    response = RuleResponse.from_rule(
        ForwardRule(
            id=1,
            name="r1",
            qq_target_type="group",
            qq_target_id="target",
            message_template=PREVIOUS_DEFAULT_MESSAGE_TEMPLATE,
        )
    )

    assert response.message_template == DEFAULT_MESSAGE_TEMPLATE
    assert "{account_id}" in response.message_template


def test_rule_response_keeps_custom_template() -> None:
    response = RuleResponse.from_rule(
        ForwardRule(
            id=1,
            name="r1",
            qq_target_type="group",
            qq_target_id="target",
            message_template="{sender_name}: {text}",
        )
    )

    assert response.message_template == "{sender_name}: {text}"


def test_rule_request_rejects_invalid_qq_target_type() -> None:
    with pytest.raises(ValidationError):
        RuleCreateRequest(
            name="bad",
            qq_target_type="bad",
            qq_target_id="target",
            message_template="{text}",
        )
