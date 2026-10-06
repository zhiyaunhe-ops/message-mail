# MCP server setup — connecting AI clients to Message-cli

`mcp_server.py` (repo root) exposes Message-cli as an MCP server over **stdio**:
7 curated tools (6 read-only + `send_mail`) covering the mail side (search,
read, threads, digest, send). It is a thin HTTP adapter — every tool call goes
to the Message-cli web app; this process stores nothing itself.

> This is an **example/template doc**: values in `<angle brackets>` are
> placeholders for your own machine. Copy it to `docs/mcp-setup.md`
> (gitignored) and fill them in if you want a concrete local guide.

An AI agent reading this can wire up a new client by itself: all configs below
are plain files, no GUI needed.

## Prerequisites

1. **The web app must be running on this machine**: `http://127.0.0.1:8791`
   (desktop shortcut / tray icon, or `python run.py serve --host 127.0.0.1 --port 8791`
   from the project root). Check: `curl http://127.0.0.1:8791/api/status`.
2. **Python 3.10+ on the client machine** with two packages:
   ```bash
   python -m pip install fastmcp httpx
   ```
   Any Python works — it does NOT have to be the web app's `.venv`.
3. Self-test (the server speaks stdio, so it will "hang" after printing the
   FastMCP banner — that is normal, Ctrl+C to exit):
   ```bash
   python <absolute path to this repo>/mcp_server.py
   ```

## Registration — the generic form

Every MCP client that supports stdio servers takes the same JSON shape, only
the file location differs:

```json
{
  "mcpServers": {
    "message-cli": {
      "command": "<absolute path to a python that has fastmcp installed>",
      "args": ["<absolute path to this repo>/mcp_server.py"]
    }
  }
}
```

Find a suitable interpreter with
`python -c "import sys; print(sys.executable)"` (run it in the environment
where you installed fastmcp).

### Where each client keeps its config

| Client | Config file | Key / format |
| --- | --- | --- |
| **ZCode** | `~/.zcode/cli/config.json` (user scope) or `<repo>/.zcode/config.json` (workspace) | nested `mcp.servers` |
| **Claude Desktop** | `%APPDATA%\Claude\claude_desktop_config.json` (Windows) / `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) | `mcpServers` (generic form above) |
| **Claude Code** | project `.mcp.json`, or `claude mcp add message-cli -- <python> <mcp_server.py>` | `mcpServers` |
| **Cursor** | `~/.cursor/mcp.json` (global) or `<project>/.cursor/mcp.json` | `mcpServers` |
| **Gemini CLI** | `~/.gemini/settings.json` | `mcpServers` |
| **Codex CLI** | `~/.codex/config.toml` | TOML: `[mcp_servers.message-cli]` with `command = "..."` and `args = ["..."]` |
| **VS Code (Copilot)** | `.vscode/mcp.json` | top-level `servers` (not `mcpServers`) |

ZCode additionally accepts the cross-tool fallback `~/.agents/mcp.json` with a
`mcpServers` key — handy if you want one file shared by several agents.
Register the server in ONE client only per machine; a duplicate name across
scopes silently shadows.

Restart the client (or start a new session) after editing; MCP servers connect
at session start.

## Environment variables (optional)

| Variable | Default | Meaning |
| --- | --- | --- |
| `MESSAGE_CLI_URL` | `http://127.0.0.1:8791` | Base URL of the Message-cli web app |
| `MESSAGE_CLI_TIMEOUT` | `90` | Per-request timeout in seconds (an initial full-folder sync can be slow) |

Set them in the same server entry if the client supports an `env` field:

```json
{ "command": "...", "args": ["..."], "env": { "MESSAGE_CLI_URL": "http://127.0.0.1:8791" } }
```

## Accessing from another machine on the tailnet

The stdio server must always run **on the device where the client runs** (the
client spawns it). On a laptop/phone-adjacent device, install Python + fastmcp
there and point it at the tailnet-exposed web app instead of localhost:

```
MESSAGE_CLI_URL=https://<your-machine>.<your-tailnet>.ts.net:<port>
```

This is the same private-tailnet HTTPS front door the WebUI already uses (the
repo's tailnet config/scripts know the real value — never hardcode a real
tailnet hostname into committed files). Only the read endpoints and
`send_mail` exist there; tool latency includes the tailnet round trip.

## Verify it works

Ask the agent to call `mail_status` — it should return the active mail
account, folder counts and module availability. Typical failures:

- **Every tool returns `{"error": "... not reachable ... Start it: python run.py serve ..."}`**
  → the web app is down; start it and retry (no client restart needed).
- **Client shows the server as failed / no tools** → `command` does not point
  at a Python with fastmcp installed; run the self-test with that exact interpreter.
- **HTTP 404 from /api/...** → the running web app predates the endpoint; restart
  the 8791 service to load current code.

## Security boundary

- All tools are read-only except `send_mail`, which really sends SMTP email.
  Its tool description instructs the agent to confirm with the user first, but
  enforcement is the client's job — only register this server with clients you
  trust and review outgoing mail in the Sent folder.
- Not exposed via MCP at all: delete/trash mail, account management, sync/config
  writes. Those stay in the WebUI.
- Do not expose `mcp_server.py` itself over the network; it has no
  authentication of its own. Remote access goes through the tailnet URL as
  described above (private tailnet only).
