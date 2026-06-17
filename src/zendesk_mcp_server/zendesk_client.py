from typing import Dict, Any, List, Tuple
import json
import re
import urllib.request
import urllib.parse
import urllib.error
import base64
import requests as _requests

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
_ORGANIZATION_FIELDS: Tuple[str, ...] = ('id', 'name')
_GROUP_FIELDS: Tuple[str, ...] = ('id', 'name')

# Zendesk Search API hard cap. page * per_page > 1000 returns HTTP 422.
_SEARCH_RESULT_CAP = 1000


def _project(record: Dict[str, Any], fields: Tuple[str, ...]) -> Dict[str, Any]:
    return {k: record.get(k) for k in fields}


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
        - Allowlist of safe image MIME types (no SVG or arbitrary binary).
        - Magic byte validation so the file header must match the declared type.
        - 10 MB size cap to prevent image bombs and excessive token usage.

        Zendesk attachment URLs redirect to zdusercontent.com (Zendesk's CDN).
        requests strips the Authorization header on cross-origin redirects,
        which is required — the CDN returns 403 if it receives an auth header.
        """
        try:
            response = _requests.get(
                content_url,
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

    def post_comment(self, ticket_id: int, comment: str, public: bool = True) -> str:
        """
        Post a comment to an existing ticket.
        """
        try:
            ticket = self.client.tickets(id=ticket_id)
            ticket.comment = Comment(
                html_body=comment,
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
        include_sideloads: bool = False,
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
            include_sideloads: when True, request ?include=users,organizations,groups.
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
        if include_sideloads:
            params['include'] = 'users,organizations,groups'

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

        if include_sideloads:
            result['users'] = [_project(u, _USER_FIELDS) for u in data.get('users') or []]
            result['organizations'] = [_project(o, _ORGANIZATION_FIELDS) for o in data.get('organizations') or []]
            result['groups'] = [_project(g, _GROUP_FIELDS) for g in data.get('groups') or []]

        return result

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
        subject, status, priority, type, assignee_id, requester_id,
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
                'organization_id': refreshed.organization_id,
                'tags': list(getattr(refreshed, 'tags', []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to update ticket {ticket_id}: {str(e)}")
