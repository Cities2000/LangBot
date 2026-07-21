"""Tests for GsbAgent Runner."""

from __future__ import annotations

import pytest

from langbot.pkg.provider.runners.gsbagent import GsbAgentRunner


class FakeApp:
    pass


@pytest.fixture
def runner():
    r = GsbAgentRunner.__new__(GsbAgentRunner)
    return r


def test_request_id_prefers_wecom_message_id(runner):
    result = runner._resolve_request_id({"message_id": "msg-1", "req_id": "req-1"})
    assert result == "msg-1"


def test_request_id_uses_req_id_when_message_id_missing(runner):
    result = runner._resolve_request_id({"req_id": "req-1"})
    assert result == "req-1"


def test_request_id_does_not_hash_message_content(runner):
    first = runner._resolve_request_id({})
    second = runner._resolve_request_id({})
    assert first != second


def test_request_id_uses_msgid_when_message_id_missing(runner):
    result = runner._resolve_request_id({"msgid": "msgid-1"})
    assert result == "msgid-1"


def test_inline_params_cannot_override_protected_request_metadata(runner):
    params = {
        "request_id": "forged",
        "bot_uuid": "forged-bot",
        "pipeline_uuid": "forged-pipeline",
        "sender_id": "13900000000",
        "period": "本月",  # non-protected, should pass through
    }
    filtered = runner._filter_inline_params(params)
    assert filtered == {"period": "本月"}


def test_inline_params_cannot_override_conversation_id(runner):
    params = {"conversation_id": "hijacked", "extra": "bad", "launcher_type": "fake"}
    filtered = runner._filter_inline_params(params)
    assert filtered == {}


def test_inline_params_passes_through_non_protected(runner):
    params = {"period": "本月", "region": "东北", "limit": "10"}
    filtered = runner._filter_inline_params(params)
    assert filtered == params
