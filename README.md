# slack-management-mcp

A focused [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server
for managing Slack users, channels, user groups, canvases and lists.

| Tool | What it does |
| --- | --- |
| `lookup_user` | Find a user by email address or user ID. |
| `lookup_channel` | Find a channel by name or channel ID. |
| `invite_user_to_channel` | Invite a user (by email or ID) to a channel (by name or ID). |
| `lookup_usergroup` | Find a user group by handle (`@marketing`) or user group ID. |
| `add_users_to_usergroup` | Add users (by email or ID) to a user group (by handle or ID). |
| `create_canvas` | Create a standalone canvas from Markdown. |
| `edit_canvas` | Insert, replace or delete a section; replace all content; rename. |
| `lookup_canvas_sections` | Find section IDs by text or section type. |
| `delete_canvas` | Permanently delete a canvas. |
| `set_canvas_access` / `remove_canvas_access` | Grant or revoke user/channel access. |
| `create_list` / `update_list` | Create a List with columns; change name, description or todo mode. |
| `get_list_items` / `get_list_item` | Read rows and column schema, with cursor pagination. |
| `create_list_item` | Add a row, subtask or copy of a row. |
| `update_list_items` | Update up to 100 cells across rows. |
| `delete_list_item` | Delete a row. |
| `set_list_access` / `remove_list_access` | Grant or revoke user/channel access. |

> **Channels vs. user groups.** A *channel* is a place where messages are posted.
> A *user group* is a named, `@`-mentionable collection of people (e.g.
> `@marketing`) used to ping or reference several people at once. The
> `*_usergroup` tools operate on the latter. User groups are a **paid** Slack
> feature. Slack has no "append" API for them, so `add_users_to_usergroup` reads
> the current membership and rewrites it with the new users merged in — existing
> members are preserved.

The server can authenticate in two ways. A **bot token** (`xoxb-`) makes every
action run as the app's bot user. **User OAuth** stores a user token (`xoxp-`)
from a browser login so actions run as that Slack member instead. If both are
configured and `SLACK_AUTH_MODE` is unset, the bot token is used.

## Quick start

### 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Select your workspace and paste the contents of
   [`slack-app-manifest.yaml`](./slack-app-manifest.yaml).
3. Create the app, then click **Install to Workspace** and approve it.
4. Under **OAuth & Permissions**, copy the **Bot User OAuth Token** — it starts
   with `xoxb-`. To act as yourself instead, skip the bot token and follow
   [User OAuth](#user-oauth) below.

### 2. Set the bot token

For the bot, the server reads one environment variable:

```bash
export SLACK_BOT_TOKEN="xoxb-your-token-here"
```

Leave this unset when you want user OAuth (step 4 of [User OAuth](#user-oauth)).

### 3. Run it

The server runs over stdio and is launched by your MCP client. To run it on its
own (e.g. for a smoke test) with [`uv`](https://docs.astral.sh/uv/) installed:

```bash
# Straight from the repo, before it is published to PyPI:
uvx --from git+https://github.com/dbuxton/slack-management-mcp slack-management-mcp

# Local development from a checkout:
uv run --locked slack-management-mcp
```

Once published to PyPI the bare form works too:

```bash
uvx slack-management-mcp
```

## Configure your MCP client

Add the server to your MCP client config. For Claude Desktop / Claude Code:

```json
{
  "mcpServers": {
    "slack-management": {
      "command": "uvx",
      "args": ["slack-management-mcp"],
      "env": { "SLACK_BOT_TOKEN": "xoxb-..." }
    }
  }
}
```

Before publishing to PyPI, use the git form instead:

```json
{
  "mcpServers": {
    "slack-management": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/dbuxton/slack-management-mcp",
        "slack-management-mcp"
      ],
      "env": { "SLACK_BOT_TOKEN": "xoxb-..." }
    }
  }
}
```

## Authentication & permissions

Choose one credential. If `SLACK_BOT_TOKEN` is set and `SLACK_AUTH_MODE` is
unset, the server acts as the bot even when a user token is also available.
Set `SLACK_AUTH_MODE=user` to force the user token, or `SLACK_AUTH_MODE=bot`
to require the bot token.

### Bot token

1. Create and install the app **once** (steps above). Slack issues a bot token
   tied to the bot user in your workspace.
2. Give the server that token via `SLACK_BOT_TOKEN`.
3. Every action the server performs is done **as the bot**.

A bot-only setup does not need the app's Client ID, Client Secret, or Signing
Secret.

### User OAuth

User OAuth lets the server act as you. Invites, canvases, and lists run as your
member, and the server only sees conversations and resources you can see. You
must already be in a channel to invite someone to it.

1. Update the Slack app from
   [`slack-app-manifest.yaml`](./slack-app-manifest.yaml). The manifest includes
   user scopes and the redirect URL `http://127.0.0.1:8765/callback`. Adding
   these does not replace an existing bot token.
2. On **Basic Information**, copy the **Client ID** and **Client Secret**.
3. Log in once on the machine that will run the server:

   ```bash
   export SLACK_CLIENT_ID="..."
   export SLACK_CLIENT_SECRET="..."
   slack-management-mcp login
   ```

   The command prints an authorize URL, opens a browser, and listens on
   `127.0.0.1:8765`. It stores the user token (`xoxp-`) at
   `$XDG_CONFIG_HOME/slack-management-mcp/credentials.json` (default
   `~/.config/slack-management-mcp/credentials.json`) with mode `0600`. It does
   not print the token and does not write a bot token.
   `slack-management-mcp logout` deletes the file.

4. Start the MCP server **without** `SLACK_BOT_TOKEN` so it loads the saved
   user token:

   ```json
   {
     "mcpServers": {
       "slack-management": {
         "command": "uvx",
         "args": ["slack-management-mcp"]
       }
     }
   }
   ```

For a headless environment, skip the browser and set `SLACK_USER_TOKEN` to a
user token (`xoxp-`). That is the same kind of credential `login` saves:

```json
"env": { "SLACK_USER_TOKEN": "xoxp-..." }
```

`SLACK_REDIRECT_URI` overrides the callback URL. It must be an
`http://127.0.0.1:<port>/<path>` URL that is also listed on the Slack app.
Token rotation stays off, so the user token lasts until you revoke the app in
Slack or run `logout`.

### Required bot scopes

These are preset in the manifest:

| Scope | Why |
| --- | --- |
| `users:read` | Look up users by ID (`users.info`). |
| `users:read.email` | Look up users by email (`users.lookupByEmail`). |
| `channels:read` | List and read public channels. |
| `groups:read` | List and read private channels the bot is in. |
| `channels:manage` | Invite users to public channels. |
| `groups:write` | Invite users to private channels. |
| `channels:join` | Let the bot self-join public channels before inviting. |
| `usergroups:read` | List and read user groups (`usergroups.list`). |
| `usergroups:write` | Update user group membership (`usergroups.users.update`). |
| `canvases:read` | Find sections within a canvas. |
| `canvases:write` | Create, edit, delete and share canvases. |
| `lists:read` | Read List rows and schema. |
| `lists:write` | Create, update and share Lists; manage rows. |

**Existing bot installations:** if the installed bot token is missing canvas or
list scopes, add them in OAuth & Permissions (or update the manifest), then
**reinstall the app to the workspace** and use the resulting bot token. Adding
scopes to the manifest alone does not upgrade an installed bot token. Existing
tools keep their current names and arguments. You do not need to reinstall to
start using user OAuth; update the manifest so the user scopes and redirect URL
are allowed, then run `login`.

### Required user scopes

`slack-management-mcp login` requests these user-token scopes (also preset in
the manifest). `channels:manage` and `channels:join` are bot-only, so user
invites use the invite scopes instead.

| Scope | Why |
| --- | --- |
| `users:read` | Look up users by ID (`users.info`). |
| `users:read.email` | Look up users by email (`users.lookupByEmail`). |
| `channels:read` | List and read public channels. |
| `groups:read` | List and read private channels the user is in. |
| `channels:write.invites` | Invite users to public channels. |
| `groups:write.invites` | Invite users to private channels. |
| `usergroups:read` | List and read user groups (`usergroups.list`). |
| `usergroups:write` | Update user group membership (`usergroups.users.update`). |
| `canvases:read` | Find sections within a canvas. |
| `canvases:write` | Create, edit, delete and share canvases. |
| `lists:read` | Read List rows and schema. |
| `lists:write` | Create, update and share Lists; manage rows. |

### Important: the acting account must be in the channel

To invite someone to a channel, **the authenticated account must already be a
member of that channel**. With a bot token, public channels can be joined by
the bot and private channels need `/invite @slack-management-mcp`. With user
OAuth, join the channel yourself first. If the account is not a member,
`invite_user_to_channel` returns a clear error explaining this.

## Canvas and List workflows

Canvas and List APIs require a paid Slack workspace and the acting account must
have access to the target resource. New resources belong to that account (the
bot, or you when signed in with user OAuth); share them with
`set_canvas_access` or `set_list_access`. Pass either user IDs or channel IDs,
not both. `owner` transfers ownership to a user.

Create a canvas with `create_canvas(title="Project notes", markdown="# Status\nOn track")`.
Append with `edit_canvas(canvas_id="F...", operation="insert_at_end", markdown="Next steps")`.
For a targeted edit, first call `lookup_canvas_sections` with `contains_text`
and/or `section_types`, then pass a returned section ID to `edit_canvas`.
Section lookup returns IDs, not the full document. **`replace` without a section
ID replaces the whole document; `delete_canvas` is permanent.**

For Lists, call `create_list(name="Tasks", todo_mode=True)` or supply `schema`
column definitions following [Slack's schema format](https://docs.slack.dev/reference/methods/slackLists.create/).
Use the returned column IDs to create rows. For an existing List, call
`get_list_items(list_id="F...")`; `include_list=True` returns the column schema.
Pass `response_metadata.next_cursor` into the next call until it is empty.
`include_list=False` avoids repeating the schema on subsequent pages.

Text cells require Block Kit rich text, not a plain `text` property:

```json
{
  "list_id": "F...",
  "initial_fields": [{
    "column_id": "Col...",
    "rich_text": [{
      "type": "rich_text",
      "elements": [{
        "type": "rich_text_section",
        "elements": [{"type": "text", "text": "Prepare launch"}]
      }]
    }]
  }]
}
```

Pass that object to `create_list_item`. To change it with `update_list_items`,
use `cells` instead of `initial_fields` and add `row_id` (the returned `Rec...`
item ID) to each cell. Other typed values include `user: ["U..."]`,
`date: ["2026-10-01"]`, `select: ["option_id"]` and `checkbox: true`.
See Slack's [field formats](https://docs.slack.dev/reference/methods/slackLists.items.create/).
Only supplied cells are updated. `update_list` updates metadata, not column
schema. There is no workspace-wide List discovery or full canvas-content reader
in this server: supply existing IDs from Slack links or tool responses.

## Reproducible dependencies

Runtime, development and build dependencies are pinned in `pyproject.toml`.
The committed `uv.lock` pins transitive dependencies for supported Python
versions. Use `uv sync --locked --extra dev` and `uv run --locked ...` from a
checkout; CI rejects a stale lockfile. To update dependencies intentionally,
change the exact versions, run `uv lock`, then test and commit both files.

`uvx --from git+...` and pip honor direct pins but do not use the checkout's
lockfile for transitive dependencies. For fully locked deployment, check out a
specific commit and launch `uv run --locked --project /path/to/checkout
slack-management-mcp`.

## Development

```bash
uv sync --locked --extra dev      # install deps including pytest
uv run --locked --extra dev pytest            # run the unit tests (Slack is mocked; no network)
```

The tests mock the Slack `WebClient`, so they run without a real workspace or
token.

## License

MIT
