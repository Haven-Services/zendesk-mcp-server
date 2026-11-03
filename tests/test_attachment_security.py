import pytest

from zendesk_mcp_server import zendesk_client


def test_attachment_rejects_untrusted_host_before_http_request(monkeypatch):
    client = zendesk_client.ZendeskClient.__new__(zendesk_client.ZendeskClient)
    client.subdomain = "test"
    client.auth_header = "Basic secret"

    def unexpected_request(*args, **kwargs):
        raise AssertionError("HTTP request must not be attempted")

    monkeypatch.setattr(zendesk_client._requests, "get", unexpected_request)

    with pytest.raises(ValueError, match="test\\.zendesk\\.com"):
        client.get_ticket_attachment("https://attacker.example/image.png")
