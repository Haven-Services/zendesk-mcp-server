import asyncio
import json
import logging
import os
from typing import Any, Dict

from cachetools.func import ttl_cache
from dotenv import load_dotenv
from mcp.server import InitializationOptions, NotificationOptions
from mcp.server import Server, types
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from zendesk_mcp_server.zendesk_client import ZendeskClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("zendesk-mcp-server")
logger.info("zendesk mcp server started")

load_dotenv()
zendesk_client = ZendeskClient(
    subdomain=os.getenv("ZENDESK_SUBDOMAIN"),
    email=os.getenv("ZENDESK_EMAIL"),
    token=os.getenv("ZENDESK_API_KEY")
)

server = Server("Zendesk Server")

TICKET_ANALYSIS_TEMPLATE = """
You are a helpful Zendesk support analyst. You've been asked to analyze ticket #{ticket_id}.

Please fetch the ticket info and comments to analyze it and provide:
1. A summary of the issue
2. The current status and timeline
3. Key points of interaction

Remember to be professional and focus on actionable insights.
"""

COMMENT_DRAFT_TEMPLATE = """
You are a helpful Zendesk support agent. You need to draft a response to ticket #{ticket_id}.

Please fetch the ticket info, comments and knowledge base to draft a professional and helpful response that:
1. Acknowledges the customer's concern
2. Addresses the specific issues raised
3. Provides clear next steps or ask for specific details need to proceed
4. Maintains a friendly and professional tone
5. Ask for confirmation before commenting on the ticket

The response should be formatted well and ready to be posted as a comment.
"""


@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    """List available prompts"""
    return [
        types.Prompt(
            name="analyze-ticket",
            description="Analyze a Zendesk ticket and provide insights",
            arguments=[
                types.PromptArgument(
                    name="ticket_id",
                    description="The ID of the ticket to analyze",
                    required=True,
                )
            ],
        ),
        types.Prompt(
            name="draft-ticket-response",
            description="Draft a professional response to a Zendesk ticket",
            arguments=[
                types.PromptArgument(
                    name="ticket_id",
                    description="The ID of the ticket to respond to",
                    required=True,
                )
            ],
        )
    ]


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: Dict[str, str] | None) -> types.GetPromptResult:
    """Handle prompt requests"""
    if not arguments or "ticket_id" not in arguments:
        raise ValueError("Missing required argument: ticket_id")

    ticket_id = int(arguments["ticket_id"])
    try:
        if name == "analyze-ticket":
            prompt = TICKET_ANALYSIS_TEMPLATE.format(
                ticket_id=ticket_id
            )
            description = f"Analysis prompt for ticket #{ticket_id}"

        elif name == "draft-ticket-response":
            prompt = COMMENT_DRAFT_TEMPLATE.format(
                ticket_id=ticket_id
            )
            description = f"Response draft prompt for ticket #{ticket_id}"

        else:
            raise ValueError(f"Unknown prompt: {name}")

        return types.GetPromptResult(
            description=description,
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt.strip()),
                )
            ],
        )

    except Exception as e:
        logger.error(f"Error generating prompt: {e}")
        raise


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available Zendesk tools"""
    return [
        types.Tool(
            name="get_ticket",
            description="Retrieve a Zendesk ticket by its ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to retrieve"
                    }
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="create_ticket",
            description="Create a new Zendesk ticket",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Ticket subject"},
                    "description": {"type": "string", "description": "Ticket description"},
                    "requester_id": {"type": "integer"},
                    "assignee_id": {"type": "integer"},
                    "group_id": {"type": "integer", "description": "Group ID to assign the ticket to"},
                    "brand_id": {"type": "integer", "description": "Brand ID (multi-brand orgs). Determines support address and branding."},
                    "ticket_form_id": {"type": "integer", "description": "Ticket form ID. Determines which custom fields are visible/required and drives routing for form-based workflows."},
                    "priority": {"type": "string", "description": "low, normal, high, urgent"},
                    "type": {"type": "string", "description": "problem, incident, question, task"},
                    "status": {"type": "string", "description": "Initial status (new, open, pending, hold, solved, closed). Defaults to 'new'."},
                    "collaborator_ids": {"type": "array", "items": {"type": "integer"}, "description": "User IDs to add as followers/CCs. Zendesk maps these to followers or CCs based on the org's 'CCs and Followers' setting."},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "custom_fields": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["subject", "description"],
            }
        ),
        types.Tool(
            name="get_tickets",
            description="Fetch the latest tickets with pagination support",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "description": "Page number",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Number of tickets per page (max 100)",
                        "default": 25
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Field to sort by (created_at, updated_at, priority, status)",
                        "default": "created_at"
                    },
                    "sort_order": {
                        "type": "string",
                        "description": "Sort order (asc or desc)",
                        "default": "desc"
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="get_ticket_comments",
            description="Retrieve all comments for a Zendesk ticket by its ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to get comments for"
                    }
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="create_ticket_comment",
            description="Create a new comment on an existing Zendesk ticket",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to comment on"
                    },
                    "comment": {
                        "type": "string",
                        "description": "The comment text/content to add"
                    },
                    "public": {
                        "type": "boolean",
                        "description": "Whether the comment should be public",
                        "default": True
                    }
                },
                "required": ["ticket_id", "comment"]
            }
        ),
        types.Tool(
            name="get_ticket_attachment",
            description="Fetch a Zendesk ticket attachment by its content_url and return the file as base64-encoded data. Use the attachment URLs returned by get_ticket_comments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content_url": {
                        "type": "string",
                        "description": "The content_url of the attachment from get_ticket_comments"
                    }
                },
                "required": ["content_url"]
            }
        ),
        types.Tool(
            name="search_users",
            description="Search Zendesk users by email, name, or phone. Matches across primary and secondary emails. Returns id, name, email, role, active, organization_id, created_at per user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search text (email address, name fragment, or phone number)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max users to return (1-100)",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="search_tickets",
            description=(
                "Search Zendesk tickets via the Search API. Accepts raw Zendesk Search syntax "
                "(e.g. 'assignee:123 status<solved tags:vpn created>2026-01-01'); 'type:ticket' "
                "is auto-prepended when no 'type:' prefix is present. Supports an optional "
                "extra_filters dict to merge structured filters onto the query. "
                "Hard 1000-result cap (page * per_page <= 1000) — narrow by tag, date, or brand "
                "for analytical work. Newly-created tickets take a few minutes to appear in the "
                "search index. Returns a lean 'summary' projection by default, or 'full' to include "
                "description + custom_fields + brand/form/group IDs. "
                "ZQL syntax reference: https://support.zendesk.com/hc/en-us/articles/4408886879258"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Raw Zendesk Search syntax. 'type:ticket' auto-prepended if no 'type:' prefix is present."
                    },
                    "extra_filters": {
                        "type": "object",
                        "description": "Optional structured filters merged onto the query as 'key:value' pairs. Keys may carry operator suffixes (e.g. 'status<', 'created>'). List values are expanded to multiple key:value pairs."
                    },
                    "fields": {
                        "type": "string",
                        "enum": ["summary", "full"],
                        "description": "Projection: 'summary' (lean, no description) or 'full' (adds description, ticket_form_id, brand_id, group_id, organization_id, custom_fields).",
                        "default": "summary"
                    },
                    "page": {
                        "type": "integer",
                        "description": "1-based page number.",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (1-100). page * per_page must be <= 1000.",
                        "default": 25
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["updated_at", "created_at", "priority", "status", "ticket_type"],
                        "default": "updated_at"
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="update_ticket",
            description="Update fields on an existing Zendesk ticket (e.g., status, priority, assignee_id)",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer", "description": "The ID of the ticket to update"},
                    "subject": {"type": "string"},
                    "status": {"type": "string", "description": "new, open, pending, on-hold, solved, closed"},
                    "priority": {"type": "string", "description": "low, normal, high, urgent"},
                    "type": {"type": "string"},
                    "assignee_id": {"type": "integer"},
                    "requester_id": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "custom_fields": {"type": "array", "items": {"type": "object"}},
                    "due_at": {"type": "string", "description": "ISO8601 datetime"}
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="get_users",
            description=(
                "Resolve Zendesk user IDs to records. Use to turn requester_id, "
                "assignee_id, author_id, or collaborator_ids (from tickets, comments, "
                "and search results) into names and emails. Accepts up to 100 ids per "
                "call. Non-existent ids are silently omitted, so the returned set may "
                "be smaller than requested. Returns id, name, email, role, active, "
                "organization_id, created_at per user."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "User IDs to resolve (max 100)."
                    }
                },
                "required": ["ids"]
            }
        ),
        types.Tool(
            name="get_organizations",
            description=(
                "Resolve Zendesk organization IDs to records. Use to turn "
                "organization_id (from tickets, users, and search results) into org "
                "names. Accepts up to 100 ids per call. Non-existent ids are silently "
                "omitted. Returns id, name, created_at per organization."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Organization IDs to resolve (max 100)."
                    }
                },
                "required": ["ids"]
            }
        ),
        types.Tool(
            name="list_groups",
            description=(
                "List agent groups. Use to resolve group_id to a group name. Group "
                "sets are small; one page is usually enough. Returns id, name, "
                "description, default, deleted, created_at per group."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "1-based page number.", "default": 1},
                    "per_page": {"type": "integer", "description": "Results per page (1-100).", "default": 100}
                },
                "required": []
            }
        ),
        types.Tool(
            name="list_brands",
            description=(
                "List brands (multi-brand orgs). Use to resolve brand_id to a brand "
                "name/subdomain. Returns id, name, brand_url, subdomain, active, "
                "default per brand."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "1-based page number.", "default": 1},
                    "per_page": {"type": "integer", "description": "Results per page (1-100).", "default": 100}
                },
                "required": []
            }
        ),
        types.Tool(
            name="list_ticket_forms",
            description=(
                "List ticket forms. Use to resolve ticket_form_id to a form name. "
                "Returns all forms (id, name, display_name, active, default, "
                "end_user_visible, ticket_field_ids)."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="list_ticket_fields",
            description=(
                "List ticket fields, including custom_field_options (id/name/value) "
                "for tagger/multiselect fields. Use this to interpret a ticket's "
                "custom_fields array: each {id, value} joins to a ticket field by id, "
                "and the value maps to an option's value/name. Returns id, type, "
                "title, active, custom_field_options per field."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "1-based page number.", "default": 1},
                    "per_page": {"type": "integer", "description": "Results per page (1-100).", "default": 100}
                },
                "required": []
            }
        )
    ]


@server.call_tool()
async def handle_call_tool(
        name: str,
        arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Handle Zendesk tool execution requests"""
    try:
        if name == "get_ticket":
            if not arguments:
                raise ValueError("Missing arguments")
            ticket = zendesk_client.get_ticket(arguments["ticket_id"])
            return [types.TextContent(
                type="text",
                text=json.dumps(ticket)
            )]

        elif name == "create_ticket":
            if not arguments:
                raise ValueError("Missing arguments")
            created = zendesk_client.create_ticket(
                subject=arguments.get("subject"),
                description=arguments.get("description"),
                requester_id=arguments.get("requester_id"),
                assignee_id=arguments.get("assignee_id"),
                group_id=arguments.get("group_id"),
                brand_id=arguments.get("brand_id"),
                ticket_form_id=arguments.get("ticket_form_id"),
                priority=arguments.get("priority"),
                type=arguments.get("type"),
                status=arguments.get("status"),
                collaborator_ids=arguments.get("collaborator_ids"),
                tags=arguments.get("tags"),
                custom_fields=arguments.get("custom_fields"),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps({"message": "Ticket created successfully", "ticket": created}, indent=2)
            )]

        elif name == "get_tickets":
            page = arguments.get("page", 1) if arguments else 1
            per_page = arguments.get("per_page", 25) if arguments else 25
            sort_by = arguments.get("sort_by", "created_at") if arguments else "created_at"
            sort_order = arguments.get("sort_order", "desc") if arguments else "desc"

            tickets = zendesk_client.get_tickets(
                page=page,
                per_page=per_page,
                sort_by=sort_by,
                sort_order=sort_order
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(tickets, indent=2)
            )]

        elif name == "get_ticket_comments":
            if not arguments:
                raise ValueError("Missing arguments")
            comments = zendesk_client.get_ticket_comments(
                arguments["ticket_id"])
            return [types.TextContent(
                type="text",
                text=json.dumps(comments)
            )]

        elif name == "create_ticket_comment":
            if not arguments:
                raise ValueError("Missing arguments")
            public = arguments.get("public", True)
            result = zendesk_client.post_comment(
                ticket_id=arguments["ticket_id"],
                comment=arguments["comment"],
                public=public
            )
            return [types.TextContent(
                type="text",
                text=f"Comment created successfully: {result}"
            )]

        elif name == "get_ticket_attachment":
            if not arguments:
                raise ValueError("Missing arguments")
            result = zendesk_client.get_ticket_attachment(arguments["content_url"])
            content_type = result["content_type"]
            if content_type.startswith("image/"):
                return [types.ImageContent(
                    type="image",
                    data=result["data"],
                    mimeType=content_type,
                )]
            else:
                return [types.TextContent(
                    type="text",
                    text=json.dumps({"content_type": content_type, "data_base64": result["data"]})
                )]

        elif name == "search_users":
            if not arguments or not arguments.get("query"):
                raise ValueError("query is required")
            result = zendesk_client.search_users(
                query=arguments["query"],
                limit=arguments.get("limit", 10),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "search_tickets":
            if not arguments or "query" not in arguments:
                raise ValueError("query is required")
            # Defaults live on the Python signature in zendesk_client.py — only
            # forward kwargs the caller actually supplied.
            optional_keys = (
                "extra_filters", "fields", "page", "per_page",
                "sort_by", "sort_order",
            )
            result = zendesk_client.search_tickets(
                query=arguments["query"],
                **{k: arguments[k] for k in optional_keys if k in arguments},
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "update_ticket":
            if not arguments:
                raise ValueError("Missing arguments")
            ticket_id = arguments.get("ticket_id")
            if ticket_id is None:
                raise ValueError("ticket_id is required")
            update_fields = {k: v for k, v in arguments.items() if k != "ticket_id"}
            updated = zendesk_client.update_ticket(ticket_id=int(ticket_id), **update_fields)
            return [types.TextContent(
                type="text",
                text=json.dumps({"message": "Ticket updated successfully", "ticket": updated}, indent=2)
            )]

        elif name == "get_users":
            if not arguments or arguments.get("ids") is None:
                raise ValueError("ids is required")
            result = zendesk_client.get_users(arguments["ids"])
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_organizations":
            if not arguments or arguments.get("ids") is None:
                raise ValueError("ids is required")
            result = zendesk_client.get_organizations(arguments["ids"])
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "list_groups":
            result = zendesk_client.list_groups(
                page=(arguments or {}).get("page", 1),
                per_page=(arguments or {}).get("per_page", 100),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "list_brands":
            result = zendesk_client.list_brands(
                page=(arguments or {}).get("page", 1),
                per_page=(arguments or {}).get("per_page", 100),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "list_ticket_forms":
            result = zendesk_client.list_ticket_forms()
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "list_ticket_fields":
            result = zendesk_client.list_ticket_fields(
                page=(arguments or {}).get("page", 1),
                per_page=(arguments or {}).get("per_page", 100),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        return [types.TextContent(
            type="text",
            text=f"Error: {str(e)}"
        )]


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    logger.debug("Handling list_resources request")
    return [
        types.Resource(
            uri=AnyUrl("zendesk://knowledge-base"),
            name="Zendesk Knowledge Base",
            description="Access to Zendesk Help Center articles and sections",
            mimeType="application/json",
        )
    ]


@ttl_cache(ttl=3600)
def get_cached_kb():
    return zendesk_client.get_all_articles()


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> str:
    logger.debug(f"Handling read_resource request for URI: {uri}")
    if uri.scheme != "zendesk":
        logger.error(f"Unsupported URI scheme: {uri.scheme}")
        raise ValueError(f"Unsupported URI scheme: {uri.scheme}")

    path = str(uri).replace("zendesk://", "")
    if path != "knowledge-base":
        logger.error(f"Unknown resource path: {path}")
        raise ValueError(f"Unknown resource path: {path}")

    try:
        kb_data = get_cached_kb()
        return json.dumps({
            "knowledge_base": kb_data,
            "metadata": {
                "sections": len(kb_data),
                "total_articles": sum(len(section['articles']) for section in kb_data.values()),
            }
        }, indent=2)
    except Exception as e:
        logger.error(f"Error fetching knowledge base: {e}")
        raise


async def main():
    # Run the server using stdin/stdout streams
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream=read_stream,
            write_stream=write_stream,
            initialization_options=InitializationOptions(
                server_name="Zendesk",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
