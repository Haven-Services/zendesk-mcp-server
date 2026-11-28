"""set_ticket_draft_reply: the field lookup, the payload and the single write."""
import io
import json as _json
from contextlib import contextmanager

import pytest

from zendesk_mcp_server.zendesk_client import (
    ZendeskClient,
    _build_draft_reply_payload,
    _build_triage_notes_value,
    _find_draft_reply_field,
)

_FIELDS = [
    {
        "id": 1, "title": "AI draft reply", "type": "textarea", "active": True,
        "visible_in_portal": False, "editable_in_portal": False,
    },
    {
        "id": 2, "title": "AI Agent Notes", "type": "textarea", "active": True,
        "visible_in_portal": False, "editable_in_portal": False,
    },
]


class TestFindDraftReplyField:
    def test_finds_the_one_active_textarea(self):
        assert _find_draft_reply_field(_FIELDS, "AI draft reply") == 1

    @pytest.mark.parametrize("fields", [
        [],
        [{**_FIELDS[0], "active": False}],
        [{**_FIELDS[0], "type": "text"}],
        [{**_FIELDS[0], "visible_in_portal": True}],
        [{**_FIELDS[0], "editable_in_portal": True}],
        [_FIELDS[0], {**_FIELDS[0], "id": 3}],
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


class TestBuildTriageNotesValue:
    def test_builds_one_json_object_with_known_keys(self):
        value = _build_triage_notes_value("request", " Check the port. ", "wiki/printers/index.md", 11, "2026-11-20")
        assert _json.loads(value) == {
            "kind": "request", "runbook": "wiki/printers/index.md", "repeat": 11,
            "generated": "2026-11-20", "note": "Check the port.",
        }

    def test_optional_keys_are_omitted(self):
        assert _json.loads(_build_triage_notes_value("noise", "x")) == {"kind": "noise", "note": "x"}

    @pytest.mark.parametrize("kwargs", [
        dict(kind="other", note="x"),
        dict(kind="request", note="  "),
        dict(kind="request", note="x" * 4001),
        dict(kind="request", note="x", runbook="r" * 201),
        dict(kind="request", note="x", repeat=-1),
        dict(kind="request", note="x", repeat=True),
        dict(kind="request", note="x", generated="yesterday"),
    ])
    def test_rejects_invalid_values(self, kwargs):
        with pytest.raises(ValueError):
            _build_triage_notes_value(**kwargs)


def test_set_ticket_triage_notes_writes_only_the_notes_field(monkeypatch):
    calls = []
    fields = _FIELDS + [{
        "id": 9, "title": "AI triage notes", "type": "textarea", "active": True,
        "visible_in_portal": False, "editable_in_portal": False,
    }]

    @contextmanager
    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url.split("?")[0], _json.loads(req.data) if req.data else None))
        if req.get_method() == "GET":
            yield io.BytesIO(_json.dumps({"ticket_fields": fields, "next_page": None}).encode())
        else:
            yield io.BytesIO(b'{"ticket": {"id": 7, "status": "open"}}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = ZendeskClient(subdomain="testsub", email="a@b.com", token="tok").set_ticket_triage_notes(7, "incident", "First checks")
    assert out["field_id"] == 9 and out["status"] == "open"
    writes = [c for c in calls if c[0] != "GET"]
    assert len(writes) == 1
    body = writes[0][2]
    assert list(body) == ["ticket"] and list(body["ticket"]) == ["custom_fields"]
    assert body["ticket"]["custom_fields"][0]["id"] == 9
    assert _json.loads(body["ticket"]["custom_fields"][0]["value"]) == {"kind": "incident", "note": "First checks"}
