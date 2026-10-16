"""Tests for the pure, network-free helpers in zendesk_client.

These import directly from zendesk_mcp_server.zendesk_client. We deliberately do
NOT import zendesk_mcp_server.server — that module constructs a ZendeskClient at
import time, which needs env vars. The functions under test here are pure:
no network, no env, no mocks.
"""
import pytest

from zendesk_mcp_server.zendesk_client import (
    _SHOW_MANY_MAX_IDS,
    _coerce_id_list,
    _coerce_pagination,
    _compose_zql,
    _project,
    _validate_attachment_url,
    _zql_format_value,
    _zql_separator,
)


# --- _validate_attachment_url -----------------------------------------------

class TestValidateAttachmentUrl:
    def test_accepts_configured_tenant_https_url(self):
        url = "https://test.zendesk.com/attachments/token/abc/?name=image.png"
        assert _validate_attachment_url(url, "test") == url

    @pytest.mark.parametrize("url", [
        "https://attacker.example/image.png",
        "https://test.zendesk.com.attacker.example/image.png",
        "http://test.zendesk.com/image.png",
        "https://test.zendesk.com:444/image.png",
        "https://attacker.example@test.zendesk.com/image.png",
    ])
    def test_rejects_url_that_must_not_receive_credentials(self, url):
        with pytest.raises(ValueError, match="must use HTTPS"):
            _validate_attachment_url(url, "test")


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
