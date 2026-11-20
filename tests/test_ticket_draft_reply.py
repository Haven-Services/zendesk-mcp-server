"""set_ticket_draft_reply: the field lookup, the payload and the single write."""
import io
import json as _json
from contextlib import contextmanager

import pytest

from zendesk_mcp_server.zendesk_client import (
    ZendeskClient,
    _build_draft_reply_payload,
    _find_draft_reply_field,
)

_FIELDS = [
    {"id": 1, "title": "AI draft reply", "type": "textarea", "active": True},
    {"id": 2, "title": "AI Agent Notes", "type": "textarea", "active": True},
]


class TestFindDraftReplyField:
    def test_finds_the_one_active_textarea(self):
        assert _find_draft_reply_field(_FIELDS, "AI draft reply") == 1

    @pytest.mark.parametrize("fields", [
        [],
        [{"id": 1, "title": "AI draft reply", "type": "textarea", "active": False}],
        [{"id": 1, "title": "AI draft reply", "type": "text", "active": True}],
        _FIELDS + [{"id": 3, "title": "AI draft reply", "type": "textarea", "active": True}],
    ])
    def test_missing_or_ambiguous_field_raises(self, fields):
        with pytest.raises(ValueError, match="expected exactly one"):
            _find_draft_reply_field(fields, "AI draft reply")


class TestBuildDraftReplyPayload:
    def test_touches_only_the_custom_field(self):
        assert _build_draft_reply_payload(1, "Hi") == {
            "ticket": {"custom_fields": [{"id": 1, "value": "Hi"}]}
        }

    def test_empty_string_clears(self):
        assert _build_draft_reply_payload(1, "")["ticket"]["custom_fields"][0]["value"] == ""

    def test_rejects_non_string_and_oversize(self):
        with pytest.raises(TypeError):
            _build_draft_reply_payload(1, None)
        with pytest.raises(ValueError, match="limit"):
            _build_draft_reply_payload(1, "x" * 8001)


def test_set_ticket_draft_reply_makes_one_write_without_a_comment(monkeypatch):
    calls = []

    @contextmanager
    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url.split("?")[0], _json.loads(req.data) if req.data else None))
        if req.get_method() == "GET":
            yield io.BytesIO(_json.dumps({"ticket_fields": _FIELDS, "next_page": None}).encode())
        else:
            yield io.BytesIO(b'{"ticket": {"id": 7, "status": "open"}}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = ZendeskClient(subdomain="testsub", email="a@b.com", token="tok").set_ticket_draft_reply(7, "Hi\nthere")
    assert out == {"ticket_id": 7, "status": "open", "field_id": 1, "draft_chars": 8}
    writes = [c for c in calls if c[0] != "GET"]
    assert writes == [(
        "PUT", "https://testsub.zendesk.com/api/v2/tickets/7.json",
        {"ticket": {"custom_fields": [{"id": 1, "value": "Hi\nthere"}]}},
    )]
