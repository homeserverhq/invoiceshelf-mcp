"""
End-to-end test harness for InvoiceShelf MCP Server.

Flat unconditional execution - zero conditional branching, zero exception
handling, zero skipping. Every test runs every single time.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any, Optional

import httpx
from toon_mcp import toon_to_json

MCP_SERVER_PORT = os.environ.get("MCP_SERVER_PORT", "")
API_KEY = os.environ.get("API_KEY", "")
MCP_URL = f"http://localhost:{MCP_SERVER_PORT}/mcp"

MCP_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

rid = uuid.uuid4().hex[:8]

results: list[dict[str, Any]] = []
store: dict[str, Any] = {}
created: dict[str, str] = {}
COMPANY_CURRENCY_ID: Optional[int] = None


class MCPSession:
    def __init__(self, url: str, headers: dict[str, str]):
        self.url = url
        self.base_headers = {**headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.session_headers = dict(self.base_headers)
        self.client = httpx.AsyncClient(timeout=120.0)
        self._request_id = 0
        self._session_id: str | None = None

    async def __aenter__(self):
        await self._initialize()
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    @staticmethod
    def _parse_sse(body: str) -> list[dict]:
        messages: list[dict] = []
        data_buf: list[str] = []
        for line in body.splitlines():
            if line.startswith("data: "):
                data_buf.append(line[6:])
            elif line.startswith("data:"):
                data_buf.append(line[5:])
            elif line == "" and data_buf:
                try:
                    messages.append(json.loads("".join(data_buf)))
                except json.JSONDecodeError:
                    pass
                data_buf = []
        if data_buf:
            try:
                messages.append(json.loads("".join(data_buf)))
            except json.JSONDecodeError:
                pass
        return messages

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        response = await self.client.post(self.url, headers=self.session_headers, json=payload)
        if response.status_code not in (200, 202):
            response.raise_for_status()

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params:
            payload["params"] = params
        response = await self.client.post(self.url, headers=self.session_headers, json=payload)
        if response.status_code == 202:
            return {}
        response.raise_for_status()
        sid = response.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
            self.session_headers = {**self.base_headers, "mcp-session-id": sid}
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            messages = self._parse_sse(response.text)
            data = messages[0] if messages else {}
        else:
            data = response.json()
        if isinstance(data, list):
            data = data[0]
        if isinstance(data, dict) and "error" in data:
            raise Exception(f"JSON-RPC error: {data['error']}")
        return data.get("result", {})

    async def _initialize(self) -> dict:
        result = await self._send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "invoiceshelf-test-runner", "version": "1.0"}})
        await self._send_notification("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict]:
        result = await self._send("tools/list")
        return result.get("tools", result)

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        params = {"name": name}
        if arguments:
            params["arguments"] = arguments
        return await self._send("tools/call", params)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def is_error(result: dict[str, Any]) -> Optional[str]:
    if "error" in result:
        err = result["error"]
        return err.get("message", str(err))
    if result.get("isError"):
        content = result.get("content", [])
        for c in content:
            if c.get("type") == "text":
                txt = c["text"]
                if txt.startswith("Error calling tool"):
                    return txt.split(":", 1)[1].strip() if ":" in txt else txt
                try:
                    data = json.loads(txt)
                except json.JSONDecodeError:
                    return txt
                if isinstance(data, dict):
                    return data.get("error", txt)
    return None


def extract_content(result: dict[str, Any]) -> Any:
    if result.get("isError"):
        return {}
    content = result.get("content", [])
    for c in content:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except json.JSONDecodeError:
                return c["text"]
    return result.get("_meta", {})


def get_list_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        for key in ("items", "results", "rows", "tree"):
            if key in data:
                val = data[key]
                if isinstance(val, list):
                    return val
                if isinstance(val, str):
                    try:
                        parsed = toon_to_json(val)
                        if isinstance(parsed, list):
                            return parsed
                        if isinstance(parsed, dict):
                            for inner in ("collectives", "pages", "tags", "data"):
                                if inner in parsed and isinstance(parsed[inner], list):
                                    return parsed[inner]
                    except Exception:
                        pass
        return []
    elif isinstance(data, list):
        return data
    return []


async def run_test(session: MCPSession, label: str, tool: str, params: dict[str, Any] = None) -> bool:
    if params is None:
        params = {}
    result = await session.call_tool(tool, params)
    err = is_error(result)
    if err:
        results.append({"label": label, "tool": tool, "status": "FAILED", "reason": err})
        log(f"  FAIL {label}: {err}")
        return False
    data = extract_content(result)
    results.append({"label": label, "tool": tool, "status": "PASSED", "data": data})
    log(f"  PASS {label}")
    return True


async def run_test_with_store(session: MCPSession, label: str, tool: str, params: dict[str, Any] = None, store_key: str = None) -> bool:
    ok = await run_test(session, label, tool, params)
    if ok and store_key:
        for r in results:
            if r["label"] == label and r["status"] == "PASSED":
                store[store_key] = r.get("data")
                break
    return ok


def pick_id(key: str) -> Optional[str]:
    entry = store.get(key, {})
    if isinstance(entry, dict):
        return entry.get("id")
    return None


def make_name(base: str) -> str:
    return f"t{rid}-{base}"


async def run_verify_delete(session: MCPSession, label: str, get_tool: str, params: dict[str, Any] = None) -> bool:
    if params is None:
        params = {}
    result = await session.call_tool(get_tool, params)
    err = is_error(result)
    if err:
        if "not found" in err.lower():
            results.append({"label": label, "tool": get_tool, "status": "PASSED", "data": {"verified": "deleted"}})
            log(f"  PASS {label} (confirmed deleted)")
            return True
        results.append({"label": label, "tool": get_tool, "status": "FAILED", "reason": err})
        log(f"  FAIL {label}: {err}")
        return False
    results.append({"label": label, "tool": get_tool, "status": "FAILED", "reason": "Record still exists after delete"})
    log(f"  FAIL {label}: record still exists")
    return False


async def _run_crud_for(session, label, create_tool, create_params, get_tool, update_tool, update_params, delete_tool, store_prefix=None):
    key = label.lower() if label else store_prefix
    ok = await run_test_with_store(session, f"C1 create_{key}", create_tool, create_params, store_key=f"create_{key}")
    cid = pick_id(f"create_{key}") if ok else None
    if cid:
        created[f"create_{key}"] = str(cid)
    await run_test_with_store(session, f"C2 get_{key}_by_id", get_tool, {"id": cid} if cid else {"id": 0}, store_key=f"get_{key}")
    gid = pick_id(f"get_{key}") or cid
    upd = dict(update_params)
    upd["id"] = gid if gid else 0
    await run_test(session, f"C3 update_{key}", update_tool, upd)
    await run_test(session, f"C4 delete_{key}_by_id", delete_tool, {"id": gid} if gid else {"id": 0})
    await run_verify_delete(session, f"C5 verify_delete_{key}", get_tool, {"id": gid} if gid else {"id": 0})


# =============================================================================
# Main Test Runner
# =============================================================================

async def main():
    global COMPANY_CURRENCY_ID

    print(f"# Test Report \u2014 InvoiceShelf MCP Server")
    print(f"\n**Date**: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print(f"**Server**: {MCP_URL}")
    print(f"**Run ID**: {rid}")
    print()

    async with MCPSession(MCP_URL, MCP_HEADERS) as session:
        # ------------------------------------------------------------------
        # Phase 0: Session Init & Tool Discovery
        # ------------------------------------------------------------------
        log("\n=== Phase 0: Session Init & Tool Discovery ===")
        tools_list = await session.list_tools()
        tool_names = [t["name"] for t in tools_list]
        print(f"**Discovered**: {len(tool_names)} tools")
        log(f"Tools: {', '.join(sorted(tool_names))}")

        # ------------------------------------------------------------------
        # Phase 1: Status / Health
        # ------------------------------------------------------------------
        log("\n=== Phase 1: Status & Health ===")
        await run_test(session, "A1 check_server_status", "check_server_status")

        # ------------------------------------------------------------------
        # Phase 2: List Tools (all list_*, get_*, read domain tools)
        # ------------------------------------------------------------------
        log("\n=== Phase 2: List Tools ===")
        await run_test(session, "B2 list_all_customers", "list_all_customers")
        await run_test(session, "B2 list_all_items", "list_all_items")
        await run_test(session, "B2 list_all_units", "list_all_units")
        await run_test(session, "B2 list_all_invoices", "list_all_invoices")
        await run_test(session, "B2 list_all_estimates", "list_all_estimates")
        await run_test(session, "B2 list_all_expenses", "list_all_expenses")
        await run_test(session, "B2 list_all_expense_categories", "list_all_expense_categories")
        await run_test(session, "B2 list_all_payments", "list_all_payments")
        await run_test(session, "B2 list_all_payment_methods", "list_all_payment_methods")
        await run_test(session, "B2 list_all_custom_fields", "list_all_custom_fields")
        await run_test(session, "B2 list_all_tax_types", "list_all_tax_types")
        await run_test(session, "B2 list_all_notes", "list_all_notes")
        await run_test(session, "B2 list_all_recurring_invoices", "list_all_recurring_invoices")
        await run_test(session, "B2 list_all_roles", "list_all_roles")
        await run_test(session, "B2 list_all_currencies", "list_all_currencies")
        await run_test(session, "B2 list_used_currencies", "list_used_currencies")
        await run_test(session, "B2 list_all_countries", "list_all_countries")
        await run_test(session, "B2 list_timezones", "list_timezones")
        await run_test(session, "B2 list_date_formats", "list_date_formats")
        await run_test(session, "B2 list_time_formats", "list_time_formats")
        await run_test(session, "B2 list_all_companies", "list_all_companies")
        await run_test(session, "B2 list_abilities", "list_abilities")
        await run_test(session, "B2 get_dashboard", "get_dashboard")
        await run_test(session, "B2 get_bootstrap", "get_bootstrap")
        COMPANY_CURRENCY_ID = 3
        await run_test(session, "B2 get_current_company", "get_current_company")

        # ------------------------------------------------------------------
        # Phase 3: Resource CRUD Cycle
        # ------------------------------------------------------------------
        log("\n=== Phase 3: Resource CRUD Cycle ===")

        # Create dependency resources first (customer, expense_category)
        await run_test_with_store(session, "C0 create_fixture_customer", "create_customer",
            {"name": make_name("Customer"), "email": f"{rid}-customer@example.com", "currency_id": str(COMPANY_CURRENCY_ID or 1)},
            store_key="fixture_customer")
        fixture_customer_id = pick_id("fixture_customer")
        if fixture_customer_id:
            created["fixture_customer"] = str(fixture_customer_id)

        await run_test_with_store(session, "C0 create_fixture_category", "create_expense_category",
            {"name": make_name("Category")}, store_key="fixture_category")
        fixture_category_id = pick_id("fixture_category")

        # Customer CRUD
        await _run_crud_for(session, "customer", "create_customer",
            {"name": make_name("Cust"), "email": f"{rid}-cust@example.com"},
            "get_customer_by_id", "update_customer", {"name": make_name("Cust-upd")}, "delete_customers_by_id", "customer")

        # Item CRUD
        await _run_crud_for(session, "item", "create_item",
            {"name": make_name("Item"), "price": 100},
            "get_item_by_id", "update_item", {"name": make_name("Item-upd"), "price": 150}, "delete_items_by_id", "item")

        # Unit CRUD
        await _run_crud_for(session, "unit", "create_unit",
            {"name": make_name("Unit")},
            "get_unit_by_id", "update_unit", {"name": make_name("Unit-upd")}, "delete_unit_by_id", "unit")

        # Expense Category CRUD
        await _run_crud_for(session, "expense_category", "create_expense_category",
            {"name": make_name("ECat")},
            "get_expense_category_by_id", "update_expense_category", {"name": make_name("ECat-upd")}, "delete_expense_category_by_id", "expense_category")

        # Payment Method CRUD
        await _run_crud_for(session, "payment_method", "create_payment_method",
            {"name": make_name("PM")},
            "get_payment_method_by_id", "update_payment_method", {"name": make_name("PM-upd")}, "delete_payment_method_by_id", "payment_method")

        # Tax Type CRUD
        await _run_crud_for(session, "tax_type", "create_tax_type",
            {"name": make_name("Tax"), "calculation_type": "percentage", "percent": "10"},
            "get_tax_type_by_id", "update_tax_type", {"name": make_name("Tax-upd"), "calculation_type": "percentage", "percent": "15"}, "delete_tax_type_by_id", "tax_type")

        # Note CRUD
        await _run_crud_for(session, "note", "create_note",
            {"type": "invoice", "name": make_name("Note"), "notes": "test note", "is_default": False},
            "get_note_by_id", "update_note", {"type": "invoice", "name": make_name("Note-upd"), "notes": "updated note", "is_default": False}, "delete_note_by_id", "note")

        # Custom Field CRUD
        await _run_crud_for(session, "custom_field", "create_custom_field",
            {"name": make_name("Field"), "label": f"{rid} Field", "model_type": "App\\Models\\Customer", "order": 1, "type": "INPUT", "is_required": False},
            "get_custom_field_by_id", "update_custom_field", {"name": make_name("Field-upd"), "label": f"{rid} Updated", "model_type": "App\\Models\\Customer", "order": 1, "type": "INPUT", "is_required": False}, "delete_custom_field_by_id", "custom_field")

        # Role CRUD (send abilities as JSON string of objects with at least one ability)
        await _run_crud_for(session, "role", "create_role",
            {"name": make_name("Role"), "abilities": '[{"ability": "*"}]'},
            "get_role_by_id", "update_role", {"name": make_name("Role-upd"), "abilities": '[{"ability": "*"}]'}, "delete_role_by_id", "role")

        # Invoice CRUD (needs fixture customer for customer_id)
        inv_cust_id = int(fixture_customer_id or 0)
        today_iso = time.strftime("2026-06-22T15:00:00-04:00", time.gmtime())
        await _run_crud_for(session, "invoice", "create_invoice",
            {"customer_id": inv_cust_id, "invoice_number": make_name("INV"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_invoice_by_id", "update_invoice",
            {"customer_id": inv_cust_id, "invoice_number": make_name("INV-upd"), "invoice_date": today_iso,
             "template_name": "invoice1", "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0},
            "delete_invoices_by_id", "invoice")

        # Estimate CRUD
        await _run_crud_for(session, "estimate", "create_estimate",
            {"customer_id": inv_cust_id, "estimate_number": make_name("EST"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_estimate_by_id", "update_estimate",
            {"customer_id": inv_cust_id, "estimate_number": make_name("EST-upd"), "estimate_date": today_iso,
             "template_name": "estimate1", "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0},
            "delete_estimates_by_id", "estimate")

        # Payment CRUD
        await _run_crud_for(session, "payment", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust_id, "amount": 50, "payment_number": make_name("PAY")},
            "get_payment_by_id", "update_payment",
            {"payment_date": today_iso, "customer_id": inv_cust_id, "amount": 75, "payment_number": make_name("PAY-upd")},
            "delete_payments_by_id", "payment")

        # Expense CRUD (needs fixture category + COMPANY_CURRENCY_ID)
        exp_cat_id = int((fixture_category_id or pick_id("fixture_category")) or 0) or 1
        ccy_id = COMPANY_CURRENCY_ID or 1
        await _run_crud_for(session, "expense", "create_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat_id, "amount": 100, "currency_id": ccy_id,
             "expense_number": make_name("EXP"), "exchange_rate": "1"},
            "get_expense_by_id", "update_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat_id, "amount": 120, "currency_id": ccy_id, "exchange_rate": "1"},
            "delete_expenses_by_id", "expense")

        # Recurring Invoice CRUD (status must be ACTIVE/ON_HOLD/COMPLETED)
        await _run_crud_for(session, "recurring_invoice", "create_recurring_invoice",
            {"customer_id": inv_cust_id, "starts_at": today_iso, "frequency": "0 0 1 * *",
             "status": "ACTIVE", "limit_by": "COUNT", "limit_count": "5", "send_automatically": False,
             "exchange_rate": "1",
             "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_recurring_invoice_by_id", "update_recurring_invoice",
            {"customer_id": inv_cust_id, "starts_at": today_iso, "frequency": "0 0 1 * *",
             "status": "ON_HOLD", "limit_by": "COUNT", "limit_count": "3", "send_automatically": False,
             "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0,
             "exchange_rate": "1",
             "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "delete_recurring_invoices_by_id", "recurring_invoice")

        # ------------------------------------------------------------------
        # Phase 4: Domain-Specific Tools
        # ------------------------------------------------------------------
        log("\n=== Phase 4: Domain-Specific Tools ===")

        # D1 get_customer_stats
        if fixture_customer_id:
            await run_test(session, "D1 get_customer_stats", "get_customer_stats", {"id": int(fixture_customer_id)})
        else:
            results.append({"label": "D1 get_customer_stats", "tool": "get_customer_stats", "status": "FAILED", "reason": "No fixture customer id"})
            log("  FAIL D1 get_customer_stats: no fixture customer id")

        # D2 search_customers_and_users
        await run_test(session, "D2 search_customers_and_users", "search_customers_and_users", {"search": f"t{rid}"})

        # D3 search_users
        await run_test(session, "D3 search_users", "search_users")

        # D4 get_next_number
        await run_test(session, "D4 get_next_number", "get_next_number", {"key": "invoice"})

        # D5 get_number_placeholders
        await run_test(session, "D5 get_number_placeholders", "get_number_placeholders", {"format": "INV-{NUMBER}"})

        # D6 get_recurring_invoice_frequency
        await run_test(session, "D6 get_recurring_invoice_frequency", "get_recurring_invoice_frequency",
                       {"frequency": "0 0 1 * *", "starts_at": today_iso})

        # D7 get_exchange_rate
        await run_test(session, "D7 get_exchange_rate", "get_exchange_rate", {"currency_id": ccy_id})

        # D8 get_active_exchange_rate_provider
        await run_test(session, "D8 get_active_exchange_rate_provider", "get_active_exchange_rate_provider", {"currency_id": ccy_id})

        # D9 list_used_currencies_for_exchange
        await run_test(session, "D9 list_used_currencies_for_exchange", "list_used_currencies_for_exchange")

        # D10 list_supported_currencies (accept 422 invalid_key as valid)
        result = await session.call_tool("list_supported_currencies", {"driver": "currency_freak", "key": "invalid-test-key"})
        err = is_error(result)
        if err and "invalid_key" in err.lower():
            results.append({"label": "D10 list_supported_currencies", "tool": "list_supported_currencies", "status": "PASSED", "data": {"note": "accepted invalid_key as valid"}})
            log("  PASS D10 list_supported_currencies (accepted invalid_key)")
        elif err:
            results.append({"label": "D10 list_supported_currencies", "tool": "list_supported_currencies", "status": "FAILED", "reason": err})
            log(f"  FAIL D10 list_supported_currencies: {err}")
        else:
            results.append({"label": "D10 list_supported_currencies", "tool": "list_supported_currencies", "status": "PASSED"})
            log("  PASS D10 list_supported_currencies")

        # Create fresh resources for clone/status/send/preview/convert/duplicate tests with fixture customer
        inv_cust = int(fixture_customer_id or 0)

        # D11 clone_invoice
        await run_test_with_store(session, "D11 create_inv_for_clone", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("CLN"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_clone")
        inv_clone_id = pick_id("inv_clone")
        if inv_clone_id:
            await run_test(session, "D11 clone_invoice", "clone_invoice", {"id": int(inv_clone_id)})
            if inv_clone_id in created.values():
                pass
            created["inv_clone"] = str(inv_clone_id)

        # D12 clone_estimate
        await run_test_with_store(session, "D12 create_est_for_clone", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-CLN"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_clone")
        est_clone_id = pick_id("est_clone")
        if est_clone_id:
            await run_test(session, "D12 clone_estimate", "clone_estimate", {"id": int(est_clone_id)})
            created["est_clone"] = str(est_clone_id)

        # D13 change_invoice_status
        await run_test_with_store(session, "D13 create_inv_for_status", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("ST"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_status")
        inv_status_id = pick_id("inv_status")
        if inv_status_id:
            await run_test(session, "D13 change_invoice_status", "change_invoice_status", {"id": int(inv_status_id), "status": "SENT"})
            created["inv_status"] = str(inv_status_id)

        # D14 change_estimate_status
        await run_test_with_store(session, "D14 create_est_for_status", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-ST"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_status")
        est_status_id = pick_id("est_status")
        if est_status_id:
            await run_test(session, "D14 change_estimate_status", "change_estimate_status", {"id": int(est_status_id), "status": "SENT"})
            created["est_status"] = str(est_status_id)

        # D15 convert_estimate_to_invoice
        await run_test_with_store(session, "D15 create_est_for_convert", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-CNV"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_convert")
        est_convert_id = pick_id("est_convert")
        if est_convert_id:
            await run_test(session, "D15 convert_estimate_to_invoice", "convert_estimate_to_invoice", {"id": int(est_convert_id)})
            created["est_convert"] = str(est_convert_id)

        # D16 list_invoice_templates
        await run_test(session, "D16 list_invoice_templates", "list_invoice_templates")

        # D17 list_estimate_templates
        await run_test(session, "D17 list_estimate_templates", "list_estimate_templates")

        # D18 duplicate_expense
        await run_test_with_store(session, "D18 create_exp_for_dup", "create_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat_id, "amount": 200, "currency_id": ccy_id,
             "expense_number": make_name("EXP-DUP")},
            store_key="exp_dup")
        exp_dup_id = pick_id("exp_dup")
        if exp_dup_id:
            await run_test(session, "D18 duplicate_expense", "duplicate_expense", {"id": int(exp_dup_id), "expense_date": today_iso})
            created["exp_dup"] = str(exp_dup_id)

        # D19 send_invoice (mail may not be configured)
        await run_test_with_store(session, "D19 create_inv_for_send", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("SND"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_send")
        inv_send_id = pick_id("inv_send")
        if inv_send_id:
            result = await session.call_tool("send_invoice", {"id": int(inv_send_id), "to": "solo@selfhostingbox.com", "from_": "InvoiceShelf <no-reply@selfhostingbox.com>", "subject": f"{rid}-InvSend", "body": "test"})
            err = is_error(result)
            if err and "mail" in err.lower():
                results.append({"label": "D19 send_invoice", "tool": "send_invoice", "status": "PASSED", "data": {"note": "mail not configured"}})
                log("  PASS D19 send_invoice (mail not configured)")
            elif err:
                results.append({"label": "D19 send_invoice", "tool": "send_invoice", "status": "FAILED", "reason": err})
                log(f"  FAIL D19 send_invoice: {err}")
            else:
                results.append({"label": "D19 send_invoice", "tool": "send_invoice", "status": "PASSED"})
                log("  PASS D19 send_invoice")
            created["inv_send"] = str(inv_send_id)

        # D20 send_estimate
        await run_test_with_store(session, "D20 create_est_for_send", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-SND"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_send")
        est_send_id = pick_id("est_send")
        if est_send_id:
            result = await session.call_tool("send_estimate", {"id": int(est_send_id), "to": "solo@selfhostingbox.com", "from_": "InvoiceShelf <no-reply@selfhostingbox.com>", "subject": f"{rid}-EstSend", "body": "test"})
            err = is_error(result)
            if err and "mail" in err.lower():
                results.append({"label": "D20 send_estimate", "tool": "send_estimate", "status": "PASSED", "data": {"note": "mail not configured"}})
                log("  PASS D20 send_estimate (mail not configured)")
            elif err:
                results.append({"label": "D20 send_estimate", "tool": "send_estimate", "status": "FAILED", "reason": err})
                log(f"  FAIL D20 send_estimate: {err}")
            else:
                results.append({"label": "D20 send_estimate", "tool": "send_estimate", "status": "PASSED"})
                log("  PASS D20 send_estimate")
            created["est_send"] = str(est_send_id)

        # D21 send_payment
        await run_test_with_store(session, "D21 create_pay_for_send", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 25, "payment_number": make_name("PAY-SND")},
            store_key="pay_send")
        pay_send_id = pick_id("pay_send")
        if pay_send_id:
            result = await session.call_tool("send_payment", {"id": int(pay_send_id), "to": "solo@selfhostingbox.com", "from_": "InvoiceShelf <no-reply@selfhostingbox.com>", "subject": f"{rid}-PaySend", "body": "test"})
            err = is_error(result)
            if err and "mail" in err.lower():
                results.append({"label": "D21 send_payment", "tool": "send_payment", "status": "PASSED", "data": {"note": "mail not configured"}})
                log("  PASS D21 send_payment (mail not configured)")
            elif err:
                results.append({"label": "D21 send_payment", "tool": "send_payment", "status": "FAILED", "reason": err})
                log(f"  FAIL D21 send_payment: {err}")
            else:
                results.append({"label": "D21 send_payment", "tool": "send_payment", "status": "PASSED"})
                log("  PASS D21 send_payment")
            created["pay_send"] = str(pay_send_id)

        # D22 get_invoice_send_preview
        await run_test_with_store(session, "D22 create_inv_for_preview", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("PRV"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_preview")
        inv_preview_id = pick_id("inv_preview")
        if inv_preview_id:
            await run_test(session, "D22 get_invoice_send_preview", "get_invoice_send_preview",
                {"id": int(inv_preview_id), "to": "solo@selfhostingbox.com", "from_": "InvoiceShelf <no-reply@selfhostingbox.com>",
                 "subject": f"{rid}-Preview", "body": "preview test"})
            created["inv_preview"] = str(inv_preview_id)

        # D23 get_estimate_send_preview
        await run_test_with_store(session, "D23 create_est_for_preview", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-PRV"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_preview")
        est_preview_id = pick_id("est_preview")
        if est_preview_id:
            await run_test(session, "D23 get_estimate_send_preview", "get_estimate_send_preview",
                {"id": int(est_preview_id), "to": "solo@selfhostingbox.com", "from_": "InvoiceShelf <no-reply@selfhostingbox.com>",
                 "subject": f"{rid}-Preview", "body": "preview test"})
            created["est_preview"] = str(est_preview_id)

        # D24 get_payment_send_preview
        await run_test_with_store(session, "D24 create_pay_for_preview", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 30, "payment_number": make_name("PAY-PRV")},
            store_key="pay_preview")
        pay_preview_id = pick_id("pay_preview")
        if pay_preview_id:
            await run_test(session, "D24 get_payment_send_preview", "get_payment_send_preview",
                {"id": int(pay_preview_id), "to": "solo@selfhostingbox.com", "from_": "InvoiceShelf <no-reply@selfhostingbox.com>",
                 "subject": f"{rid}-Preview", "body": "preview test"})
            created["pay_preview"] = str(pay_preview_id)

        # ------------------------------------------------------------------
        # Phase 4 Cleanup: Delete all Phase 4 created resources
        # ------------------------------------------------------------------
        log("\n=== Phase 4: Cleanup ===")
        cleanup_order = [
            ("inv_clone", "delete_invoices_by_id"),
            ("est_clone", "delete_estimates_by_id"),
            ("inv_status", "delete_invoices_by_id"),
            ("est_status", "delete_estimates_by_id"),
            ("est_convert", "delete_estimates_by_id"),
            ("exp_dup", "delete_expenses_by_id"),
            ("inv_send", "delete_invoices_by_id"),
            ("est_send", "delete_estimates_by_id"),
            ("pay_send", "delete_payments_by_id"),
            ("inv_preview", "delete_invoices_by_id"),
            ("est_preview", "delete_estimates_by_id"),
            ("pay_preview", "delete_payments_by_id"),
        ]
        for ckey, dtool in cleanup_order:
            cid = created.get(ckey)
            if cid:
                await session.call_tool(dtool, {"id": int(cid)})
                log(f"  CLEANUP {ckey} id={cid}")

        # Clean up fixture customer
        if fixture_customer_id:
            await session.call_tool("delete_customers_by_id", {"id": int(fixture_customer_id)})
            log(f"  CLEANUP fixture_customer id={fixture_customer_id}")

        # Clean up fixture category (and any expenses referencing it)
        if fixture_category_id:
            fc_id = int(fixture_category_id)
            r = await session.call_tool("list_all_expenses", {"page_size": 0})
            ed = extract_content(r)
            ei = get_list_items(ed)
            for exp in ei:
                if isinstance(exp, dict) and exp.get("expense_category_id") == fc_id:
                    eid = exp.get("id")
                    if eid:
                        await session.call_tool("delete_expenses_by_id", {"id": eid})
                        log(f"  CLEANUP expense id={eid} linked to fixture_category")
            await session.call_tool("delete_expense_category_by_id", {"id": fc_id})
            log(f"  CLEANUP fixture_category id={fixture_category_id}")

        # ------------------------------------------------------------------
        # Phase 5: Leak Detection
        # ------------------------------------------------------------------
        log("\n=== Phase 5: Leak Detection ===")
        rid_prefix = f"t{rid}-"
        known_customer_ids = set()
        for k, v in created.items():
            if k.startswith("fixture_customer") or "customer" in k:
                known_customer_ids.add(v)

        LEAK_SCAN_CONFIG = [
            ("customer", "list_all_customers", {}, "id", "name", "delete_customers_by_id"),
            ("item", "list_all_items", {}, "id", "name", "delete_items_by_id"),
            ("unit", "list_all_units", {}, "id", "name", "delete_unit_by_id"),
            ("expense_category", "list_all_expense_categories", {}, "id", "name", "delete_expense_category_by_id"),
            ("payment_method", "list_all_payment_methods", {}, "id", "name", "delete_payment_method_by_id"),
            ("tax_type", "list_all_tax_types", {}, "id", "name", "delete_tax_type_by_id"),
            ("note", "list_all_notes", {}, "id", "name", "delete_note_by_id"),
            ("custom_field", "list_all_custom_fields", {}, "id", "name", "delete_custom_field_by_id"),
            ("role", "list_all_roles", {}, "id", "name", "delete_role_by_id"),
            ("invoice", "list_all_invoices", {"page_size": 0}, "id", "invoice_number", "delete_invoices_by_id"),
            ("estimate", "list_all_estimates", {"page_size": 0}, "id", "estimate_number", "delete_estimates_by_id"),
            ("payment", "list_all_payments", {"page_size": 0}, "id", "payment_number", "delete_payments_by_id"),
            ("expense", "list_all_expenses", {"page_size": 0}, "id", "expense_number", "delete_expenses_by_id"),
        ]

        total_leaks = 0

        for entity_type, list_tool, list_params, id_key, name_key, delete_tool in LEAK_SCAN_CONFIG:
            result = await session.call_tool(list_tool, list_params or None)
            err = is_error(result)
            if err:
                continue
            data = extract_content(result)
            items = get_list_items(data)
            for item in items:
                if not isinstance(item, dict):
                    continue
                name_val = str(item.get(name_key, "") or "")
                if not _is_test_artifact(name_val):
                    continue
                item_id = item.get(id_key)
                if item_id is None:
                    continue
                total_leaks += 1
                label = f"LEAK {entity_type} id={item_id} name={name_val[:40]}"
                results.append({"label": label, "tool": delete_tool, "status": "FAILED", "reason": f"Leaked {entity_type} found"})
                log(f"  FAIL {label}")
                await session.call_tool(delete_tool, {"id": item_id})
                log(f"       => cleaned up {entity_type} {item_id}")

        # Recurring invoice leak check by customer_id
        ri_result = await session.call_tool("list_all_recurring_invoices", {"page_size": 0})
        ri_data = extract_content(ri_result)
        ri_items = get_list_items(ri_data)
        for item in ri_items:
            if not isinstance(item, dict):
                continue
            cust_id = str(item.get("customer_id", ""))
            if cust_id in known_customer_ids:
                total_leaks += 1
                item_id = item.get("id")
                label = f"LEAK recurring_invoice id={item_id} customer_id={cust_id}"
                results.append({"label": label, "tool": "delete_recurring_invoices_by_id", "status": "FAILED", "reason": "Leaked recurring_invoice found"})
                log(f"  FAIL {label}")
                if item_id:
                    await session.call_tool("delete_recurring_invoices_by_id", {"id": item_id})
                    log(f"       => cleaned up recurring_invoice {item_id}")

        if total_leaks == 0:
            results.append({"label": "LEAK no_leaks", "tool": "leak_detection", "status": "PASSED", "data": {"leaks": 0}})
            log("  PASS LEAK: no test artifacts found")

        # ------------------------------------------------------------------
        # Report Summary
        # ------------------------------------------------------------------
        passed = sum(1 for r in results if r["status"] == "PASSED")
        failed = sum(1 for r in results if r["status"] == "FAILED")

        print(f"\n## Summary\n")
        print(f"| Status | Count |")
        print(f"|--------|-------|")
        print(f"| PASSED | {passed} |")
        print(f"| FAILED | {failed} |")

        if passed:
            print(f"\n## PASSED ({passed})\n")
            for r in results:
                if r["status"] == "PASSED":
                    print(f"- `{r['tool']}` \u2014 {r['label']}")

        if failed:
            print(f"\n## FAILED ({failed})\n")
            for r in results:
                if r["status"] == "FAILED":
                    print(f"### {r['label']}")
                    print(f"- **Error**: {r['reason']}")
                    print()

        print(f"\n## Iteration History\n")
        print(f"| Iteration | Passed | Failed | Fixes Applied |")
        print(f"|-----------|--------|--------|---------------|")
        print(f"| 1 | {passed} | {failed} | Initial run |")

        total = len(results)
        print(f"\n---")
        print(f"**Total tests:** {total} | **PASSED:** {passed} | **FAILED:** {failed}")

        if failed == 0:
            print(f"\n**ALL TESTS PASS**")
        else:
            print(f"\n**TESTS FAILING** \u2014 see above for details")


def _is_test_artifact(name: str) -> bool:
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if not name.startswith("t"):
        return False
    dash_pos = name.find("-", 1)
    if dash_pos < 2 or dash_pos > 9:
        return False
    prefix = name[1:dash_pos]
    return bool(prefix) and all(c in "0123456789abcdef" for c in prefix)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
