# InvoiceShelf MCP Server

Multi-tenant MCP proxy server for InvoiceShelf, exposing **106 MCP tools** across 14 resource domains with full CRUD, domain-specific operations, and relationship management.

## Features

- Identity Passthrough — Extracts the Authorization: Bearer <token> header from incoming HTTP requests and forwards it to the InvoiceShelf API.
- Multi-Tenancy — Uses Python contextvars to maintain thread-safe user identity isolation.
- Full InvoiceShelf Coverage — 106 tools mapped to InvoiceShelf API endpoints across 14 resource domains.
- TOON Optimization — Bulk list responses are compressed using TOON to reduce token consumption.
- Efficient Gets — GET responses return only commonly used fields by default; full objects via `include_all_fields` flag.
- Tool Tags — Every tool tagged by operation type (read/write), access level (basic/primary/advanced), and project namespace.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| INVOICESHELF_BASE_URL | Yes | Docker-internal URL of the InvoiceShelf API |
| MCP_SERVER_PORT | Yes | Port the MCP server listens on |
| API_KEY | Yes | Bearer token for the InvoiceShelf API |
| ALLOW_ALL_AGGREGATE | No | When true, aggregate list tools honor include_all_fields (default false) |
| IS_STATEFUL | No | When true, uses stateful Streamable HTTP (default false) |

## Docker Deployment

```bash
docker build -t invoiceshelf-mcp .
docker run -d --name invoiceshelf-mcp --network dock-ext \
    -p 5821:5821 \
    -e INVOICESHELF_BASE_URL="http://invoiceshelf-app:8080" \
    -e MCP_SERVER_PORT=5821 \
    -e API_KEY="<your-api-key>" \
    invoiceshelf-mcp
```

The MCP server serves at `http://localhost:5821/mcp` (Streamable HTTP).

## Tool Groups

| Group | Tools | Description |
|-------|-------|-------------|
| Domain | 21 | Server status, bootstrap, dashboard, search, currencies, countries, timezones, next number, companies, abilities, exchange rates |
| Customers | 6 | CRUD + stats |
| Items | 5 | CRUD |
| Units | 5 | CRUD |
| Invoices | 10 | CRUD + send, clone, status, templates, preview |
| Estimates | 11 | CRUD + send, clone, status, convert, templates, preview |
| Expenses | 6 | CRUD + duplicate |
| Expense Categories | 5 | CRUD |
| Payments | 7 | CRUD + send, preview |
| Payment Methods | 5 | CRUD |
| Custom Fields | 5 | CRUD |
| Tax Types | 5 | CRUD |
| Notes | 5 | CRUD |
| Recurring Invoices | 5 | CRUD |
| Roles | 5 | CRUD |

## Testing

```bash
API_KEY="<your-api-key>" MCP_SERVER_PORT=5821 python -m src.test_runner
```
