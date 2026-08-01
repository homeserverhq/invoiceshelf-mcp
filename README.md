# InvoiceShelf MCP Multitenant Proxy Server

This repository contains a Model Context Protocol (MCP) server that acts
as a secure, multi-tenant proxy between an AI Assistant and the
InvoiceShelf backend API. It exposes **106 MCP tools** covering
14 resource domains with full CRUD, search, dashboard,
and relationship management.

## ✨ Features

- **🔑 Identity Passthrough** — Extracts the `Authorization: Bearer <token>`
  header from incoming HTTP requests and forwards it to the InvoiceShelf
  API without server-side authentication.
- **👥 Multi-Tenancy** — Uses Python `contextvars` to maintain thread-safe
  user identity isolation, ensuring all AI-driven actions are scoped to
  the authenticated user's permissions.
- **📊 Full InvoiceShelf Coverage** — 106 tools mapped to InvoiceShelf
  API endpoints across 14 resource domains.
- **⚡ TOON Optimization** — All list responses are automatically compressed
  using TOON (Token-Optimized Object Notation) to reduce token consumption
  and maximize context window efficiency.
- **⚡ Efficient Gets** — GET responses return only commonly used fields by
  default. Full objects are available via an `include_all_fields` flag.
- **🧪 Comprehensive Testing** — 149 automated tests covering all tool
  domains, run via the test runner pipeline.

## 🔧 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `INVOICESHELF_BASE_URL` | Yes | Docker-internal URL of the InvoiceShelf API |
| `MCP_SERVER_PORT` | Yes | Port number the MCP server listens on |
| `ALLOW_ALL_AGGREGATE` | No | When `true`, aggregate listing tools honor the `include_all_fields` parameter. When `false` (default), the parameter is silently forced to `False` for aggregate list operations. |
| `IS_STATEFUL` | No | When `true`, uses stateful Streamable HTTP with session tracking. When `false` (default), uses stateless mode. |

## 📦 Installation & Local Development

1. Ensure you have Python 3.12+ installed.
2. Install dependencies:
    ```bash
    pip install fastmcp httpx pydantic uvicorn toon-mcp-server
    ```
3. Run the server:
    ```bash
    export INVOICESHELF_BASE_URL=http://localhost:8080
    export MCP_SERVER_PORT=80
    python -m src.main
    ```

## 🐳 Docker Deployment

Build and run the server using Docker:

```bash
docker build -t invoiceshelf-mcp:latest .
docker run -d --name invoiceshelf-mcp \
    -e INVOICESHELF_BASE_URL="http://invoiceshelf-app:8080" \
    -e MCP_SERVER_PORT=80 \
    invoiceshelf-mcp:latest

The MCP server serves at `http://invoiceshelf-mcp:80/mcp`
(Streamable HTTP).
```

## ⚠️ Important Notes

- **📋 `include_all_fields`** — The `include_all_fields` parameter (available
  on all `get_*` and `list_*` tools) controls whether all available fields
  are included in responses. Defaults to `False` for performance; set to
  `True` only when additional fields are needed.
- **⚡ TOON Compression** — All list responses are automatically compressed
  using TOON (Token-Optimized Object Notation) to reduce token
  consumption by 30-60%.
- **📝 Required Fields & Defaults** — Each `create_*` tool requires specific
  key fields (e.g. `name` for resources). All other fields default to
  empty strings or reasonable values. The company assignment is
  automatically set to the authenticated user's company for most resources.

## 🛠️ API Tool Mapping

The server implements 106 MCP tools organized into the following
categories:

### 🧾 Invoices (10 tools)
- `list_all_invoices` — List all invoice records
- `get_invoice_by_id` — Get a single invoice by ID
- `create_invoice` — Create a new invoice
- `update_invoice` — Update an existing invoice
- `delete_invoices_by_id` — Delete an invoice by ID
- `send_invoice` — Send an invoice via email
- `clone_invoice` — Clone an existing invoice
- `change_invoice_status` — Change an invoice's status
- `list_invoice_templates` — List available invoice templates
- `get_invoice_send_preview` — Get an invoice send preview

### 📋 Estimates (11 tools)
- `list_all_estimates` — List all estimate records
- `get_estimate_by_id` — Get a single estimate by ID
- `create_estimate` — Create a new estimate
- `update_estimate` — Update an existing estimate
- `delete_estimates_by_id` — Delete an estimate by ID
- `send_estimate` — Send an estimate via email
- `clone_estimate` — Clone an existing estimate
- `change_estimate_status` — Change an estimate's status
- `convert_estimate_to_invoice` — Convert an estimate to an invoice
- `list_estimate_templates` — List available estimate templates
- `get_estimate_send_preview` — Get an estimate send preview

### 👥 Customers (6 tools)
- `list_all_customers` — List all customer records
- `get_customer_by_id` — Get a single customer by ID
- `create_customer` — Create a new customer
- `update_customer` — Update an existing customer
- `delete_customers_by_id` — Delete a customer by ID
- `get_customer_stats` — Get statistics for a single customer

### 📦 Items (5 tools)
- `list_all_items` — List all item records
- `get_item_by_id` — Get a single item by ID
- `create_item` — Create a new item
- `update_item` — Update an existing item
- `delete_items_by_id` — Delete an item by ID

### 📏 Units (5 tools)
- `list_all_units` — List all unit records
- `get_unit_by_id` — Get a single unit by ID
- `create_unit` — Create a new unit
- `update_unit` — Update an existing unit
- `delete_unit_by_id` — Delete a unit by ID

### 🔁 Recurring Invoices (5 tools)
- `list_all_recurring_invoices` — List all recurring invoice records
- `get_recurring_invoice_by_id` — Get a single recurring invoice by ID
- `create_recurring_invoice` — Create a new recurring invoice
- `update_recurring_invoice` — Update an existing recurring invoice
- `delete_recurring_invoices_by_id` — Delete a recurring invoice by ID

### 💳 Payments (7 tools)
- `list_all_payments` — List all payment records
- `get_payment_by_id` — Get a single payment by ID
- `create_payment` — Create a new payment
- `update_payment` — Update an existing payment
- `delete_payments_by_id` — Delete a payment by ID
- `send_payment` — Send a payment receipt via email
- `get_payment_send_preview` — Get a payment send preview

### 🏦 Payment Methods (5 tools)
- `list_all_payment_methods` — List all payment method records
- `get_payment_method_by_id` — Get a single payment method by ID
- `create_payment_method` — Create a new payment method
- `update_payment_method` — Update an existing payment method
- `delete_payment_method_by_id` — Delete a payment method by ID

### 💸 Expenses (6 tools)
- `list_all_expenses` — List all expense records
- `get_expense_by_id` — Get a single expense by ID
- `create_expense` — Create a new expense
- `update_expense` — Update an existing expense
- `delete_expenses_by_id` — Delete an expense by ID
- `duplicate_expense` — Duplicate an existing expense

### 🗂️ Expense Categories (5 tools)
- `list_all_expense_categories` — List all expense category records
- `get_expense_category_by_id` — Get a single expense category by ID
- `create_expense_category` — Create a new expense category
- `update_expense_category` — Update an existing expense category
- `delete_expense_category_by_id` — Delete an expense category by ID

### 🧮 Tax Types (5 tools)
- `list_all_tax_types` — List all tax type records
- `get_tax_type_by_id` — Get a single tax type by ID
- `create_tax_type` — Create a new tax type
- `update_tax_type` — Update an existing tax type
- `delete_tax_type_by_id` — Delete a tax type by ID

### 📝 Notes (5 tools)
- `list_all_notes` — List all note records
- `get_note_by_id` — Get a single note by ID
- `create_note` — Create a new note
- `update_note` — Update an existing note
- `delete_note_by_id` — Delete a note by ID

### 🏷️ Custom Fields (5 tools)
- `list_all_custom_fields` — List all custom field records
- `get_custom_field_by_id` — Get a single custom field by ID
- `create_custom_field` — Create a new custom field
- `update_custom_field` — Update an existing custom field
- `delete_custom_field_by_id` — Delete a custom field by ID

### 🛡️ Roles (5 tools)
- `list_all_roles` — List all role records
- `get_role_by_id` — Get a single role by ID
- `create_role` — Create a new role
- `update_role` — Update an existing role
- `delete_role_by_id` — Delete a role by ID

### 🌐 Domain (21 tools)
- `check_server_status` — Check connectivity to the InvoiceShelf backend
- `get_dashboard` — Get dashboard data
- `get_bootstrap` — Get application bootstrap data
- `search_customers_and_users` — Search across customers and users
- `search_users` — Search users by email
- `list_all_currencies` — List all currencies
- `list_used_currencies` — List currencies in use
- `list_all_countries` — List all countries
- `list_timezones` — List available timezones
- `list_date_formats` — List available date formats
- `list_time_formats` — List available time formats
- `get_next_number` — Get the next number for a resource
- `get_number_placeholders` — Get number placeholders for a format
- `get_current_company` — Get the current company
- `list_all_companies` — List all companies
- `list_abilities` — List all available abilities
- `get_recurring_invoice_frequency` — Get the next occurrence for a recurring frequency
- `get_exchange_rate` — Get an exchange rate for a currency
- `get_active_exchange_rate_provider` — Get the active exchange-rate provider
- `list_used_currencies_for_exchange` — List currencies used for exchange
- `list_supported_currencies` — List currencies supported by an exchange-rate provider
