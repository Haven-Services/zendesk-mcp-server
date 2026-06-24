"""Tests for the pure, network-free helpers in zendesk_client.

These import directly from zendesk_mcp_server.zendesk_client. We deliberately do
NOT import zendesk_mcp_server.server — that module constructs a ZendeskClient at
import time, which needs env vars. The functions under test here are pure:
no network, no env, no mocks.
"""
import io
import json as _json
import urllib.error
from contextlib import contextmanager

import pytest

from zendesk_mcp_server.zendesk_client import (
    _SHOW_MANY_MAX_IDS,
    _build_ticket_field_payload,
    _build_ticket_form_payload,
    _build_macro_payload,
    _coerce_id_list,
    _coerce_pagination,
    _compose_zql,
    _project,
    _zql_format_value,
    _zql_separator,
    ZendeskClient,
)


# --- _zql_format_value -------------------------------------------------------

class TestZqlFormatValue:
    def test_bool_renders_lowercase(self):
        # bool must be checked before int (bool is an int subclass)
        assert _zql_format_value(True) == "true"
        assert _zql_format_value(False) == "false"

    def test_plain_int_unquoted(self):
        assert _zql_format_value(123) == "123"

    def test_plain_word_unquoted(self):
        assert _zql_format_value("open") == "open"

    def test_space_value_quoted(self):
        assert _zql_format_value("needs review") == '"needs review"'

    def test_tab_value_quoted(self):
        assert _zql_format_value("a\tb") == '"a\tb"'

    def test_embedded_quote_escaped_and_quoted(self):
        assert _zql_format_value('say "hi"') == '"say \\"hi\\""'

    def test_bare_backslash_not_quoted(self):
        # The quoting predicate triggers only on space/tab/quote — a lone
        # backslash with none of those is left as-is.
        assert _zql_format_value("a\\b") == "a\\b"


# --- _zql_separator ----------------------------------------------------------

class TestZqlSeparator:
    def test_plain_key_colon(self):
        assert _zql_separator("status") == ":"

    def test_single_char_operator_suffix(self):
        assert _zql_separator("status<") == ""
        assert _zql_separator("created>") == ""

    def test_two_char_operator_suffix(self):
        assert _zql_separator("updated<=") == ""
        assert _zql_separator("due>=") == ""

    def test_empty_key_colon(self):
        assert _zql_separator("") == ":"

    def test_no_false_operator_match(self):
        assert _zql_separator("prototype") == ":"


# --- _compose_zql ------------------------------------------------------------

class TestComposeZql:
    def test_none_query_yields_only_type(self):
        assert _compose_zql(None, None, object_type="ticket") == "type:ticket"

    def test_raw_query_gets_type_prefix(self):
        assert _compose_zql("assignee:123", None, object_type="ticket") == "type:ticket assignee:123"

    def test_existing_type_not_re_prefixed(self):
        assert _compose_zql("type:user foo", None, object_type="ticket") == "type:user foo"

    def test_prototype_substring_still_prefixed(self):
        # word-boundary check, not substring — prototype: is not a type: filter
        assert _compose_zql("prototype:x", None, object_type="ticket") == "type:ticket prototype:x"

    def test_operator_suffix_filter_no_colon(self):
        assert _compose_zql(None, {"status<": "solved"}, object_type="ticket") == "type:ticket status<solved"

    def test_list_value_expands(self):
        assert _compose_zql(None, {"tags": ["vpn", "wifi"]}, object_type="ticket") == "type:ticket tags:vpn tags:wifi"

    def test_empty_list_skipped(self):
        assert _compose_zql(None, {"tags": []}, object_type="ticket") == "type:ticket"

    def test_none_and_empty_string_filters_skipped(self):
        assert _compose_zql(None, {"a": None, "b": ""}, object_type="ticket") == "type:ticket"

    def test_list_with_none_and_empty_items_skipped(self):
        result = _compose_zql(None, {"tags": ["vpn", None, "", "wifi"]}, object_type="ticket")
        assert result == "type:ticket tags:vpn tags:wifi"

    def test_space_value_quoted(self):
        assert _compose_zql(None, {"subject": "needs review"}, object_type="ticket") == 'type:ticket subject:"needs review"'

    def test_bool_filter_lowercase(self):
        assert _compose_zql(None, {"shared": True}, object_type="ticket") == "type:ticket shared:true"

    def test_non_string_non_none_query_raises(self):
        with pytest.raises(TypeError):
            _compose_zql(123, None, object_type="ticket")

    def test_whitespace_only_query_stripped(self):
        assert _compose_zql("   ", None, object_type="ticket") == "type:ticket"

    def test_no_object_type_no_prefix(self):
        assert _compose_zql("assignee:123", None, object_type=None) == "assignee:123"


# --- _coerce_pagination ------------------------------------------------------

class TestCoercePagination:
    def test_defaults(self):
        assert _coerce_pagination(None, None) == (1, 25)

    def test_clamp_high_per_page(self):
        assert _coerce_pagination(1, 500) == (1, 100)

    def test_custom_max_per_page(self):
        assert _coerce_pagination(1, 500, max_per_page=50) == (1, 50)

    def test_clamp_low(self):
        assert _coerce_pagination(0, 0) == (1, 1)

    def test_negative_page_clamped(self):
        assert _coerce_pagination(-3, 10) == (1, 10)

    def test_bool_page_rejected(self):
        with pytest.raises(TypeError):
            _coerce_pagination(True, 10)

    def test_bool_per_page_rejected(self):
        with pytest.raises(TypeError):
            _coerce_pagination(1, False)

    def test_custom_default_per_page(self):
        assert _coerce_pagination(1, None, default_per_page=10) == (1, 10)

    def test_string_ints_cast(self):
        assert _coerce_pagination("2", "50") == (2, 50)


# --- _coerce_id_list ---------------------------------------------------------

class TestCoerceIdList:
    def test_list_of_ints(self):
        assert _coerce_id_list([1, 2, 3]) == [1, 2, 3]

    def test_list_of_int_strings(self):
        assert _coerce_id_list(["1", "2"]) == [1, 2]

    def test_comma_separated_string(self):
        assert _coerce_id_list("1, 2,3") == [1, 2, 3]

    def test_none_raises(self):
        with pytest.raises(ValueError):
            _coerce_id_list(None)

    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            _coerce_id_list([])

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _coerce_id_list("")

    def test_bool_item_rejected(self):
        with pytest.raises(TypeError):
            _coerce_id_list([1, True])

    def test_wrong_type_rejected(self):
        with pytest.raises(TypeError):
            _coerce_id_list(42)

    def test_over_cap_raises(self):
        with pytest.raises(ValueError):
            _coerce_id_list(list(range(_SHOW_MANY_MAX_IDS + 1)))

    def test_at_cap_ok(self):
        ids = list(range(_SHOW_MANY_MAX_IDS))
        assert _coerce_id_list(ids) == ids


# --- _project ----------------------------------------------------------------

class TestProject:
    def test_subset_selection(self):
        record = {"id": 1, "name": "x", "extra": "drop"}
        assert _project(record, ("id", "name")) == {"id": 1, "name": "x"}

    def test_missing_key_is_none(self):
        assert _project({"id": 1}, ("id", "name")) == {"id": 1, "name": None}

    def test_extra_keys_dropped(self):
        assert _project({"id": 1, "junk": 2}, ("id",)) == {"id": 1}

    def test_empty_fields_yields_empty_dict(self):
        assert _project({"id": 1}, ()) == {}


# --- _send_json (POST/PUT transport) -----------------------------------------

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


# --- _build_ticket_field_payload -----------------------------------------

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


# --- _build_ticket_form_payload ------------------------------------------

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


# --- _build_macro_payload ---------------------------------------------------

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
