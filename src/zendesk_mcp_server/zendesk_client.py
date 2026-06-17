from typing import Dict, Any, List
import json
import urllib.request
import urllib.parse
import base64
import requests as _requests

from zenpy import Zenpy
from zenpy.lib.api_objects import Comment
from zenpy.lib.api_objects import Ticket as ZenpyTicket


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

    def get_tickets(self, page: int = 1, per_page: int = 25, sort_by: str = 'created_at', sort_order: str = 'desc') -> Dict[str, Any]:
        """
        Get the latest tickets with proper pagination support using direct API calls.

        Args:
            page: Page number (1-based)
            per_page: Number of tickets per page (max 100)
            sort_by: Field to sort by (created_at, updated_at, priority, status)
            sort_order: Sort order (asc or desc)

        Returns:
            Dict containing tickets and pagination info
        """
        try:
            # Cap at reasonable limit
            per_page = min(per_page, 100)

            # Build URL with parameters for offset pagination
            params = {
                'page': str(page),
                'per_page': str(per_page),
                'sort_by': sort_by,
                'sort_order': sort_order
            }
            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/tickets.json?{query_string}"

            # Create request with auth header
            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            # Make the API request
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            tickets_data = data.get('tickets', [])

            # Process tickets to return only essential fields
            ticket_list = []
            for ticket in tickets_data:
                ticket_list.append({
                    'id': ticket.get('id'),
                    'subject': ticket.get('subject'),
                    'status': ticket.get('status'),
                    'priority': ticket.get('priority'),
                    'description': ticket.get('description'),
                    'created_at': ticket.get('created_at'),
                    'updated_at': ticket.get('updated_at'),
                    'requester_id': ticket.get('requester_id'),
                    'assignee_id': ticket.get('assignee_id')
                })

            return {
                'tickets': ticket_list,
                'page': page,
                'per_page': per_page,
                'count': len(ticket_list),
                'sort_by': sort_by,
                'sort_order': sort_order,
                'has_more': data.get('next_page') is not None,
                'next_page': page + 1 if data.get('next_page') else None,
                'previous_page': page - 1 if data.get('previous_page') and page > 1 else None
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get latest tickets: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get latest tickets: {str(e)}")

    def search_users(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """
        Search users via /api/v2/users/search.json. Zendesk decides what to match
        on (email, name, phone); secondary emails are included in the search.

        Args:
            query: Search text (email, name fragment, phone number).
            limit: Max users to return (1..100). Zendesk pages at 100; we trim client-side.
        """
        try:
            limit = max(1, min(int(limit or 10), 100))
            params = {'query': query, 'per_page': str(limit)}
            url = f"{self.base_url}/users/search.json?{urllib.parse.urlencode(params)}"

            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            users = []
            for u in data.get('users', [])[:limit]:
                users.append({
                    'id': u.get('id'),
                    'name': u.get('name'),
                    'email': u.get('email'),
                    'role': u.get('role'),
                    'active': u.get('active'),
                    'organization_id': u.get('organization_id'),
                    'created_at': u.get('created_at'),
                })

            return {
                'query': query,
                'count': len(users),
                'users': users,
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to search users: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to search users: {str(e)}")

    _TICKET_SUMMARY_FIELDS = (
        'id', 'subject', 'status', 'priority', 'type',
        'created_at', 'updated_at',
        'requester_id', 'assignee_id', 'tags',
    )
    _TICKET_FULL_EXTRA_FIELDS = (
        'description', 'ticket_form_id', 'brand_id', 'group_id',
        'organization_id', 'custom_fields',
    )

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
        `extra_filters` (merged as `key:value` pairs). `type:ticket` is auto-prepended
        when no `type:` prefix is present in the input.

        Hard 1000-result cap enforced server-side: page * per_page > 1000 raises
        before any API call. Newly-created tickets take a few minutes to appear
        in the search index.

        Args:
            query: Raw Zendesk Search syntax (e.g. "assignee:123 status<solved").
            extra_filters: Optional dict merged onto `query` as `key:value`. Keys
                may carry operator suffixes (e.g. "status<": "solved", "created>": "2026-01-01").
            fields: "summary" (lean) or "full" (adds description + categorization fields).
            page: 1-based. Clamped to >= 1.
            per_page: 1..100. Clamped.
            sort_by: updated_at, created_at, priority, status, ticket_type.
            sort_order: asc | desc.
            include_sideloads: when True, request ?include=users,organizations,groups.
        """
        try:
            page = max(1, int(page or 1))
            per_page = max(1, min(int(per_page or 25), 100))

            if page * per_page > 1000:
                raise ValueError(
                    f"Search API cap reached: page * per_page = {page * per_page} > 1000. "
                    "Narrow the query with tags, date range, or brand_id."
                )

            extras = []
            for k, v in (extra_filters or {}).items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    for item in v:
                        extras.append(f"{k}:{item}")
                else:
                    extras.append(f"{k}:{v}")

            composed = ' '.join(part for part in [query.strip() if query else '', *extras] if part)
            if 'type:' not in composed:
                composed = f"type:ticket {composed}".strip()

            params = {
                'query': composed,
                'page': str(page),
                'per_page': str(per_page),
                'sort_by': sort_by,
                'sort_order': sort_order,
            }
            if include_sideloads:
                params['include'] = 'users,organizations,groups'

            url = f"{self.base_url}/search.json?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            summary_keys = self._TICKET_SUMMARY_FIELDS
            full_keys = summary_keys + self._TICKET_FULL_EXTRA_FIELDS
            keys = full_keys if fields == 'full' else summary_keys

            tickets = []
            for t in data.get('results', []):
                row = {k: t.get(k) for k in keys}
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
                'has_more': data.get('next_page') is not None,
                'tickets': tickets,
            }
            if total > 1000:
                result['truncation_warning'] = (
                    f"total ({total}) exceeds the 1000-result Search API cap; "
                    "results past page 10 (per_page=100) are unreachable. Narrow the query."
                )

            if include_sideloads:
                result['users'] = [
                    {k: u.get(k) for k in ('id', 'name', 'email', 'role')}
                    for u in data.get('users', []) or []
                ]
                result['organizations'] = [
                    {k: o.get(k) for k in ('id', 'name')}
                    for o in data.get('organizations', []) or []
                ]
                result['groups'] = [
                    {k: g.get(k) for k in ('id', 'name')}
                    for g in data.get('groups', []) or []
                ]

            return result
        except ValueError:
            raise
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to search tickets: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to search tickets: {str(e)}")

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