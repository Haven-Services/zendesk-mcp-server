# Zendesk Config-Write Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-write tier to the Zendesk MCP fork — create/update/list/get tools for ticket fields, ticket forms, macros, and triggers (no delete).

**Architecture:** Pure payload-builder/validator functions (network-free, unit-tested) feed a shared `_send_json` POST/PUT helper that mirrors the existing `_get_json`. Thin `ZendeskClient` methods compose builder + transport. `server.py` gains 12 tool declarations + dispatch branches. Two files only — no new modules (the fork stays consolidated).

**Tech Stack:** Python 3.12, `mcp`, `zenpy` (unused by this tier), `urllib` (stdlib) for transport, `pytest` for pure-helper tests.

## Global Constraints

- `requires-python = ">=3.12"`; deps stay as in `pyproject.toml` (no new dependencies — `urllib` is stdlib).
- Two-file layout: API logic in `src/zendesk_mcp_server/zendesk_client.py`, tool surface in `src/zendesk_mcp_server/server.py`. No new source modules.
- Unit tests are **pure / network-free** and live in `tests/test_pure_helpers.py`; they must not import `zendesk_mcp_server.server` (it builds a client at import time needing env vars).
- Create/update wrap the object under its singular key: `{ticket_field|ticket_form|macro|trigger: {…}}`.
- Payload builders **drop `None` values** so the same builder serves create and update; supplied arrays (`actions`, trigger `conditions`, `custom_field_options`) **replace** wholesale (Zendesk has no partial-array update).
- **No delete tools.**
- Tool dispatch errors return `Error: <msg>` TextContent (existing pattern); `_send_json` surfaces the Zendesk HTTP response body verbatim.
- Each task ends with a commit on branch `feat/config-write-tools`.

---

### Task 1: `_send_json` write transport

**Files:**
- Modify: `src/zendesk_mcp_server/zendesk_client.py` (add method to `ZendeskClient`, after `_get_json`)
- Test: `tests/test_pure_helpers.py`

**Interfaces:**
- Consumes: `self.base_url`, `self.auth_header` (already set in `__init__`).
- Produces: `ZendeskClient._send_json(self, method: str, path: str, payload: dict, *, error_context: str, timeout: int = 30) -> dict` — JSON-decoded response body; raises `Exception` carrying the HTTP error body on `HTTPError`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pure_helpers.py`:

```python
import io
import json as _json
import urllib.error
from contextlib import contextmanager

from zendesk_mcp_server.zendesk_client import ZendeskClient


def _client():
    # __init__ does no network — safe to construct with dummy creds.
    return ZendeskClient(subdomain="testsub", email="a@b.com", token="tok")


class TestSendJson:
    def test_post_sends_method_body_and_auth(self, monkeypatch):
        captured = {}

        @contextmanager
        def fake_urlopen(req, timeout=None):
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["auth"] = req.get_header("Authorization")
            yield io.BytesIO(b'{"macro": {"id": 5}}')

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        out = _client()._send_json(
            "POST", "macros.json", {"macro": {"title": "t"}},
            error_context="create macro",
        )
        assert out == {"macro": {"id": 5}}
        assert captured["method"] == "POST"
        assert captured["url"] == "https://testsub.zendesk.com/api/v2/macros.json"
        assert _json.loads(captured["body"]) == {"macro": {"title": "t"}}
        assert captured["auth"].startswith("Basic ")

    def test_http_error_surfaces_response_body(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 422, "Unprocessable Entity", {},
                io.BytesIO(b'{"description":"Invalid action field"}'),
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(Exception) as ei:
            _client()._send_json(
                "PUT", "macros/5.json", {"macro": {}},
                error_context="update macro 5",
            )
        msg = str(ei.value)
        assert "update macro 5" in msg
        assert "422" in msg
        assert "Invalid action field" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestSendJson -v`
Expected: FAIL — `AttributeError: 'ZendeskClient' object has no attribute '_send_json'`.

- [ ] **Step 3: Write minimal implementation**

In `zendesk_client.py`, add immediately after the `_get_json` method (around line 228):

```python
    def _send_json(
        self,
        method: str,
        path: str,
        payload: Dict[str, Any],
        *,
        error_context: str,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Authenticated POST/PUT against the Zendesk REST API. Returns decoded JSON.

        Mirrors `_get_json`: HTTP errors re-raise as `Exception` carrying the
        response body (where Zendesk explains 422 reasons); other exceptions
        propagate with their original type.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('Authorization', self.auth_header)
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='replace') if e.fp else "No response body"
            raise Exception(f"Failed to {error_context}: HTTP {e.code} - {e.reason}. {error_body}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestSendJson -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/code/zendesk-mcp-server
git add src/zendesk_mcp_server/zendesk_client.py tests/test_pure_helpers.py
git commit -m "feat: add _send_json POST/PUT transport helper"
```

---

### Task 2: Ticket-field write tools

**Files:**
- Modify: `src/zendesk_mcp_server/zendesk_client.py` (module-level builder + constants near the other `_*_FIELDS`; methods in `ZendeskClient`)
- Test: `tests/test_pure_helpers.py`

**Interfaces:**
- Consumes: `_project`, `_TICKET_FIELD_FIELDS` (existing), `_send_json` (Task 1).
- Produces:
  - `_build_ticket_field_payload(fields: dict) -> dict` — `{'ticket_field': <non-None fields>}`; raises `ValueError` on bad `type` or missing options for tagger/multiselect.
  - `ZendeskClient.create_ticket_field(self, **fields) -> dict`
  - `ZendeskClient.update_ticket_field(self, ticket_field_id: int, **fields) -> dict`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pure_helpers.py` (and add `_build_ticket_field_payload` to the existing import block from `zendesk_client`):

```python
from zendesk_mcp_server.zendesk_client import _build_ticket_field_payload


class TestBuildTicketFieldPayload:
    def test_drops_none_and_wraps(self):
        out = _build_ticket_field_payload(
            {"type": "text", "title": "Ship-to Address", "description": None}
        )
        assert out == {"ticket_field": {"type": "text", "title": "Ship-to Address"}}

    def test_tagger_requires_options(self):
        with pytest.raises(ValueError, match="custom_field_options"):
            _build_ticket_field_payload({"type": "tagger", "title": "Job Type"})

    def test_tagger_with_options_ok(self):
        out = _build_ticket_field_payload({
            "type": "multiselect", "title": "HW",
            "custom_field_options": [{"name": "Laptop", "value": "laptop"}],
        })
        assert out["ticket_field"]["type"] == "multiselect"

    def test_bad_type_rejected(self):
        with pytest.raises(ValueError, match="Unsupported ticket field type"):
            _build_ticket_field_payload({"type": "lookup", "title": "x"})

    def test_update_without_type_skips_option_check(self):
        # update may send only mutable fields, no type — must not raise.
        out = _build_ticket_field_payload({"title": "Renamed"})
        assert out == {"ticket_field": {"title": "Renamed"}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildTicketFieldPayload -v`
Expected: FAIL — `ImportError: cannot import name '_build_ticket_field_payload'`.

- [ ] **Step 3: Write minimal implementation**

In `zendesk_client.py`, add near the projection constants (after `_CUSTOM_FIELD_OPTION_FIELDS`, ~line 47):

```python
_TICKET_FIELD_TYPES = frozenset({
    'text', 'textarea', 'checkbox', 'date', 'integer', 'decimal',
    'regexp', 'multiselect', 'tagger',
})
_OPTION_REQUIRED_FIELD_TYPES = frozenset({'tagger', 'multiselect'})


def _build_ticket_field_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the {'ticket_field': {...}} envelope, dropping None values so the
    same builder serves create and update. Validates the type enum and that
    tagger/multiselect fields carry non-empty custom_field_options."""
    body = {k: v for k, v in fields.items() if v is not None}
    ftype = body.get('type')
    if ftype is not None and ftype not in _TICKET_FIELD_TYPES:
        raise ValueError(
            f"Unsupported ticket field type {ftype!r}; one of {sorted(_TICKET_FIELD_TYPES)}"
        )
    if ftype in _OPTION_REQUIRED_FIELD_TYPES and not body.get('custom_field_options'):
        raise ValueError(
            f"ticket field type {ftype!r} requires non-empty custom_field_options"
        )
    return {'ticket_field': body}
```

In the `ZendeskClient` class (after `list_ticket_fields`, ~line 647), add:

```python
    def create_ticket_field(self, **fields: Any) -> Dict[str, Any]:
        """Create a ticket field via POST /api/v2/ticket_fields.json.
        tagger/multiselect require custom_field_options [{name, value}]."""
        payload = _build_ticket_field_payload(fields)
        data = self._send_json(
            'POST', 'ticket_fields.json', payload, error_context='create ticket field'
        )
        return _project(data.get('ticket_field', {}), _TICKET_FIELD_FIELDS)

    def update_ticket_field(self, ticket_field_id: int, **fields: Any) -> Dict[str, Any]:
        """Update a ticket field via PUT /api/v2/ticket_fields/{id}.json.
        A supplied custom_field_options list replaces the existing option set."""
        payload = _build_ticket_field_payload(fields)
        data = self._send_json(
            'PUT', f'ticket_fields/{int(ticket_field_id)}.json', payload,
            error_context=f'update ticket field {ticket_field_id}',
        )
        return _project(data.get('ticket_field', {}), _TICKET_FIELD_FIELDS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildTicketFieldPayload -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/code/zendesk-mcp-server
git add src/zendesk_mcp_server/zendesk_client.py tests/test_pure_helpers.py
git commit -m "feat: add create/update ticket field client methods"
```

---

### Task 3: Ticket-form write tools

**Files:**
- Modify: `src/zendesk_mcp_server/zendesk_client.py`
- Test: `tests/test_pure_helpers.py`

**Interfaces:**
- Consumes: `_project`, `_TICKET_FORM_FIELDS` (existing), `_send_json`.
- Produces:
  - `_build_ticket_form_payload(fields: dict) -> dict` — `{'ticket_form': <non-None>}`; raises `TypeError` if `ticket_field_ids` present and not a list.
  - `ZendeskClient.create_ticket_form(self, **fields) -> dict`
  - `ZendeskClient.update_ticket_form(self, ticket_form_id: int, **fields) -> dict`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pure_helpers.py` (extend the `zendesk_client` import with `_build_ticket_form_payload`):

```python
from zendesk_mcp_server.zendesk_client import _build_ticket_form_payload


class TestBuildTicketFormPayload:
    def test_drops_none_and_wraps(self):
        out = _build_ticket_form_payload(
            {"name": "Hardware Request", "ticket_field_ids": [1, 2, 3], "default": None}
        )
        assert out == {"ticket_form": {"name": "Hardware Request", "ticket_field_ids": [1, 2, 3]}}

    def test_field_ids_must_be_list(self):
        with pytest.raises(TypeError, match="ticket_field_ids"):
            _build_ticket_form_payload({"name": "x", "ticket_field_ids": 5})

    def test_update_partial_ok(self):
        out = _build_ticket_form_payload({"active": False})
        assert out == {"ticket_form": {"active": False}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildTicketFormPayload -v`
Expected: FAIL — `ImportError: cannot import name '_build_ticket_form_payload'`.

- [ ] **Step 3: Write minimal implementation**

In `zendesk_client.py`, add near the other builders:

```python
def _build_ticket_form_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the {'ticket_form': {...}} envelope, dropping None values. A
    supplied ticket_field_ids list is ordered and replaces the form's field set."""
    body = {k: v for k, v in fields.items() if v is not None}
    field_ids = body.get('ticket_field_ids')
    if field_ids is not None and not isinstance(field_ids, (list, tuple)):
        raise TypeError("ticket_field_ids must be a list of field IDs")
    return {'ticket_form': body}
```

In `ZendeskClient` (after `list_ticket_forms`):

```python
    def create_ticket_form(self, **fields: Any) -> Dict[str, Any]:
        """Create a ticket form via POST /api/v2/ticket_forms.json.
        ticket_field_ids is an ordered list driving field display order."""
        payload = _build_ticket_form_payload(fields)
        data = self._send_json(
            'POST', 'ticket_forms.json', payload, error_context='create ticket form'
        )
        return _project(data.get('ticket_form', {}), _TICKET_FORM_FIELDS)

    def update_ticket_form(self, ticket_form_id: int, **fields: Any) -> Dict[str, Any]:
        """Update a ticket form via PUT /api/v2/ticket_forms/{id}.json.
        A supplied ticket_field_ids list REPLACES the form's field set (send the
        full ordered list, not just additions)."""
        payload = _build_ticket_form_payload(fields)
        data = self._send_json(
            'PUT', f'ticket_forms/{int(ticket_form_id)}.json', payload,
            error_context=f'update ticket form {ticket_form_id}',
        )
        return _project(data.get('ticket_form', {}), _TICKET_FORM_FIELDS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildTicketFormPayload -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/code/zendesk-mcp-server
git add src/zendesk_mcp_server/zendesk_client.py tests/test_pure_helpers.py
git commit -m "feat: add create/update ticket form client methods"
```

---

### Task 4: Macro tools (list/get/create/update)

**Files:**
- Modify: `src/zendesk_mcp_server/zendesk_client.py`
- Test: `tests/test_pure_helpers.py`

**Interfaces:**
- Consumes: `_project`, `_get_json`, `_send_json`, `_coerce_pagination`.
- Produces:
  - `_MACRO_FIELDS` tuple; `_build_macro_payload(fields: dict) -> dict`.
  - `ZendeskClient.list_macros(self, page=1, per_page=100) -> dict`
  - `ZendeskClient.get_macro(self, macro_id: int) -> dict`
  - `ZendeskClient.create_macro(self, **fields) -> dict`
  - `ZendeskClient.update_macro(self, macro_id: int, **fields) -> dict`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pure_helpers.py` (import `_build_macro_payload`):

```python
from zendesk_mcp_server.zendesk_client import _build_macro_payload


class TestBuildMacroPayload:
    def test_wraps_and_drops_none(self):
        out = _build_macro_payload({
            "title": "Intake",
            "actions": [{"field": "status", "value": "pending"}],
            "restriction": None,
        })
        assert out == {"macro": {
            "title": "Intake",
            "actions": [{"field": "status", "value": "pending"}],
        }}

    def test_actions_must_be_list(self):
        with pytest.raises(TypeError, match="actions"):
            _build_macro_payload({"title": "x", "actions": {"field": "status"}})

    def test_update_partial_ok(self):
        out = _build_macro_payload({"active": False})
        assert out == {"macro": {"active": False}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildMacroPayload -v`
Expected: FAIL — `ImportError: cannot import name '_build_macro_payload'`.

- [ ] **Step 3: Write minimal implementation**

In `zendesk_client.py`, add a projection constant near the other `_*_FIELDS`:

```python
_MACRO_FIELDS: Tuple[str, ...] = (
    'id', 'title', 'active', 'restriction', 'actions', 'created_at', 'updated_at',
)
```

Add the builder near the others:

```python
def _build_macro_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the {'macro': {...}} envelope, dropping None values. A supplied
    actions list REPLACES the macro's entire action set (no partial update)."""
    body = {k: v for k, v in fields.items() if v is not None}
    actions = body.get('actions')
    if actions is not None and not isinstance(actions, list):
        raise TypeError("macro actions must be a list of {field, value} dicts")
    return {'macro': body}
```

In `ZendeskClient` (after `list_ticket_fields`/the form methods):

```python
    def list_macros(self, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """List macros via GET /api/v2/macros.json. Lean projection — call
        get_macro for a single macro's full actions array."""
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        data = self._get_json('macros.json', params, error_context='list macros')
        macros = [_project(m, _MACRO_FIELDS) for m in data.get('macros', [])]
        return {
            'page': page,
            'per_page': per_page,
            'count': len(macros),
            'has_more': data.get('next_page') is not None or len(macros) == per_page,
            'macros': macros,
        }

    def get_macro(self, macro_id: int) -> Dict[str, Any]:
        """Get one macro via GET /api/v2/macros/{id}.json, including its full
        actions array — fetch this before update_macro to edit actions safely."""
        data = self._get_json(
            f'macros/{int(macro_id)}.json', {}, error_context=f'get macro {macro_id}'
        )
        return _project(data.get('macro', {}), _MACRO_FIELDS)

    def create_macro(self, **fields: Any) -> Dict[str, Any]:
        """Create a macro via POST /api/v2/macros.json. Requires title + actions
        [{field, value}]. Omit restriction for All Agents."""
        payload = _build_macro_payload(fields)
        data = self._send_json('POST', 'macros.json', payload, error_context='create macro')
        return _project(data.get('macro', {}), _MACRO_FIELDS)

    def update_macro(self, macro_id: int, **fields: Any) -> Dict[str, Any]:
        """Update a macro via PUT /api/v2/macros/{id}.json. A supplied actions
        list REPLACES all actions — fetch with get_macro, edit, then send the
        full array."""
        payload = _build_macro_payload(fields)
        data = self._send_json(
            'PUT', f'macros/{int(macro_id)}.json', payload,
            error_context=f'update macro {macro_id}',
        )
        return _project(data.get('macro', {}), _MACRO_FIELDS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildMacroPayload -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/code/zendesk-mcp-server
git add src/zendesk_mcp_server/zendesk_client.py tests/test_pure_helpers.py
git commit -m "feat: add macro list/get/create/update client methods"
```

---

### Task 5: Trigger tools (list/get/create/update)

**Files:**
- Modify: `src/zendesk_mcp_server/zendesk_client.py`
- Test: `tests/test_pure_helpers.py`

**Interfaces:**
- Consumes: `_project`, `_get_json`, `_send_json`, `_coerce_pagination`.
- Produces:
  - `_TRIGGER_FIELDS` tuple; `_build_trigger_payload(fields: dict) -> dict`.
  - `ZendeskClient.list_triggers(self, page=1, per_page=100) -> dict`
  - `ZendeskClient.get_trigger(self, trigger_id: int) -> dict`
  - `ZendeskClient.create_trigger(self, **fields) -> dict`
  - `ZendeskClient.update_trigger(self, trigger_id: int, **fields) -> dict`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pure_helpers.py` (import `_build_trigger_payload`):

```python
from zendesk_mcp_server.zendesk_client import _build_trigger_payload


class TestBuildTriggerPayload:
    def test_wraps_and_drops_none(self):
        out = _build_trigger_payload({
            "title": "Email -> Hardware",
            "conditions": {"all": [{"field": "status", "operator": "is", "value": "new"}], "any": []},
            "actions": [{"field": "group_id", "value": "21014104298395"}],
            "category_id": None,
        })
        assert out["trigger"]["title"] == "Email -> Hardware"
        assert "category_id" not in out["trigger"]

    def test_conditions_must_be_dict(self):
        with pytest.raises(TypeError, match="conditions"):
            _build_trigger_payload({"title": "x", "conditions": [], "actions": []})

    def test_actions_must_be_list(self):
        with pytest.raises(TypeError, match="actions"):
            _build_trigger_payload({"title": "x", "actions": {"field": "group_id"}})

    def test_update_partial_ok(self):
        out = _build_trigger_payload({"active": False})
        assert out == {"trigger": {"active": False}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildTriggerPayload -v`
Expected: FAIL — `ImportError: cannot import name '_build_trigger_payload'`.

- [ ] **Step 3: Write minimal implementation**

In `zendesk_client.py`, add a projection constant:

```python
_TRIGGER_FIELDS: Tuple[str, ...] = (
    'id', 'title', 'active', 'conditions', 'actions', 'category_id',
    'created_at', 'updated_at',
)
```

Add the builder:

```python
def _build_trigger_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the {'trigger': {...}} envelope, dropping None values. Supplied
    conditions ({all, any}) and actions REPLACE the existing sets wholesale."""
    body = {k: v for k, v in fields.items() if v is not None}
    conditions = body.get('conditions')
    if conditions is not None and not isinstance(conditions, dict):
        raise TypeError("trigger conditions must be a dict with 'all' and/or 'any' lists")
    actions = body.get('actions')
    if actions is not None and not isinstance(actions, list):
        raise TypeError("trigger actions must be a list of {field, value} dicts")
    return {'trigger': body}
```

In `ZendeskClient`:

```python
    def list_triggers(self, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """List triggers via GET /api/v2/triggers.json. Lean projection — call
        get_trigger for one trigger's full conditions + actions."""
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        data = self._get_json('triggers.json', params, error_context='list triggers')
        triggers = [_project(t, _TRIGGER_FIELDS) for t in data.get('triggers', [])]
        return {
            'page': page,
            'per_page': per_page,
            'count': len(triggers),
            'has_more': data.get('next_page') is not None or len(triggers) == per_page,
            'triggers': triggers,
        }

    def get_trigger(self, trigger_id: int) -> Dict[str, Any]:
        """Get one trigger via GET /api/v2/triggers/{id}.json, including its full
        conditions + actions — fetch before update_trigger to edit safely."""
        data = self._get_json(
            f'triggers/{int(trigger_id)}.json', {}, error_context=f'get trigger {trigger_id}'
        )
        return _project(data.get('trigger', {}), _TRIGGER_FIELDS)

    def create_trigger(self, **fields: Any) -> Dict[str, Any]:
        """Create a trigger via POST /api/v2/triggers.json. Requires title,
        conditions {all, any}, and actions [{field, value}]."""
        payload = _build_trigger_payload(fields)
        data = self._send_json('POST', 'triggers.json', payload, error_context='create trigger')
        return _project(data.get('trigger', {}), _TRIGGER_FIELDS)

    def update_trigger(self, trigger_id: int, **fields: Any) -> Dict[str, Any]:
        """Update a trigger via PUT /api/v2/triggers/{id}.json. Supplied
        conditions and actions REPLACE the existing sets — fetch with
        get_trigger, edit, then send the full arrays."""
        payload = _build_trigger_payload(fields)
        data = self._send_json(
            'PUT', f'triggers/{int(trigger_id)}.json', payload,
            error_context=f'update trigger {trigger_id}',
        )
        return _project(data.get('trigger', {}), _TRIGGER_FIELDS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest tests/test_pure_helpers.py::TestBuildTriggerPayload -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/code/zendesk-mcp-server
git add src/zendesk_mcp_server/zendesk_client.py tests/test_pure_helpers.py
git commit -m "feat: add trigger list/get/create/update client methods"
```

---

### Task 6: Wire the 12 tools into `server.py`

**Files:**
- Modify: `src/zendesk_mcp_server/server.py` (add 12 `types.Tool` entries to `handle_list_tools`; add 12 `elif` branches to `handle_call_tool`)
- Test: full pure-helper suite still green; live verification via `/mcp` reconnect (manual).

**Interfaces:**
- Consumes: all `ZendeskClient` methods from Tasks 2–5.
- Produces: 12 MCP tools — `create_ticket_field`, `update_ticket_field`, `create_ticket_form`, `update_ticket_form`, `list_macros`, `get_macro`, `create_macro`, `update_macro`, `list_triggers`, `get_trigger`, `create_trigger`, `update_trigger`.

- [ ] **Step 1: Add tool declarations**

In `server.py`, inside `handle_list_tools`'s returned list, before the closing `]` (after the `list_ticket_fields` entry, ~line 444), add:

```python
        ,
        types.Tool(
            name="create_ticket_field",
            description=(
                "Create a custom ticket field. type is one of text, textarea, "
                "checkbox, date, integer, decimal, regexp, multiselect, tagger. "
                "tagger/multiselect REQUIRE custom_field_options [{name, value}]. "
                "Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "text|textarea|checkbox|date|integer|decimal|regexp|multiselect|tagger"},
                    "title": {"type": "string", "description": "Agent-facing title"},
                    "description": {"type": "string"},
                    "title_in_portal": {"type": "string", "description": "End-user-facing title"},
                    "visible_in_portal": {"type": "boolean"},
                    "editable_in_portal": {"type": "boolean"},
                    "required_in_portal": {"type": "boolean"},
                    "required": {"type": "boolean"},
                    "active": {"type": "boolean"},
                    "custom_field_options": {"type": "array", "items": {"type": "object"}, "description": "tagger/multiselect: [{name, value}]"},
                    "tag": {"type": "string", "description": "checkbox: tag set when checked"},
                    "regexp_for_validation": {"type": "string", "description": "regexp type validation pattern"},
                },
                "required": ["type", "title"],
            }
        ),
        types.Tool(
            name="update_ticket_field",
            description=(
                "Update a custom ticket field by id. A supplied custom_field_options "
                "list REPLACES the field's option set. Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_field_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "title_in_portal": {"type": "string"},
                    "visible_in_portal": {"type": "boolean"},
                    "editable_in_portal": {"type": "boolean"},
                    "required_in_portal": {"type": "boolean"},
                    "required": {"type": "boolean"},
                    "active": {"type": "boolean"},
                    "custom_field_options": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["ticket_field_id"],
            }
        ),
        types.Tool(
            name="create_ticket_form",
            description=(
                "Create a ticket form. ticket_field_ids is an ORDERED list of "
                "field ids driving display order. Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ticket_field_ids": {"type": "array", "items": {"type": "integer"}, "description": "Ordered field ids"},
                    "display_name": {"type": "string"},
                    "active": {"type": "boolean"},
                    "end_user_visible": {"type": "boolean"},
                    "default": {"type": "boolean"},
                    "position": {"type": "integer"},
                },
                "required": ["name"],
            }
        ),
        types.Tool(
            name="update_ticket_form",
            description=(
                "Update a ticket form by id. A supplied ticket_field_ids list "
                "REPLACES the form's field set — send the FULL ordered list, not "
                "just additions (call list_ticket_forms first to read current ids). "
                "Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_form_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "ticket_field_ids": {"type": "array", "items": {"type": "integer"}, "description": "Full ordered field-id list (replaces existing)"},
                    "display_name": {"type": "string"},
                    "active": {"type": "boolean"},
                    "end_user_visible": {"type": "boolean"},
                    "default": {"type": "boolean"},
                    "position": {"type": "integer"},
                },
                "required": ["ticket_form_id"],
            }
        ),
        types.Tool(
            name="list_macros",
            description="List macros (lean projection). Call get_macro for one macro's full actions array.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "default": 1},
                    "per_page": {"type": "integer", "default": 100},
                },
                "required": [],
            }
        ),
        types.Tool(
            name="get_macro",
            description="Get one macro by id, including its full actions array. Fetch this before update_macro to edit actions safely.",
            inputSchema={
                "type": "object",
                "properties": {"macro_id": {"type": "integer"}},
                "required": ["macro_id"],
            }
        ),
        types.Tool(
            name="create_macro",
            description=(
                "Create a macro. Requires title and actions [{field, value}]. "
                "Omit restriction for All Agents; {type:'Group', id:N} or "
                "{type:'User', id:N} to scope. Action notes: status accepts only "
                "open/pending/hold/solved (use custom_status_id for custom statuses); "
                "current_tags adds, set_tags replaces, remove_tags removes; comment "
                "via comment_value (+ optional comment_value_html). Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "actions": {"type": "array", "items": {"type": "object"}, "description": "[{field, value}]"},
                    "restriction": {"type": "object", "description": "Omit=All Agents; {type:'Group'|'User', id|ids}"},
                    "description": {"type": "string"},
                    "active": {"type": "boolean"},
                },
                "required": ["title", "actions"],
            }
        ),
        types.Tool(
            name="update_macro",
            description=(
                "Update a macro by id. A supplied actions list REPLACES ALL actions "
                "(no partial update — omitting an action deletes it). Call get_macro, "
                "edit the array, then send it in full. Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "macro_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "actions": {"type": "array", "items": {"type": "object"}, "description": "Full action array (replaces existing)"},
                    "restriction": {"type": "object"},
                    "active": {"type": "boolean"},
                },
                "required": ["macro_id"],
            }
        ),
        types.Tool(
            name="list_triggers",
            description="List triggers (lean projection). Call get_trigger for one trigger's full conditions + actions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "default": 1},
                    "per_page": {"type": "integer", "default": 100},
                },
                "required": [],
            }
        ),
        types.Tool(
            name="get_trigger",
            description="Get one trigger by id, including full conditions + actions. Fetch before update_trigger to edit safely.",
            inputSchema={
                "type": "object",
                "properties": {"trigger_id": {"type": "integer"}},
                "required": ["trigger_id"],
            }
        ),
        types.Tool(
            name="create_trigger",
            description=(
                "Create a trigger. Requires title, conditions {all:[{field,operator,value}], "
                "any:[...]}, and actions [{field, value}]. Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "conditions": {"type": "object", "description": "{all:[{field,operator,value}], any:[...]}"},
                    "actions": {"type": "array", "items": {"type": "object"}, "description": "[{field, value}]"},
                    "active": {"type": "boolean"},
                    "category_id": {"type": "string"},
                    "position": {"type": "integer"},
                },
                "required": ["title", "conditions", "actions"],
            }
        ),
        types.Tool(
            name="update_trigger",
            description=(
                "Update a trigger by id. Supplied conditions and actions REPLACE the "
                "existing sets (no partial update). Call get_trigger, edit, then send "
                "the full arrays. Writes live to production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trigger_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "conditions": {"type": "object", "description": "Full conditions object (replaces existing)"},
                    "actions": {"type": "array", "items": {"type": "object"}, "description": "Full action array (replaces existing)"},
                    "active": {"type": "boolean"},
                    "category_id": {"type": "string"},
                    "position": {"type": "integer"},
                },
                "required": ["trigger_id"],
            }
        )
```

- [ ] **Step 2: Add dispatch branches**

In `server.py`, inside `handle_call_tool`, before the final `else: raise ValueError(f"Unknown tool: {name}")` (~line 643), add:

```python
        elif name == "create_ticket_field":
            if not arguments:
                raise ValueError("Missing arguments")
            result = zendesk_client.create_ticket_field(**arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Ticket field created", "ticket_field": result}, indent=2))]

        elif name == "update_ticket_field":
            if not arguments or arguments.get("ticket_field_id") is None:
                raise ValueError("ticket_field_id is required")
            field_id = arguments.pop("ticket_field_id")
            result = zendesk_client.update_ticket_field(int(field_id), **arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Ticket field updated", "ticket_field": result}, indent=2))]

        elif name == "create_ticket_form":
            if not arguments:
                raise ValueError("Missing arguments")
            result = zendesk_client.create_ticket_form(**arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Ticket form created", "ticket_form": result}, indent=2))]

        elif name == "update_ticket_form":
            if not arguments or arguments.get("ticket_form_id") is None:
                raise ValueError("ticket_form_id is required")
            form_id = arguments.pop("ticket_form_id")
            result = zendesk_client.update_ticket_form(int(form_id), **arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Ticket form updated", "ticket_form": result}, indent=2))]

        elif name == "list_macros":
            result = zendesk_client.list_macros(
                page=(arguments or {}).get("page", 1),
                per_page=(arguments or {}).get("per_page", 100),
            )
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_macro":
            if not arguments or arguments.get("macro_id") is None:
                raise ValueError("macro_id is required")
            result = zendesk_client.get_macro(int(arguments["macro_id"]))
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_macro":
            if not arguments:
                raise ValueError("Missing arguments")
            result = zendesk_client.create_macro(**arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Macro created", "macro": result}, indent=2))]

        elif name == "update_macro":
            if not arguments or arguments.get("macro_id") is None:
                raise ValueError("macro_id is required")
            macro_id = arguments.pop("macro_id")
            result = zendesk_client.update_macro(int(macro_id), **arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Macro updated", "macro": result}, indent=2))]

        elif name == "list_triggers":
            result = zendesk_client.list_triggers(
                page=(arguments or {}).get("page", 1),
                per_page=(arguments or {}).get("per_page", 100),
            )
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_trigger":
            if not arguments or arguments.get("trigger_id") is None:
                raise ValueError("trigger_id is required")
            result = zendesk_client.get_trigger(int(arguments["trigger_id"]))
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_trigger":
            if not arguments:
                raise ValueError("Missing arguments")
            result = zendesk_client.create_trigger(**arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Trigger created", "trigger": result}, indent=2))]

        elif name == "update_trigger":
            if not arguments or arguments.get("trigger_id") is None:
                raise ValueError("trigger_id is required")
            trigger_id = arguments.pop("trigger_id")
            result = zendesk_client.update_trigger(int(trigger_id), **arguments)
            return [types.TextContent(type="text", text=json.dumps({"message": "Trigger updated", "trigger": result}, indent=2))]
```

- [ ] **Step 3: Run the full pure-helper suite + import check**

Run: `cd /Users/alex/code/zendesk-mcp-server && uv run pytest -q && uv run python -c "import ast; ast.parse(open('src/zendesk_mcp_server/server.py').read()); print('server.py parses')"`
Expected: all tests pass; `server.py parses`.

- [ ] **Step 4: Live verification (manual, after `/mcp` reconnect)**

Reconnect the MCP (`/mcp` in Claude Code), then run a non-destructive read first, then a throwaway create:
- `list_macros` (per_page 5) → returns macros with `actions` projected.
- `create_ticket_field` type `text`, title `zz-test-delete-me` → returns the new field id; confirm in Admin Center; deactivate manually (no delete tool by design).
Expected: both succeed; 422s (if any) show Zendesk's reason text in the `Error:` output.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/code/zendesk-mcp-server
git add src/zendesk_mcp_server/server.py
git commit -m "feat: expose 12 config-write tools (fields, forms, macros, triggers)"
```

---

### Task 7: Code review, docs, and tracker update

**Files:**
- Repo: review fixes as needed.
- Vault: `DEHMANI-2/01-projects/zendesk-mcp-toolkit/to-ingest/docs/config-write-tools.md` (create), `DEHMANI-2/01-projects/zendesk-mcp-toolkit/index.md` (modify).

- [ ] **Step 1: Run `/code-review --effort high`** on the branch diff (per toolkit cadence). Resolve real findings in a fix-up commit; note cleanup-only findings.

- [ ] **Step 2: Author the wiki-feed reference doc** `to-ingest/docs/config-write-tools.md` documenting the 12 tools — endpoints, payload envelopes, the PUT-replace semantics for macros/triggers/field-options, the tagger-needs-options rule, and the returned projections. Follow the format of the existing `to-ingest/docs/*.md` (frontmatter with `tags:` from the closed taxonomy: `[zendesk, api, macros, business-rules, ticketing]`).

- [ ] **Step 3: Update `zendesk-mcp-toolkit/index.md`:** mark Tier 3 reads (`list_ticket_forms`, `list_ticket_fields`) and Tier 6 (`list_macros`, `list_triggers`) shipped; add a **"Tier: Config writes (shipped)"** section listing the 12 tools with fork commits; append a `## Log` line dated 2026-06-24.

- [ ] **Step 4: Commit the repo doc** (if any repo-side doc added) and note the vault edits are tracked in the vault (OneDrive, non-git).

```bash
cd /Users/alex/code/zendesk-mcp-server
git add -A && git commit -m "docs: config-write tools reference + review fix-ups" || echo "no repo doc changes"
```

- [ ] **Step 5: Acceptance — build the equipment-request objects** via the new tools (the real proof, per spec): create the Ship-to Address field; update Hardware form `49422106190107` to the specified field order; create the intake macro; create the email trigger. Update `zendesk-equipment-request/index.md` success-criteria checkboxes and remove its "Build constraint" once unblocked.

---

## Self-Review

**Spec coverage:**
- `_send_json` direct-REST approach → Task 1. ✅
- 12 tools (fields create/update, forms create/update, macros list/get/create/update, triggers list/get/create/update) → Tasks 2–6. ✅
- Pure builders + tagger-options validation → Tasks 2–5 tests. ✅
- Projections `_MACRO_FIELDS`/`_TRIGGER_FIELDS` → Tasks 4–5. ✅
- Safety: no delete (absent from all tasks), PUT-replace called out in tool descriptions (Task 6) and method docstrings (Tasks 4–5). ✅
- Error handling: `_send_json` surfaces body (Task 1), dispatcher wraps `Error:` (existing pattern, reused Task 6). ✅
- Testing: pure unit tests per task; live battery (Task 6 Step 4); equipment-request acceptance (Task 7 Step 5). ✅
- Docs/tracker (to-ingest doc, index.md) → Task 7. ✅

**Placeholder scan:** No TBD/TODO; all code steps carry full code; no "similar to Task N" cross-refs. ✅

**Type consistency:** Builder names (`_build_ticket_field_payload`, `_build_ticket_form_payload`, `_build_macro_payload`, `_build_trigger_payload`) and method signatures match between definition (Tasks 2–5) and dispatch (Task 6). `update_*` dispatch uses `arguments.pop(<id>)` then `**arguments`, matching the `(id, **fields)` method signatures. Projections referenced (`_TICKET_FIELD_FIELDS`, `_TICKET_FORM_FIELDS`, `_MACRO_FIELDS`, `_TRIGGER_FIELDS`) are all defined (first two pre-exist; latter two added Tasks 4–5). ✅
