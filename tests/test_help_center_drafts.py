"""Help Center article drafts: the payload builders and the draft-only guards.

Network-free: urllib.request.urlopen is replaced, as in test_pure_helpers.
"""
import io
import json as _json
from contextlib import contextmanager

import pytest

from zendesk_mcp_server.zendesk_client import (
    ZendeskClient,
    _build_article_draft_payload,
    _build_article_translation_payload,
)

_VALID = dict(title="T", body="<p>b</p>", user_segment_id=11, permission_group_id=22)


def _client():
    return ZendeskClient(subdomain="testsub", email="a@b.com", token="tok")


def _fake_api(monkeypatch, responses):
    """Replace urlopen; `responses` maps (method, path suffix) to a JSON-able
    reply. Returns the list of (method, url, decoded body) calls made."""
    calls = []

    @contextmanager
    def fake_urlopen(req, timeout=None):
        body = _json.loads(req.data) if req.data else None
        calls.append((req.get_method(), req.full_url, body))
        for (method, suffix), reply in responses.items():
            if req.get_method() == method and req.full_url.split("?")[0].endswith(suffix):
                yield io.BytesIO(_json.dumps(reply).encode())
                return
        raise AssertionError(f"unexpected call {req.get_method()} {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


class TestBuildArticleDraftPayload:
    def test_forces_draft_and_no_notification(self):
        out = _build_article_draft_payload(dict(_VALID))
        assert out["article"]["draft"] is True
        assert out["notify_subscribers"] is False
        assert out["article"]["locale"] == "en-us"

    @pytest.mark.parametrize("value", [False, True])
    def test_rejects_any_draft_argument(self, value):
        with pytest.raises(ValueError, match="draft cannot be set"):
            _build_article_draft_payload({**_VALID, "draft": value})

    @pytest.mark.parametrize("key", ["user_segment_id", "permission_group_id"])
    def test_requires_audience_and_permission_group(self, key):
        fields = {**_VALID, key: None}
        with pytest.raises(ValueError, match=f"{key} is required"):
            _build_article_draft_payload(fields)

    def test_rejects_bool_as_segment_id(self):
        with pytest.raises(ValueError, match="user_segment_id is required"):
            _build_article_draft_payload({**_VALID, "user_segment_id": True})

    @pytest.mark.parametrize("key", ["title", "body"])
    def test_requires_non_empty_title_and_body(self, key):
        with pytest.raises(ValueError, match=f"{key} is required"):
            _build_article_draft_payload({**_VALID, key: "  "})

    def test_label_names_must_be_list_of_strings(self):
        with pytest.raises(TypeError):
            _build_article_draft_payload({**_VALID, "label_names": "ai-draft"})
        out = _build_article_draft_payload({**_VALID, "label_names": ["ai-draft"]})
        assert out["article"]["label_names"] == ["ai-draft"]


class TestBuildArticleTranslationPayload:
    def test_always_keeps_draft_true(self):
        out = _build_article_translation_payload({"title": "New", "body": None})
        assert out == {"translation": {"title": "New", "draft": True}}

    def test_rejects_draft_argument(self):
        with pytest.raises(ValueError, match="draft cannot be set"):
            _build_article_translation_payload({"title": "x", "draft": False})

    def test_rejects_empty_update(self):
        with pytest.raises(ValueError, match="nothing to update"):
            _build_article_translation_payload({"title": None, "body": None})


class TestCreateArticleDraft:
    def test_posts_draft_to_the_section(self, monkeypatch):
        calls = _fake_api(monkeypatch, {
            ("POST", "/help_center/sections/7/articles"): {"article": {"id": 9, "draft": True}},
        })
        out = _client().create_help_center_article_draft(7, **_VALID)
        assert out["id"] == 9
        method, url, body = calls[0]
        assert url == "https://testsub.zendesk.com/api/v2/help_center/sections/7/articles"
        assert body["article"]["draft"] is True
        assert body["notify_subscribers"] is False

    def test_draft_false_never_reaches_the_api(self, monkeypatch):
        calls = _fake_api(monkeypatch, {})
        with pytest.raises(ValueError, match="draft cannot be set"):
            _client().create_help_center_article_draft(7, **_VALID, draft=False)
        assert calls == []


class TestUpdateArticleDraft:
    def test_refuses_a_published_article(self, monkeypatch):
        calls = _fake_api(monkeypatch, {
            ("GET", "/help_center/articles/9"): {"article": {"id": 9, "draft": False, "locale": "en-us"}},
        })
        with pytest.raises(ValueError, match="not a draft"):
            _client().update_help_center_article_draft(9, title="x")
        assert [c[0] for c in calls] == ["GET"]

    def test_refuses_when_draft_flag_is_missing(self, monkeypatch):
        _fake_api(monkeypatch, {("GET", "/help_center/articles/9"): {"article": {"id": 9}}})
        with pytest.raises(ValueError, match="not a draft"):
            _client().update_help_center_article_draft(9, title="x")

    def test_updates_translation_with_draft_true_and_labels(self, monkeypatch):
        calls = _fake_api(monkeypatch, {
            ("GET", "/help_center/articles/9"): {"article": {"id": 9, "draft": True, "locale": "en-us"}},
            ("PUT", "/help_center/articles/9/translations/en-us"): {"translation": {"id": 1}},
            ("PUT", "/help_center/articles/9"): {"article": {"id": 9}},
        })
        _client().update_help_center_article_draft(9, body="<p>new</p>", label_names=["ai-draft"])
        puts = [(u.rsplit("/api/v2/", 1)[1], b) for m, u, b in calls if m == "PUT"]
        assert puts == [
            ("help_center/articles/9/translations/en-us", {"translation": {"body": "<p>new</p>", "draft": True}}),
            ("help_center/articles/9", {"article": {"label_names": ["ai-draft"]}}),
        ]

    def test_rejects_unsupported_fields_before_any_call(self, monkeypatch):
        calls = _fake_api(monkeypatch, {})
        with pytest.raises(ValueError, match="unsupported field"):
            _client().update_help_center_article_draft(9, draft=False)
        assert calls == []
