# slack-management-mcp

A small, single-purpose [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) server for **adding users to Slack channels**. It exposes exactly three
tools and nothing else — it does **not** try to reproduce the full published
Slack MCP.

| Tool | What it does |
| --- | --- |
| `lookup_user` | Find a user by email address or user ID. |
| `lookup_channel` | Find a channel by name or channel ID. |
| `invite_user_to_channel` | Invite a user (by email or ID) to a channel (by name or ID). |

The server authenticates as a **bot** using a single bot token. You install the
app into your workspace once, and the server acts as that bot — it does not act
on behalf of an individual user and there is no per-request OAuth flow.

## Quick start

### 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Select your workspace and paste the contents of
   [`slack-app-manifest.yaml`](./slack-app-manifest.yaml).
3. Create the app, then click **Install to Workspace** and approve it.
4. Under **OAuth & Permissions**, copy the **Bot User OAuth Token** — it starts
   with `xoxb-`. This is the only secret you need.

### 2. Set the token

The server reads one environment variable:

```bash
export SLACK_BOT_TOKEN="xoxb-your-token-here"
```

### 3. Run it

The server runs over stdio and is launched by your MCP client. To run it on its
own (e.g. for a smoke test) with [`uv`](https://docs.astral.sh/uv/) installed:

```bash
# Straight from the repo, before it is published to PyPI:
uvx --from git+https://github.com/dbuxton/slack-management-mcp slack-management-mcp

# Local development from a checkout:
uv run slack-management-mcp
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

This server uses a **bot token** (`xoxb-`), not OAuth-per-user. The flow is:

1. You create and install the app **once** (steps above). Slack issues a bot
   token tied to the bot user in your workspace.
2. You give the server that token via `SLACK_BOT_TOKEN`.
3. Every action the server performs is done **as the bot**.

You do **not** need the app's Client ID, Client Secret, or Signing Secret at
runtime — those only matter if you implement a browser OAuth flow, which this
server intentionally does not. The bot token is the single credential.

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

### Important: the bot must be in the channel

To invite someone to a channel, **the bot itself must already be a member of
that channel**. For public channels the bot can join automatically; for private
channels you must add the bot manually (e.g. `/invite @slack-management-mcp` in
the channel). If the bot is not a member, `invite_user_to_channel` returns a
clear error explaining this.

## Development

```bash
uv sync --extra dev      # install deps including pytest
uv run pytest            # run the unit tests (Slack is mocked; no network)
```

The tests mock the Slack `WebClient`, so they run without a real workspace or
token.

## License

MIT
