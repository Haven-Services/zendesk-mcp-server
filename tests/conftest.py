"""Test bootstrap.

Importing zendesk_mcp_server pulls in `server`, which constructs a ZendeskClient
(and thus a Zenpy client) at import time. Zenpy validates that credentials are
*present* on construction but makes no network call, so dummy env vars are enough
to let the import succeed. This keeps the pure-helper tests hermetic — no real
credentials, no network. Set before any test module imports the package.
"""
import os

os.environ.setdefault("ZENDESK_SUBDOMAIN", "test")
os.environ.setdefault("ZENDESK_EMAIL", "test@example.com")
os.environ.setdefault("ZENDESK_API_KEY", "dummy-token")
