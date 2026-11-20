from typing import Dict, Any, List, Tuple
import json
import re
import urllib.request
import urllib.parse
import urllib.error
import base64
import requests as _requests

from markdown_it import MarkdownIt
from zenpy import Zenpy
from zenpy.lib.api_objects import Comment
from zenpy.lib.api_objects import Ticket as ZenpyTicket


# Field projections — single source of truth so the same record type returns the
# same keys whether it's surfaced as a primary result or as a sideload.
_TICKET_SUMMARY_FIELDS: Tuple[str, ...] = (
    'id', 'subject', 'status', 'priority', 'type',
    'created_at', 'updated_at',
    'requester_id', 'assignee_id', 'tags',
)
_TICKET_FULL_FIELDS: Tuple[str, ...] = _TICKET_SUMMARY_FIELDS + (
    'description', 'ticket_form_id', 'brand_id', 'group_id',
    'organization_id', 'custom_fields',
)
_USER_FIELDS: Tuple[str, ...] = (
    'id', 'name', 'email', 'role', 'active', 'organization_id', 'created_at',
)
_ORGANIZATION_FIELDS: Tuple[str, ...] = (
    'id', 'name', 'created_at',
)
_GROUP_FIELDS: Tuple[str, ...] = (
    'id', 'name', 'description', 'default', 'deleted', 'created_at',
)
_BRAND_FIELDS: Tuple[str, ...] = (
    'id', 'name', 'brand_url', 'subdomain', 'active', 'default',
)
_TICKET_FORM_FIELDS: Tuple[str, ...] = (
    'id', 'name', 'display_name', 'active', 'default', 'end_user_visible',
    'ticket_field_ids', 'end_user_conditions', 'agent_conditions',
)
_TICKET_FIELD_FIELDS: Tuple[str, ...] = (
    'id', 'type', 'title', 'active', 'custom_field_options',
)
# Each custom_field_options entry on a tagger/multiselect ticket field projects
# down to this — the join key for interpreting a ticket's custom_fields array.
_CUSTOM_FIELD_OPTION_FIELDS: Tuple[str, ...] = ('id', 'name', 'value')
_MACRO_FIELDS: Tuple[str, ...] = (
    'id', 'title', 'active', 'restriction', 'actions', 'created_at', 'updated_at',
)
_MACRO_LIST_FIELDS: Tuple[str, ...] = (
    'id', 'title', 'active', 'restriction', 'created_at', 'updated_at',
)  # no 'actions' — call get_macro for the full actions array
_TRIGGER_FIELDS: Tuple[str, ...] = (
    'id', 'title', 'active', 'conditions', 'actions', 'category_id',
    'created_at', 'updated_at',
)
_TRIGGER_LIST_FIELDS: Tuple[str, ...] = (
    'id', 'title', 'active', 'category_id', 'created_at', 'updated_at',
)  # no 'conditions', no 'actions' — call get_trigger for the full sets

_HC_SECTION_FIELDS: Tuple[str, ...] = (
    'id', 'name', 'description', 'category_id', 'locale', 'html_url',
)
_HC_USER_SEGMENT_FIELDS: Tuple[str, ...] = ('id', 'name', 'user_type', 'built_in')
_HC_PERMISSION_GROUP_FIELDS: Tuple[str, ...] = ('id', 'name', 'built_in')
_HC_ARTICLE_LIST_FIELDS: Tuple[str, ...] = (
    'id', 'title', 'draft', 'section_id', 'user_segment_id', 'permission_group_id',
    'label_names', 'locale', 'html_url', 'created_at', 'updated_at',
)  # no 'body' — call get_help_center_article for the full HTML
_HC_ARTICLE_FIELDS: Tuple[str, ...] = _HC_ARTICLE_LIST_FIELDS + ('body',)

_TICKET_FIELD_TYPES = frozenset({
    'text', 'textarea', 'checkbox', 'date', 'integer', 'decimal',
    'regexp', 'multiselect', 'tagger',
})
_OPTION_REQUIRED_FIELD_TYPES = frozenset({'tagger', 'multiselect'})

# The agent-only multi-line ticket field that holds a reply drafted for review.
_DRAFT_REPLY_FIELD_TITLE = 'AI draft reply'
_DRAFT_REPLY_MAX_CHARS = 8000

# Zendesk Search API hard cap. page * per_page > 1000 returns HTTP 422.
_SEARCH_RESULT_CAP = 1000

# show_many endpoints accept at most 100 comma-joined ids per request.
_SHOW_MANY_MAX_IDS = 100


def _validate_attachment_url(content_url: str, subdomain: str) -> str:
    """Allow authenticated attachment requests only to this Zendesk tenant."""
    if not isinstance(content_url, str) or not content_url:
        raise ValueError("Attachment URL must be a non-empty string.")

    try:
        parsed = urllib.parse.urlsplit(content_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Attachment URL is invalid.") from exc

    expected_host = f"{subdomain}.zendesk.com".lower()
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != expected_host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            f"Attachment URL must use HTTPS on {expected_host}."
        )

    return content_url


def _project(record: Dict[str, Any], fields: Tuple[str, ...]) -> Dict[str, Any]:
    return {k: record.get(k) for k in fields}


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


_TICKET_FORM_CONDITION_KEYS: Tuple[str, ...] = ('end_user_conditions', 'agent_conditions')


def _validate_ticket_form_conditions(key: str, conditions: Any) -> None:
    """Check the shape of a conditional-field list for a ticket form.

    Each condition is {'parent_field_id': int, 'value': <parent value>,
    'child_fields': [{'id': int, ...}, ...]}: when the parent field holds the
    value, the child fields are shown. An empty list is valid and clears the
    form's conditions."""
    if not isinstance(conditions, (list, tuple)):
        raise TypeError(f"{key} must be a list of conditions")
    for index, condition in enumerate(conditions):
        where = f"{key}[{index}]"
        if not isinstance(condition, dict):
            raise TypeError(f"{where} must be an object")
        if condition.get('parent_field_id') is None:
            raise ValueError(f"{where} requires parent_field_id")
        if 'value' not in condition:
            raise ValueError(f"{where} requires value")
        children = condition.get('child_fields')
        if not isinstance(children, (list, tuple)) or not children:
            raise ValueError(f"{where} requires a non-empty child_fields list")
        for child in children:
            if not isinstance(child, dict) or child.get('id') is None:
                raise ValueError(f"{where} child_fields entries require id")


def _build_ticket_form_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the {'ticket_form': {...}} envelope, dropping None values. A
    supplied ticket_field_ids list is ordered and replaces the form's field set.
    A supplied end_user_conditions or agent_conditions list replaces that
    condition set."""
    body = {k: v for k, v in fields.items() if v is not None}
    field_ids = body.get('ticket_field_ids')
    if field_ids is not None and not isinstance(field_ids, (list, tuple)):
        raise TypeError("ticket_field_ids must be a list of field IDs")
    for key in _TICKET_FORM_CONDITION_KEYS:
        if key in body:
            _validate_ticket_form_conditions(key, body[key])
    return {'ticket_form': body}


def _build_macro_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the {'macro': {...}} envelope, dropping None values. A supplied
    actions list REPLACES the macro's entire action set (no partial update)."""
    body = {k: v for k, v in fields.items() if v is not None}
    actions = body.get('actions')
    if actions is not None and not isinstance(actions, list):
        raise TypeError("macro actions must be a list of {field, value} dicts")
    return {'macro': body}


def _build_trigger_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the {'trigger': {...}} envelope, dropping None values. Supplied
    conditions ({all, any}) and actions (list) REPLACE the existing sets wholesale."""
    body = {k: v for k, v in fields.items() if v is not None}
    conditions = body.get('conditions')
    if conditions is not None and not isinstance(conditions, dict):
        raise TypeError("trigger conditions must be a dict with 'all' and/or 'any' lists")
    actions = body.get('actions')
    if actions is not None and not isinstance(actions, list):
        raise TypeError("trigger actions must be a list of {field, value} dicts")
    return {'trigger': body}


def _build_article_draft_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the create-article body for a DRAFT. `draft` is always True and
    `notify_subscribers` always False, whatever the caller sent: this server has
    no way to publish. A caller-supplied `draft` is rejected rather than
    silently overridden, so a request to publish fails loudly. The audience is
    an explicit user segment; "everyone" (a null user_segment_id) is refused."""
    if 'draft' in fields:
        raise ValueError(
            "draft cannot be set: articles are always created as drafts; publish in Zendesk Guide"
        )
    body = {k: v for k, v in fields.items() if v is not None}
    for key in ('title', 'body'):
        if not isinstance(body.get(key), str) or not body[key].strip():
            raise ValueError(f"{key} is required and must be a non-empty string")
    for key in ('user_segment_id', 'permission_group_id'):
        value = body.get(key)
        if isinstance(value, bool) or value is None:
            raise ValueError(
                f"{key} is required: pick an id from list_help_center_"
                f"{'user_segments' if key == 'user_segment_id' else 'permission_groups'}"
            )
        body[key] = int(value)
    labels = body.get('label_names')
    if labels is not None and not (
        isinstance(labels, list) and all(isinstance(x, str) for x in labels)
    ):
        raise TypeError("label_names must be a list of strings")
    body.setdefault('locale', 'en-us')
    body['draft'] = True
    return {'article': body, 'notify_subscribers': False}


def _build_article_translation_payload(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the update-translation body for a draft article: title and/or body,
    and always `draft: True` so the write can never publish."""
    if 'draft' in fields:
        raise ValueError(
            "draft cannot be set: this server only edits drafts; publish in Zendesk Guide"
        )
    body = {k: v for k, v in fields.items() if k in ('title', 'body') and v is not None}
    if not body:
        raise ValueError("nothing to update: give title and/or body")
    for key, value in body.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
    body['draft'] = True
    return {'translation': body}


def _find_draft_reply_field(ticket_fields: List[Dict[str, Any]], title: str) -> int:
    """Return the id of the one active textarea ticket field titled `title`.
    Raises when it is missing or ambiguous, so a draft is never written to a
    field picked by guesswork."""
    matches = [
        f for f in ticket_fields
        if f.get('title') == title and f.get('type') == 'textarea' and f.get('active')
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one active textarea ticket field titled {title!r}, found {len(matches)}"
        )
    return int(matches[0]['id'])


def _build_draft_reply_payload(field_id: int, draft: Any) -> Dict[str, Any]:
    """Build a ticket update that touches ONLY the draft-reply field: no comment,
    no status, no assignee. An empty string clears the draft."""
    if not isinstance(draft, str):
        raise TypeError("draft must be a string (empty to clear)")
    if len(draft) > _DRAFT_REPLY_MAX_CHARS:
        raise ValueError(f"draft is {len(draft)} characters; the limit is {_DRAFT_REPLY_MAX_CHARS}")
    return {'ticket': {'custom_fields': [{'id': int(field_id), 'value': draft}]}}


def _zql_format_value(v: Any) -> str:
    """Format a Python value as a ZQL operand. Bools → ZQL lowercase. Values with
    whitespace or embedded quotes get double-quoted (and embedded quotes escaped)."""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    s = str(v)
    if any(c in s for c in ' \t"'):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def _zql_separator(key: str) -> str:
    """Return the separator between a ZQL field name and its operand. Keys carrying
    a comparison-operator suffix (status<, created>, updated<=, due>=) take no
    extra ':' — the operator already separates field from value."""
    if key.endswith(('<=', '>=')):
        return ''
    if key and key[-1] in '<>':
        return ''
    return ':'


def _compose_zql(
    query: Any,
    extra_filters: Dict[str, Any] | None,
    object_type: str | None,
) -> str:
    """Compose a Zendesk Search query from raw `query` + structured `extra_filters`.

    Adds `type:<object_type>` only when the composed query has no top-level `type:`
    filter (word-boundary check — substrings like `prototype:` don't count).
    Empty-string and None values are skipped.
    """
    if query is None:
        raw = ''
    elif isinstance(query, str):
        raw = query.strip()
    else:
        raise TypeError(f"query must be a string, got {type(query).__name__}")

    parts: List[str] = []
    if raw:
        parts.append(raw)

    for k, v in (extra_filters or {}).items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            sep = _zql_separator(k)
            for item in v:
                if item is None or (isinstance(item, str) and not item):
                    continue
                parts.append(f"{k}{sep}{_zql_format_value(item)}")
        else:
            if isinstance(v, str) and not v:
                continue
            sep = _zql_separator(k)
            parts.append(f"{k}{sep}{_zql_format_value(v)}")

    composed = ' '.join(parts)
    if object_type and not re.search(r'(^|\s)type:', composed):
        composed = f"type:{object_type} {composed}".strip()
    return composed


def _coerce_pagination(
    page: Any,
    per_page: Any,
    *,
    default_per_page: int = 25,
    max_per_page: int = 100,
) -> Tuple[int, int]:
    """Coerce page/per_page to clamped ints. Rejects bool (which would otherwise
    pass `isinstance(x, int)` since bool is a subclass). page < 1 → 1; per_page
    above max → max; per_page < 1 → 1."""
    if isinstance(page, bool) or isinstance(per_page, bool):
        raise TypeError("page and per_page must be integers, not bool")
    page_int = 1 if page is None else int(page)
    per_page_int = default_per_page if per_page is None else int(per_page)
    return max(1, page_int), max(1, min(per_page_int, max_per_page))


def _coerce_id_list(ids: Any, *, field: str = 'ids') -> List[int]:
    """Coerce a caller-supplied id collection to a list of ints for a show_many
    request. Accepts a list/tuple of ints (or int-like strings) or a single
    comma-separated string. Rejects bool (subclass of int), rejects empty, and
    *raises* — never silently truncates — when more than _SHOW_MANY_MAX_IDS are
    requested, since dropping ids would lose records the caller asked for
    (unlike pagination, which clamps to a still-valid smaller page)."""
    if ids is None:
        raise ValueError(f"{field} is required")
    if isinstance(ids, str):
        items: List[Any] = [s.strip() for s in ids.split(',') if s.strip()]
    elif isinstance(ids, (list, tuple)):
        items = list(ids)
    else:
        raise TypeError(
            f"{field} must be a list of integers or a comma-separated string, "
            f"got {type(ids).__name__}"
        )

    coerced: List[int] = []
    for item in items:
        if isinstance(item, bool):
            raise TypeError(f"{field} values must be integers, not bool")
        coerced.append(int(item))

    if not coerced:
        raise ValueError(f"{field} must contain at least one id")
    if len(coerced) > _SHOW_MANY_MAX_IDS:
        raise ValueError(
            f"{field} accepts at most {_SHOW_MANY_MAX_IDS} ids per request "
            f"(got {len(coerced)}); batch the ids into chunks of "
            f"{_SHOW_MANY_MAX_IDS} or fewer."
        )
    return coerced


# CommonMark, with single newlines kept as <br> and inline HTML passed through, so
# plain text, markdown and HTML all keep their structure once sent as html_body.
_COMMENT_MD = MarkdownIt("commonmark", {"breaks": True, "html": True})


def _comment_html(comment: str, format: str = "markdown") -> str:
    """Render a comment to the HTML Zendesk stores as html_body."""
    if format == "html":
        return comment
    if format == "markdown":
        return _COMMENT_MD.render(comment)
    raise ValueError(f"format must be 'markdown' or 'html', got {format!r}")


class ZendeskClient:
    def __init__(self, subdomain: str, email: str, token: str):
        """
        Initialize the Zendesk client using zenpy lib and direct API.
        """
        self.client = Zenpy(
            subdomain=subdomain,
            email=email,
            token=token
        )

        # For direct API calls
        self.subdomain = subdomain
        self.email = email
        self.token = token
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        # Create basic auth header
        credentials = f"{email}/token:{token}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode('ascii')
        self.auth_header = f"Basic {encoded_credentials}"

    # --- shared HTTP path -----------------------------------------------------

    def _get_json(
        self,
        path: str,
        params: Dict[str, str],
        *,
        error_context: str,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Authenticated GET against the Zendesk REST API. Returns decoded JSON.

        HTTP errors are re-raised as `Exception` carrying the response body — the
        body is where Zendesk explains the failure (e.g. 422 with reasons). All
        other exceptions (URLError, JSONDecodeError, etc.) propagate with their
        original type so the traceback survives for debugging.
        """
        url = f"{self.base_url}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)
        req.add_header('Authorization', self.auth_header)
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='replace') if e.fp else "No response body"
            raise Exception(f"Failed to {error_context}: HTTP {e.code} - {e.reason}. {error_body}")

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

    # --- Zenpy-backed methods (unchanged shape) -------------------------------

    def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
        """
        Query a ticket by its ID
        """
        try:
            ticket = self.client.tickets(id=ticket_id)
            return {
                'id': ticket.id,
                'subject': ticket.subject,
                'description': ticket.description,
                'status': ticket.status,
                'priority': ticket.priority,
                'created_at': str(ticket.created_at),
                'updated_at': str(ticket.updated_at),
                'requester_id': ticket.requester_id,
                'assignee_id': ticket.assignee_id,
                'organization_id': ticket.organization_id
            }
        except Exception as e:
            raise Exception(f"Failed to get ticket {ticket_id}: {str(e)}")

    def get_ticket_comments(self, ticket_id: int) -> List[Dict[str, Any]]:
        """
        Get all comments for a specific ticket, including attachment metadata.
        """
        try:
            comments = self.client.tickets.comments(ticket=ticket_id)
            result = []
            for comment in comments:
                attachments = []
                for a in getattr(comment, 'attachments', []) or []:
                    attachments.append({
                        'id': a.id,
                        'file_name': a.file_name,
                        'content_url': a.content_url,
                        'content_type': a.content_type,
                        'size': a.size,
                    })
                result.append({
                    'id': comment.id,
                    'author_id': comment.author_id,
                    'body': comment.body,
                    'html_body': comment.html_body,
                    'public': comment.public,
                    'created_at': str(comment.created_at),
                    'attachments': attachments,
                })
            return result
        except Exception as e:
            raise Exception(f"Failed to get comments for ticket {ticket_id}: {str(e)}")

    # Allowed image MIME types. SVG is excluded — it can contain active XML/JS content.
    _ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}

    # Magic bytes (file signatures) for each allowed type.
    _MAGIC_BYTES: Dict[str, List[bytes]] = {
        'image/jpeg': [b'\xff\xd8\xff'],
        'image/png':  [b'\x89PNG\r\n\x1a\n'],
        'image/gif':  [b'GIF87a', b'GIF89a'],
        'image/webp': [b'RIFF'],  # RIFF....WEBP — checked further below
    }

    # 10 MB hard cap to guard against image bombs and token budget blowout.
    _MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

    def get_ticket_attachment(self, content_url: str) -> Dict[str, Any]:
        """
        Fetch an image attachment and return base64-encoded data.

        Security measures applied:
        - Only the configured tenant's HTTPS host receives Zendesk credentials.
        - Allowlist of safe image MIME types (no SVG or arbitrary binary).
        - Magic byte validation so the file header must match the declared type.
        - 10 MB size cap to prevent image bombs and excessive token usage.

        Zendesk attachment URLs redirect to zdusercontent.com (Zendesk's CDN).
        requests strips the Authorization header on cross-origin redirects,
        which is required — the CDN returns 403 if it receives an auth header.
        """
        try:
            validated_url = _validate_attachment_url(content_url, self.subdomain)
            response = _requests.get(
                validated_url,
                headers={'Authorization': self.auth_header},
                timeout=30,
                stream=True,
            )
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()

            if content_type not in self._ALLOWED_IMAGE_TYPES:
                raise ValueError(
                    f"Attachment type '{content_type}' is not allowed. "
                    f"Supported types: {sorted(self._ALLOWED_IMAGE_TYPES)}"
                )

            # Read with size cap — stops download as soon as limit is exceeded.
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > self._MAX_ATTACHMENT_BYTES:
                    raise ValueError(
                        f"Attachment exceeds the {self._MAX_ATTACHMENT_BYTES // (1024*1024)} MB size limit."
                    )
                chunks.append(chunk)
            content = b''.join(chunks)

            # Validate magic bytes to catch MIME type spoofing.
            magic_signatures = self._MAGIC_BYTES.get(content_type, [])
            if magic_signatures and not any(content.startswith(sig) for sig in magic_signatures):
                raise ValueError(
                    f"File header does not match declared content type '{content_type}'. "
                    "The attachment may be spoofed."
                )
            # Extra check for WebP: bytes 8–12 must be b'WEBP'.
            if content_type == 'image/webp' and content[8:12] != b'WEBP':
                raise ValueError("File header does not match declared content type 'image/webp'.")

            return {
                'data': base64.b64encode(content).decode('ascii'),
                'content_type': content_type,
            }
        except (ValueError, _requests.HTTPError):
            raise
        except Exception as e:
            raise Exception(f"Failed to fetch attachment from {content_url}: {str(e)}")

    def post_comment(self, ticket_id: int, comment: str, public: bool = True,
                     format: str = "markdown") -> str:
        """
        Post a comment to an existing ticket. `format` is "markdown" (rendered to
        HTML here) or "html" (sent as-is); either way Zendesk receives html_body.
        """
        html_body = _comment_html(comment, format)
        try:
            ticket = self.client.tickets(id=ticket_id)
            ticket.comment = Comment(
                html_body=html_body,
                public=public
            )
            self.client.tickets.update(ticket)
            return comment
        except Exception as e:
            raise Exception(f"Failed to post comment on ticket {ticket_id}: {str(e)}")

    # --- list/search via direct REST ------------------------------------------

    def get_tickets(
        self,
        page: int = 1,
        per_page: int = 25,
        sort_by: str = 'created_at',
        sort_order: str = 'desc',
    ) -> Dict[str, Any]:
        """
        Get the latest tickets with offset pagination via /tickets.json.

        Args:
            page: Page number (1-based; <1 clamped).
            per_page: Results per page (1-100; out-of-range clamped).
            sort_by: created_at, updated_at, priority, status.
            sort_order: asc or desc.
        """
        page, per_page = _coerce_pagination(page, per_page)
        params = {
            'page': str(page),
            'per_page': str(per_page),
            'sort_by': sort_by,
            'sort_order': sort_order,
        }
        data = self._get_json('tickets.json', params, error_context='get latest tickets')

        ticket_list = [
            {
                'id': t.get('id'),
                'subject': t.get('subject'),
                'status': t.get('status'),
                'priority': t.get('priority'),
                'description': t.get('description'),
                'created_at': t.get('created_at'),
                'updated_at': t.get('updated_at'),
                'requester_id': t.get('requester_id'),
                'assignee_id': t.get('assignee_id'),
            }
            for t in data.get('tickets', [])
        ]

        return {
            'tickets': ticket_list,
            'page': page,
            'per_page': per_page,
            'count': len(ticket_list),
            'sort_by': sort_by,
            'sort_order': sort_order,
            'has_more': data.get('next_page') is not None,
            'next_page': page + 1 if data.get('next_page') else None,
            'previous_page': page - 1 if data.get('previous_page') and page > 1 else None,
        }

    def search_users(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """
        Search users via /api/v2/users/search.json. Zendesk decides what to match
        on (email, name, phone); secondary emails are included in the search.

        Args:
            query: Search text (email, name fragment, phone number).
            limit: Max users to return (1..100).
        """
        if not query:
            raise ValueError("query is required")
        _, limit = _coerce_pagination(1, limit, default_per_page=10)
        params = {'query': query, 'per_page': str(limit)}
        data = self._get_json('users/search.json', params, error_context='search users')
        users = [_project(u, _USER_FIELDS) for u in data.get('users', [])[:limit]]
        return {'query': query, 'count': len(users), 'users': users}

    def search_tickets(
        self,
        query: str,
        extra_filters: Dict[str, Any] | None = None,
        fields: str = 'summary',
        page: int = 1,
        per_page: int = 25,
        sort_by: str = 'updated_at',
        sort_order: str = 'desc',
    ) -> Dict[str, Any]:
        """
        Search tickets via /api/v2/search.json.

        Composes a Zendesk Search query from raw `query` plus optional structured
        `extra_filters`. Keys with operator suffixes (`status<`, `created>`,
        `updated<=`, `due>=`) emit ZQL without an extra `:`. String values
        containing whitespace are double-quoted. Booleans render as lowercase
        `true`/`false`. None and empty-string values are skipped. `type:ticket`
        is auto-prepended when no top-level `type:` filter is present in the
        composed query.

        Hard 1000-result cap enforced server-side: page * per_page > 1000 raises
        before any API call. The `truncated` flag in the response is True when
        the total exceeds the cap so callers can detect when results are
        unreachable rather than missing. Newly-created tickets take a few
        minutes to appear in the search index.

        Args:
            query: Raw Zendesk Search syntax (e.g. "assignee:123 status<solved").
            extra_filters: Structured filters merged onto `query`. Keys may
                carry operator suffixes (e.g. "status<": "solved").
            fields: "summary" (lean) or "full" (adds description + categorization
                fields). Matched case-insensitively.
            page: 1-based. <1 clamps to 1.
            per_page: 1..100. Out-of-range clamps.
            sort_by: updated_at, created_at, priority, status, ticket_type.
            sort_order: asc | desc.

        Note: sideload support (`?include=users,organizations,groups`) was
        tried and dropped — Zendesk's /search.json returns empty sideload
        arrays even when result rows reference users/orgs/groups. To resolve
        IDs to names, call `search_users` (or future metadata-listing tools)
        separately.
        """
        page, per_page = _coerce_pagination(page, per_page)
        if page * per_page > _SEARCH_RESULT_CAP:
            raise ValueError(
                f"Search API cap reached: page * per_page = {page * per_page} > {_SEARCH_RESULT_CAP}. "
                "Narrow the query with tags, date range, or brand_id."
            )

        normalized_fields = (fields or 'summary').strip().lower()
        if normalized_fields not in ('summary', 'full'):
            raise ValueError(f"fields must be 'summary' or 'full', got {fields!r}")

        composed = _compose_zql(query, extra_filters, object_type='ticket')

        params = {
            'query': composed,
            'page': str(page),
            'per_page': str(per_page),
            'sort_by': sort_by,
            'sort_order': sort_order,
        }

        data = self._get_json('search.json', params, error_context='search tickets')

        keys = _TICKET_FULL_FIELDS if normalized_fields == 'full' else _TICKET_SUMMARY_FIELDS
        tickets = []
        for t in data.get('results', []):
            row = _project(t, keys)
            row['via_channel'] = (t.get('via') or {}).get('channel')
            tickets.append(row)

        total = data.get('count', 0)
        result: Dict[str, Any] = {
            'query': query,
            'effective_query': composed,
            'page': page,
            'per_page': per_page,
            'count': len(tickets),
            'total': total,
            # has_more = "the API can serve another page". truncated = "there are
            # results past the 1000-row Search API cap that no caller can reach".
            # Branch on `truncated` for "did I miss results?"; branch on `has_more`
            # for "should I fetch page N+1?".
            'has_more': data.get('next_page') is not None,
            'truncated': total > _SEARCH_RESULT_CAP,
            'tickets': tickets,
        }
        if total > _SEARCH_RESULT_CAP:
            result['truncation_warning'] = (
                f"total ({total}) exceeds the {_SEARCH_RESULT_CAP}-result Search API cap; "
                "results past page 10 (per_page=100) are unreachable. Narrow the query."
            )

        return result

    # --- metadata lookups (resolve IDs / list config) -------------------------

    def get_users(self, ids: Any) -> Dict[str, Any]:
        """
        Resolve user IDs to records via /api/v2/users/show_many.json.

        Use to turn requester_id / assignee_id / author_id / collaborator_ids
        (from tickets, comments, and search results) into names and emails.

        Accepts up to 100 ids. Non-existent ids are silently omitted by Zendesk,
        so the returned set may be smaller than requested — compare returned ids
        to the ids you asked for to detect misses.
        """
        id_list = _coerce_id_list(ids)
        params = {'ids': ','.join(str(i) for i in id_list)}
        data = self._get_json('users/show_many.json', params, error_context='get users by id')
        users = [_project(u, _USER_FIELDS) for u in data.get('users', [])]
        return {'count': len(users), 'users': users}

    def get_organizations(self, ids: Any) -> Dict[str, Any]:
        """
        Resolve organization IDs to records via
        /api/v2/organizations/show_many.json.

        Use to turn organization_id (from tickets/users/search results) into org
        names. Accepts up to 100 ids. Non-existent ids are silently omitted.
        """
        id_list = _coerce_id_list(ids)
        params = {'ids': ','.join(str(i) for i in id_list)}
        data = self._get_json(
            'organizations/show_many.json', params,
            error_context='get organizations by id',
        )
        orgs = [_project(o, _ORGANIZATION_FIELDS) for o in data.get('organizations', [])]
        return {'count': len(orgs), 'organizations': orgs}

    def list_groups(self, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """
        List agent groups via /api/v2/groups.json. Use to resolve group_id to a
        group name. Group sets are small; one page is usually enough.
        """
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        data = self._get_json('groups.json', params, error_context='list groups')
        groups = [_project(g, _GROUP_FIELDS) for g in data.get('groups', [])]
        return {
            'page': page,
            'per_page': per_page,
            'count': len(groups),
            'has_more': data.get('next_page') is not None or len(groups) == per_page,
            'groups': groups,
        }

    def list_brands(self, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """
        List brands via /api/v2/brands.json (multi-brand orgs). Use to resolve
        brand_id to a brand name/subdomain. May use cursor pagination, in which
        case next_page is absent — has_more then falls back to a full page.
        """
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        data = self._get_json('brands.json', params, error_context='list brands')
        brands = [_project(b, _BRAND_FIELDS) for b in data.get('brands', [])]
        return {
            'page': page,
            'per_page': per_page,
            'count': len(brands),
            'has_more': data.get('next_page') is not None or len(brands) == per_page,
            'brands': brands,
        }

    def list_ticket_forms(self) -> Dict[str, Any]:
        """
        List ticket forms via /api/v2/ticket_forms.json. Use to resolve
        ticket_form_id to a form name. The endpoint returns all forms.
        """
        data = self._get_json('ticket_forms.json', {}, error_context='list ticket forms')
        forms = [_project(f, _TICKET_FORM_FIELDS) for f in data.get('ticket_forms', [])]
        return {'count': len(forms), 'ticket_forms': forms}

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

    def list_ticket_fields(self, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """
        List ticket fields via /api/v2/ticket_fields.json. Each field includes
        custom_field_options (id/name/value) for tagger/multiselect fields — the
        map needed to interpret a ticket's custom_fields array (each {id, value}
        joins to an option's value). May use cursor pagination.
        """
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        data = self._get_json('ticket_fields.json', params, error_context='list ticket fields')
        fields = []
        for f in data.get('ticket_fields', []):
            row = _project(f, _TICKET_FIELD_FIELDS)
            options = row.get('custom_field_options')
            if options:
                row['custom_field_options'] = [
                    _project(opt, _CUSTOM_FIELD_OPTION_FIELDS) for opt in options
                ]
            fields.append(row)
        return {
            'page': page,
            'per_page': per_page,
            'count': len(fields),
            'has_more': data.get('next_page') is not None or len(fields) == per_page,
            'ticket_fields': fields,
        }

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

    def list_macros(self, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """List macros via GET /api/v2/macros.json. Lean projection — call
        get_macro for a single macro's full actions array."""
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        data = self._get_json('macros.json', params, error_context='list macros')
        macros = [_project(m, _MACRO_LIST_FIELDS) for m in data.get('macros', [])]
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

    def list_triggers(self, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """List triggers via GET /api/v2/triggers.json. Lean projection — call
        get_trigger for one trigger's full conditions + actions."""
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        data = self._get_json('triggers.json', params, error_context='list triggers')
        triggers = [_project(t, _TRIGGER_LIST_FIELDS) for t in data.get('triggers', [])]
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

    def set_ticket_draft_reply(
        self, ticket_id: int, draft: str, field_title: str = _DRAFT_REPLY_FIELD_TITLE,
    ) -> Dict[str, Any]:
        """Store a reply draft on a ticket for an agent to review, via
        PUT /api/v2/tickets/{id}.json with the draft-reply custom field only.
        Adds no comment and sends nothing to the requester; an agent inserts the
        draft into the reply box with a macro and submits it."""
        fields: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._get_json(
                'ticket_fields.json', {'page': str(page), 'per_page': '100'},
                error_context='list ticket fields',
            )
            fields.extend(data.get('ticket_fields', []))
            if not data.get('next_page') or page >= 10:
                break
            page += 1
        field_id = _find_draft_reply_field(fields, field_title)
        payload = _build_draft_reply_payload(field_id, draft)
        data = self._send_json(
            'PUT', f'tickets/{int(ticket_id)}.json', payload,
            error_context=f'set draft reply on ticket {ticket_id}',
        )
        ticket = data.get('ticket', {})
        return {
            'ticket_id': ticket.get('id'),
            'status': ticket.get('status'),
            'field_id': field_id,
            'draft_chars': len(draft),
        }

    # --- Help Center (Guide) ---------------------------------------------------

    def list_help_center_sections(self) -> Dict[str, Any]:
        """List Help Center sections with their category names, via
        GET /api/v2/help_center/sections and /categories."""
        params = {'per_page': '100'}
        sections = self._get_json(
            'help_center/sections', params, error_context='list help center sections'
        ).get('sections', [])
        categories = self._get_json(
            'help_center/categories', params, error_context='list help center categories'
        ).get('categories', [])
        names = {c.get('id'): c.get('name') for c in categories}
        rows = []
        for section in sections:
            row = _project(section, _HC_SECTION_FIELDS)
            row['category_name'] = names.get(section.get('category_id'))
            rows.append(row)
        return {'count': len(rows), 'sections': rows}

    def list_help_center_user_segments(self) -> Dict[str, Any]:
        """List user segments (an article's audience) via
        GET /api/v2/help_center/user_segments."""
        data = self._get_json(
            'help_center/user_segments', {'per_page': '100'},
            error_context='list help center user segments',
        )
        rows = [_project(r, _HC_USER_SEGMENT_FIELDS) for r in data.get('user_segments', [])]
        return {'count': len(rows), 'user_segments': rows}

    def list_help_center_permission_groups(self) -> Dict[str, Any]:
        """List permission groups (who may edit and publish) via
        GET /api/v2/guide/permission_groups.json."""
        data = self._get_json(
            'guide/permission_groups.json', {'per_page': '100'},
            error_context='list help center permission groups',
        )
        rows = [
            _project(r, _HC_PERMISSION_GROUP_FIELDS) for r in data.get('permission_groups', [])
        ]
        return {'count': len(rows), 'permission_groups': rows}

    def list_help_center_articles(
        self,
        section_id: Any = None,
        label_names: Any = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """List articles, drafts included (lean projection, no body), via
        GET /api/v2/help_center/articles or /sections/{id}/articles.
        `label_names` returns only articles carrying all of the labels."""
        page, per_page = _coerce_pagination(page, per_page, default_per_page=100)
        params = {'page': str(page), 'per_page': str(per_page)}
        if label_names:
            if isinstance(label_names, str):
                label_names = [label_names]
            params['label_names'] = ','.join(str(x) for x in label_names)
        path = 'help_center/articles'
        if section_id is not None:
            path = f'help_center/sections/{int(section_id)}/articles'
        data = self._get_json(path, params, error_context='list help center articles')
        rows = [_project(a, _HC_ARTICLE_LIST_FIELDS) for a in data.get('articles', [])]
        return {
            'page': page,
            'per_page': per_page,
            'count': len(rows),
            'has_more': data.get('next_page') is not None,
            'articles': rows,
        }

    def get_help_center_article(self, article_id: int) -> Dict[str, Any]:
        """Get one article including its HTML body via
        GET /api/v2/help_center/articles/{id}."""
        data = self._get_json(
            f'help_center/articles/{int(article_id)}', {},
            error_context=f'get help center article {article_id}',
        )
        return _project(data.get('article', {}), _HC_ARTICLE_FIELDS)

    def create_help_center_article_draft(self, section_id: int, **fields: Any) -> Dict[str, Any]:
        """Create an article as a DRAFT via
        POST /api/v2/help_center/sections/{section_id}/articles. The payload
        builder forces draft=True and notify_subscribers=False."""
        payload = _build_article_draft_payload(fields)
        data = self._send_json(
            'POST', f'help_center/sections/{int(section_id)}/articles', payload,
            error_context='create help center article draft',
        )
        return _project(data.get('article', {}), _HC_ARTICLE_FIELDS)

    def update_help_center_article_draft(
        self,
        article_id: int,
        title: Any = None,
        body: Any = None,
        label_names: Any = None,
        **rest: Any,
    ) -> Dict[str, Any]:
        """Edit an article that is still a draft. Refuses a published article.
        Title and body go through PUT .../articles/{id}/translations/{locale}
        (always with draft=True); labels through PUT .../articles/{id}."""
        if rest:
            raise ValueError(
                f"unsupported field(s) {sorted(rest)}: only title, body and label_names can be changed"
            )
        article_id = int(article_id)
        current = self._get_json(
            f'help_center/articles/{article_id}', {},
            error_context=f'get help center article {article_id}',
        ).get('article', {})
        if current.get('draft') is not True:
            raise ValueError(
                f"article {article_id} is not a draft: published articles are edited in Zendesk Guide"
            )
        if title is None and body is None and label_names is None:
            raise ValueError("nothing to update: give title, body and/or label_names")
        if label_names is not None and not (
            isinstance(label_names, list) and all(isinstance(x, str) for x in label_names)
        ):
            raise TypeError("label_names must be a list of strings")
        if title is not None or body is not None:
            payload = _build_article_translation_payload({'title': title, 'body': body})
            locale = urllib.parse.quote(str(current.get('locale') or 'en-us'), safe='')
            self._send_json(
                'PUT', f'help_center/articles/{article_id}/translations/{locale}', payload,
                error_context=f'update help center article draft {article_id}',
            )
        if label_names is not None:
            self._send_json(
                'PUT', f'help_center/articles/{article_id}',
                {'article': {'label_names': label_names}},
                error_context=f'update labels on help center article {article_id}',
            )
        return self.get_help_center_article(article_id)

    def get_all_articles(self) -> Dict[str, Any]:
        """
        Fetch help center articles as knowledge base.
        Returns a Dict of section -> [article].
        """
        try:
            # Get all sections
            sections = self.client.help_center.sections()

            # Get articles for each section
            kb = {}
            for section in sections:
                articles = self.client.help_center.sections.articles(section.id)
                kb[section.name] = {
                    'section_id': section.id,
                    'description': section.description,
                    'articles': [{
                        'id': article.id,
                        'title': article.title,
                        'body': article.body,
                        'updated_at': str(article.updated_at),
                        'url': article.html_url
                    } for article in articles]
                }

            return kb
        except Exception as e:
            raise Exception(f"Failed to fetch knowledge base: {str(e)}")

    def create_ticket(
        self,
        subject: str,
        description: str,
        requester_id: int | None = None,
        assignee_id: int | None = None,
        group_id: int | None = None,
        brand_id: int | None = None,
        ticket_form_id: int | None = None,
        priority: str | None = None,
        type: str | None = None,
        status: str | None = None,
        collaborator_ids: List[int] | None = None,
        tags: List[str] | None = None,
        custom_fields: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """
        Create a new Zendesk ticket using Zenpy and return essential fields.

        Args:
            subject: Ticket subject
            description: Ticket description (plain text). Will also be used as initial comment.
            requester_id: Optional requester user ID
            assignee_id: Optional assignee user ID
            group_id: Optional group ID to assign the ticket to
            brand_id: Optional brand ID (multi-brand orgs)
            ticket_form_id: Optional ticket form ID; drives form-based workflows
            priority: Optional priority (low, normal, high, urgent)
            type: Optional ticket type (problem, incident, question, task)
            status: Optional initial status; Zendesk defaults to 'new' when omitted
            collaborator_ids: Optional user IDs to add as followers/CCs (Zendesk maps to followers or CCs per org setting)
            tags: Optional list of tags
            custom_fields: Optional list of dicts: {id: int, value: Any}
        """
        try:
            ticket = ZenpyTicket(
                subject=subject,
                description=description,
                requester_id=requester_id,
                assignee_id=assignee_id,
                group_id=group_id,
                brand_id=brand_id,
                ticket_form_id=ticket_form_id,
                priority=priority,
                type=type,
                status=status,
                collaborator_ids=collaborator_ids,
                tags=tags,
                custom_fields=custom_fields,
            )
            created_audit = self.client.tickets.create(ticket)
            # Fetch created ticket id from audit
            created_ticket_id = getattr(getattr(created_audit, 'ticket', None), 'id', None)
            if created_ticket_id is None:
                # Fallback: try to read id from audit events
                created_ticket_id = getattr(created_audit, 'id', None)

            # Fetch full ticket to return consistent data
            created = self.client.tickets(id=created_ticket_id) if created_ticket_id else None

            return {
                'id': getattr(created, 'id', created_ticket_id),
                'subject': getattr(created, 'subject', subject),
                'description': getattr(created, 'description', description),
                'status': getattr(created, 'status', status or 'new'),
                'priority': getattr(created, 'priority', priority),
                'type': getattr(created, 'type', type),
                'created_at': str(getattr(created, 'created_at', '')),
                'updated_at': str(getattr(created, 'updated_at', '')),
                'requester_id': getattr(created, 'requester_id', requester_id),
                'assignee_id': getattr(created, 'assignee_id', assignee_id),
                'group_id': getattr(created, 'group_id', group_id),
                'brand_id': getattr(created, 'brand_id', brand_id),
                'ticket_form_id': getattr(created, 'ticket_form_id', ticket_form_id),
                'collaborator_ids': list(getattr(created, 'collaborator_ids', collaborator_ids or []) or []),
                'organization_id': getattr(created, 'organization_id', None),
                'tags': list(getattr(created, 'tags', tags or []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to create ticket: {str(e)}")

    def update_ticket(self, ticket_id: int, **fields: Any) -> Dict[str, Any]:
        """
        Update a Zendesk ticket with provided fields using Zenpy.

        Supported fields include common ticket attributes like:
        subject, status, priority, type, assignee_id, requester_id, group_id,
        tags (list[str]), custom_fields (list[dict]), due_at, etc.
        """
        try:
            # Load the ticket, mutate fields directly, and update
            ticket = self.client.tickets(id=ticket_id)
            for key, value in fields.items():
                if value is None:
                    continue
                setattr(ticket, key, value)

            # This call returns a TicketAudit (not a Ticket). Don't read attrs from it.
            self.client.tickets.update(ticket)

            # Fetch the fresh ticket to return consistent data
            refreshed = self.client.tickets(id=ticket_id)

            return {
                'id': refreshed.id,
                'subject': refreshed.subject,
                'description': refreshed.description,
                'status': refreshed.status,
                'priority': refreshed.priority,
                'type': getattr(refreshed, 'type', None),
                'created_at': str(refreshed.created_at),
                'updated_at': str(refreshed.updated_at),
                'requester_id': refreshed.requester_id,
                'assignee_id': refreshed.assignee_id,
                'group_id': getattr(refreshed, 'group_id', None),
                'organization_id': refreshed.organization_id,
                'tags': list(getattr(refreshed, 'tags', []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to update ticket {ticket_id}: {str(e)}")
