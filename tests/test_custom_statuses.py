"""list_custom_statuses: the query string, the projection and the category check."""
import io
import json as _json
import urllib.parse
from contextlib import contextmanager

import pytest

from zendesk_mcp_server.zendesk_client import ZendeskClient, _TICKET_SUMMARY_FIELDS

_STATUS = {
    "id": 101, "status_category": "pending", "agent_label": "Waiting on vendor",
    "end_user_label": "Awaiting reply", "description": "Vendor has the ball", "active": True,
    "default": False, "raw_agent_label": "Waiting on vendor", "url": "https://x/api/v2/custom_statuses/101.json",
}


def _client(monkeypatch, calls):
    @contextmanager
    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        yield io.BytesIO(_json.dumps({"custom_statuses": [_STATUS]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return ZendeskClient(subdomain="testsub", email="a@b.com", token="tok")


def test_lists_and_projects(monkeypatch):
    calls = []
    out = _client(monkeypatch, calls).list_custom_statuses()
    assert calls[0].split("?")[0] == "https://testsub.zendesk.com/api/v2/custom_statuses.json"
    assert out == {"count": 1, "custom_statuses": [{
        "id": 101, "status_category": "pending", "agent_label": "Waiting on vendor",
        "end_user_label": "Awaiting reply", "description": "Vendor has the ball", "active": True, "default": False,
    }]}


def test_filters_go_on_the_query_string(monkeypatch):
    calls = []
    _client(monkeypatch, calls).list_custom_statuses(status_categories=["open", "hold"], active=False)
    q = urllib.parse.parse_qs(calls[0].split("?", 1)[1])
    assert q == {"status_categories": ["open hold"], "active": ["false"]}


def test_unknown_category_raises_before_any_call(monkeypatch):
    calls = []
    with pytest.raises(ValueError, match="unknown status_categories"):
        _client(monkeypatch, calls).list_custom_statuses(status_categories=["closed"])
    assert calls == []


def test_ticket_summary_carries_custom_status_id():
    assert "custom_status_id" in _TICKET_SUMMARY_FIELDS
