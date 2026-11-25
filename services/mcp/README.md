# Company OS MCP surface

MCP tools live inside the core service (not a separate process):

- Handlers: `services/company_os/app/mcp/tools.py`
- HTTP: `GET /mcp/tools`, `POST /mcp/tools/{name}`, `POST /mcp/jsonrpc`
- Mounted from `app.main` on core (port 3300)

## Tools

| Tool | Write? | Purpose |
|------|--------|---------|
| `list_workspaces` | no | List workspaces |
| `get_status` | no | Open work / blockers / cost |
| `request_work` | yes | Create an engagement |
| `get_engagement` | no | Engagement detail |
| `approve` | yes | Decide an approval |
| `list_opportunities` | no | Idea Lab slate |
| `run_workflow` | yes | Trigger a workflow |
| `get_activity` | no | Recent activity |

## Quick check

```bash
curl -s http://127.0.0.1:3300/mcp/tools | jq
curl -s -X POST http://127.0.0.1:3300/mcp/tools/list_workspaces \
  -H 'content-type: application/json' \
  -d '{"name":"list_workspaces","arguments":{}}' | jq
```

Stdio transport (same handlers, no second business layer):

```bash
PYTHONPATH=services/company_os:. .venv/bin/python -m services.mcp.main
```

One JSON-RPC object per line on stdin. Write tools return the pending state when
approval is required — never a fake success.
