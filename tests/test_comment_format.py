"""Tests for comment rendering: create_ticket_comment sends html_body, so plain
text and markdown must be rendered to HTML or newlines and lists collapse."""
import pytest

from zendesk_mcp_server.zendesk_client import _comment_html, ZendeskClient


REPLY = """Delivery completed.

Installed
- MA14437: Canon imageFORCE C3130
- MA14438: Canon imageFORCE 4125

Follow-ups:
1. Confirm the GPO.
2. Set the default address book."""


class TestCommentHtml:
    def test_paragraphs_survive(self):
        html = _comment_html("First.\n\nSecond.")
        assert html.count("<p>") == 2

    def test_single_newline_becomes_break(self):
        assert "<br" in _comment_html("line one\nline two")

    def test_lists_directly_under_a_line_render(self):
        html = _comment_html(REPLY)
        assert html.count("<li>") == 4
        assert "<ul>" in html and "<ol>" in html

    def test_html_passes_through_in_markdown_mode(self):
        html = _comment_html("<p>Hi</p>\n<ul>\n<li>a</li>\n</ul>")
        assert "<ul>" in html and "&lt;" not in html

    def test_html_format_is_verbatim(self):
        raw = "line one\nline two"
        assert _comment_html(raw, "html") == raw

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError):
            _comment_html("x", "rtf")


class TestPostCommentPayload:
    def _client(self, monkeypatch):
        client = ZendeskClient.__new__(ZendeskClient)
        sent = {}

        class _Ticket:
            comment = None

        class _Tickets:
            def __call__(self, id):
                return _Ticket()

            def update(self, ticket):
                sent["comment"] = ticket.comment

        class _Zenpy:
            tickets = _Tickets()

        client.client = _Zenpy()
        return client, sent

    def test_markdown_default_sends_rendered_html(self, monkeypatch):
        client, sent = self._client(monkeypatch)
        client.post_comment(1, REPLY, public=False)
        assert "<ol>" in sent["comment"].html_body
        assert sent["comment"].public is False

    def test_bad_format_fails_before_any_api_call(self, monkeypatch):
        client, sent = self._client(monkeypatch)
        with pytest.raises(ValueError):
            client.post_comment(1, "x", format="rtf")
        assert sent == {}
