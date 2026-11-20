# Zendesk MCP Server

![ci](https://github.com/reminia/zendesk-mcp-server/actions/workflows/ci.yml/badge.svg)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Model Context Protocol server for Zendesk.

This server provides a comprehensive integration with Zendesk. It offers:

- Tools for retrieving and managing Zendesk tickets and comments
- Specialized prompts for ticket analysis and response drafting
- Full access to the Zendesk Help Center articles as knowledge base

![demo](https://res.cloudinary.com/leecy-me/image/upload/v1736410626/open/zendesk_yunczu.gif)

## Setup

- build: `uv venv && uv pip install -e .` or `uv build` in short.
- setup zendesk credentials in `.env` file, refer to [.env.example](.env.example).
- configure in Claude desktop:

```json
{
  "mcpServers": {
      "zendesk": {
          "command": "uv",
          "args": [
              "--directory",
              "/path/to/zendesk-mcp-server",
              "run",
              "zendesk"
          ]
      }
  }
}
```

### Docker

You can containerize the server if you prefer an isolated runtime:

1. Copy `.env.example` to `.env` and fill in your Zendesk credentials. Keep this file outside version control.
2. Build the image:

   ```bash
   docker build -t zendesk-mcp-server .
   ```

3. Run the server, providing the environment file:

   ```bash
   docker run --rm --env-file /path/to/.env zendesk-mcp-server
   ```

   Add `-i` when wiring the container to MCP clients over STDIN/STDOUT (Claude Code uses this mode). For daemonized runs, add `-d --name zendesk-mcp`.

The image installs dependencies from `requirements.lock`, drops privileges to a non-root user, and expects configuration exclusively via environment variables.

#### Claude MCP Integration

To use the Dockerized server from Claude Code/Desktop, add an entry to Claude Code's `settings.json` similar to:

```json
{
  "mcpServers": {
    "zendesk": {
      "command": "/usr/local/bin/docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "--env-file",
        "/path/to/zendesk-mcp-server/.env",
        "zendesk-mcp-server"
      ]
    }
  }
}
```

Adjust the paths to match your environment. After saving the file, restart Claude for the new MCP server to be detected.

## Resources

- zendesk://knowledge-base, get access to the whole help center articles.

## Prompts

### analyze-ticket

Analyze a Zendesk ticket and provide a detailed analysis of the ticket.

### draft-ticket-response

Draft a response to a Zendesk ticket.

## Tools

### get_tickets

Fetch the latest tickets with pagination support

- Input:
  - `page` (integer, optional): Page number (defaults to 1)
  - `per_page` (integer, optional): Number of tickets per page, max 100 (defaults to 25)
  - `sort_by` (string, optional): Field to sort by - created_at, updated_at, priority, or status (defaults to created_at)
  - `sort_order` (string, optional): Sort order - asc or desc (defaults to desc)

- Output: Returns a list of tickets with essential fields including id, subject, status, priority, description, timestamps, and assignee information, along with pagination metadata

### get_ticket

Retrieve a Zendesk ticket by its ID

- Input:
  - `ticket_id` (integer): The ID of the ticket to retrieve

### get_ticket_comments

Retrieve all comments for a Zendesk ticket by its ID

- Input:
  - `ticket_id` (integer): The ID of the ticket to get comments for

### create_ticket_comment

Create a new comment on an existing Zendesk ticket

- Input:
  - `ticket_id` (integer): The ID of the ticket to comment on
  - `comment` (string): The comment text/content to add
  - `public` (boolean, optional): Whether the comment should be public (defaults to true)

### create_ticket

Create a new Zendesk ticket

- Input:
  - `subject` (string): Ticket subject
  - `description` (string): Ticket description
  - `requester_id` (integer, optional)
  - `assignee_id` (integer, optional)
  - `priority` (string, optional): one of `low`, `normal`, `high`, `urgent`
  - `type` (string, optional): one of `problem`, `incident`, `question`, `task`
  - `tags` (array[string], optional)
  - `custom_fields` (array[object], optional)

### update_ticket

Update fields on an existing Zendesk ticket (e.g., status, priority, assignee, group)

- Input:
  - `ticket_id` (integer): The ID of the ticket to update
  - `subject` (string, optional)
  - `status` (string, optional): one of `new`, `open`, `pending`, `on-hold`, `solved`, `closed`
  - `priority` (string, optional): one of `low`, `normal`, `high`, `urgent`
  - `type` (string, optional)
  - `assignee_id` (integer, optional)
  - `requester_id` (integer, optional)
  - `group_id` (integer, optional): group to move the ticket to
  - `tags` (array[string], optional)
  - `custom_fields` (array[object], optional)
  - `due_at` (string, optional): ISO8601 datetime

### set_ticket_draft_reply

Store a reply draft on a ticket for an agent to review. Writes only the agent-only multi-line ticket field titled
`AI draft reply`; it adds no comment, sends nothing to the requester and leaves status and assignee alone. A macro
whose comment is `{{ticket.ticket_field_<id> | newline_to_br}}` puts the draft in the reply box.

- Input:
  - `ticket_id` (integer)
  - `draft` (string): plain text, at most 8000 characters; an empty string clears the draft

### set_ticket_triage_notes

Store triage notes on a ticket for agents. Writes only the agent-only multi-line ticket field titled
`AI triage notes`, as one JSON object with the keys `kind`, `runbook`, `repeat`, `generated` and `note`. It adds
no comment and leaves status and assignee alone.

- Input:
  - `ticket_id` (integer)
  - `kind` (string): one of `request`, `incident`, `security`, `vendor`, `noise`
  - `note` (string): plain text, at most 4000 characters
  - `runbook` (string, optional): at most 200 characters
  - `repeat` (integer, optional): similar tickets in the past month
  - `generated` (string, optional): `YYYY-MM-DD`

### Help Center article drafts

Read the Help Center and stage articles for review. The server creates and edits **drafts only**: a draft is not
shown in the help center, and publishing is done by a person in Zendesk Guide. There is no publish or delete tool.

- `list_help_center_sections`: sections with their category names
- `list_help_center_user_segments`: audiences (`staff` = agents and admins, `signed_in_users` = any signed-in user)
- `list_help_center_permission_groups`: who may edit and publish
- `list_help_center_articles`: articles, drafts included, without bodies
  - `section_id` (integer, optional)
  - `label_names` (array[string], optional): only articles carrying all of the labels
  - `page`, `per_page` (integer, optional)
- `get_help_center_article`: one article with its HTML body
  - `article_id` (integer)
- `create_help_center_article_draft`: create an article as a draft; `draft` is always true, subscribers are never
  notified, and a `draft` argument is rejected
  - `section_id` (integer)
  - `title` (string)
  - `body` (string): HTML fragment
  - `user_segment_id` (integer): the audience once published; "everyone" is not accepted
  - `permission_group_id` (integer)
  - `locale` (string, optional): defaults to `en-us`
  - `label_names` (array[string], optional)
- `update_help_center_article_draft`: change the title, body or labels of an article that is still a draft; a
  published article is refused
  - `article_id` (integer)
  - `title`, `body` (string, optional)
  - `label_names` (array[string], optional): replaces the article's labels
