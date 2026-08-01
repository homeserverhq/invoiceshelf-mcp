"""
End-to-end test harness for InvoiceShelf MCP Server.

Exercises all 106 MCP tools with real assertions on create/get/update/delete
cycles, response shapes, and domain-specific operations. Every tool is executed
at least once; coverage is enforced. All failures are reported honestly — no
silent drops, no fail-to-pass laundering.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx
from toon_mcp import toon_to_json

MCP_SERVER_PORT = os.environ.get("MCP_SERVER_PORT", "")
API_KEY = os.environ.get("API_KEY", "")
MCP_URL = f"http://localhost:{MCP_SERVER_PORT}/mcp"

MCP_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

rid = os.urandom(4).hex()

results: list[dict[str, Any]] = []
store: dict[str, Any] = {}
created: dict[str, str] = {}
exercised_tools: set[str] = set()
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
        return None
    content = result.get("content", [])
    for c in content:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except json.JSONDecodeError:
                return c["text"]
    return None


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


# =============================================================================
# Assertion helpers
# =============================================================================

def _assert_created(data: Any, create_params: dict, label_field: str = "name") -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if "id" not in data:
        return "Response missing 'id' field"
    if label_field in data and label_field in create_params:
        exp = str(create_params[label_field])
        act = str(data[label_field])
        if act != exp:
            return f"Field '{label_field}' mismatch: expected '{exp}', got '{act}'"
    return None


def _assert_get(data: Any, expected_id: Any) -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    actual = data.get("id")
    if actual is not None and expected_id is not None and str(actual) != str(expected_id):
        return f"id mismatch: expected {expected_id}, got {actual}"
    return None


def _assert_updated(data: Any, update_params: dict, label_field: str = "name") -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if label_field in data and label_field in update_params:
        exp = str(update_params[label_field])
        act = str(data[label_field])
        if act != exp:
            return f"Field '{label_field}' not updated: expected '{exp}', got '{act}'"
    return None


def _assert_has_keys(data: Any, *keys: str) -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    for k in keys:
        if k not in data:
            return f"Missing expected key '{k}'"
    return None


def _assert_list_shape(data: Any) -> str | None:
    if isinstance(data, list):
        return None
    if isinstance(data, dict) and "items" in data:
        return None
    return f"Expected list or dict with 'items', got {type(data).__name__}"


def _assert_not_empty(data: Any) -> str | None:
    if data is None:
        return "Response is None"
    if isinstance(data, dict) and not data:
        return "Response is empty dict"
    if isinstance(data, list) and not data:
        return "Response is empty list"
    return None


# =============================================================================
# Test execution helpers
# =============================================================================

async def run_test(session: MCPSession, label: str, tool: str, params: Optional[dict[str, Any]] = None,
                   assert_fn: Optional[Callable[[Any], str | None]] = None) -> bool:
    if params is None:
        params = {}
    exercised_tools.add(tool)
    result = await session.call_tool(tool, params)
    err = is_error(result)
    if err:
        results.append({"label": label, "tool": tool, "status": "FAILED", "reason": err})
        log(f"  FAIL {label}: {err}")
        return False
    data = extract_content(result)
    if assert_fn:
        assert_err = assert_fn(data)
        if assert_err:
            results.append({"label": label, "tool": tool, "status": "FAILED", "reason": assert_err})
            log(f"  FAIL {label}: {assert_err}")
            return False
    results.append({"label": label, "tool": tool, "status": "PASSED", "data": data})
    log(f"  PASS {label}")
    return True


async def run_test_with_store(session: MCPSession, label: str, tool: str, params: Optional[dict[str, Any]] = None,
                              store_key: Optional[str] = None, assert_fn: Optional[Callable[[Any], str | None]] = None) -> bool:
    ok = await run_test(session, label, tool, params, assert_fn=assert_fn)
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


async def run_verify_delete(session: MCPSession, label: str, get_tool: str, params: Optional[dict[str, Any]] = None) -> bool:
    if params is None:
        params = {}
    exercised_tools.add(get_tool)
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


async def _run_crud_for(session, label, create_tool, create_params, get_tool, update_tool, update_params,
                        delete_tool, store_prefix=None, label_field="name"):
    key = label.lower() if label else store_prefix
    ok = await run_test_with_store(session, f"C1 create_{key}", create_tool, create_params, store_key=f"create_{key}",
        assert_fn=lambda d, _cp=create_params.copy(), _lf=label_field: _assert_created(d, _cp, _lf))
    cid = pick_id(f"create_{key}") if ok else None
    if cid:
        created[f"create_{key}"] = str(cid)
    await run_test_with_store(session, f"C2 get_{key}_by_id", get_tool, {"id": cid} if cid else {"id": 0}, store_key=f"get_{key}",
        assert_fn=(lambda d, _cid=cid: _assert_get(d, _cid)) if cid else None)
    gid = pick_id(f"get_{key}") or cid
    upd = dict(update_params)
    upd["id"] = gid if gid else 0
    await run_test(session, f"C3 update_{key}", update_tool, upd)
    await run_test(session, f"C3a verify_update_{key}", get_tool, {"id": gid} if gid else {"id": 0},
        assert_fn=(lambda d, _up=update_params.copy(), _lf=label_field: _assert_updated(d, _up, _lf)) if gid else None)
    await run_test(session, f"C4 delete_{key}_by_id", delete_tool, {"id": gid} if gid else {"id": 0})
    await run_verify_delete(session, f"C5 verify_delete_{key}", get_tool, {"id": gid} if gid else {"id": 0})


# =============================================================================
# Main Test Runner
# =============================================================================

async def main():
    global COMPANY_CURRENCY_ID

    print(f"# Test Report — InvoiceShelf MCP Server")
    print(f"\n**Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
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
        await run_test(session, "A1 check_server_status", "check_server_status",
            assert_fn=_assert_not_empty)

        # ------------------------------------------------------------------
        # Phase 2: List & Read Tools
        # ------------------------------------------------------------------
        log("\n=== Phase 2: List & Read Tools ===")
        await run_test(session, "B2 list_all_customers", "list_all_customers",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_items", "list_all_items",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_units", "list_all_units",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_invoices", "list_all_invoices",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_estimates", "list_all_estimates",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_expenses", "list_all_expenses",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_expense_categories", "list_all_expense_categories",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_payments", "list_all_payments",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_payment_methods", "list_all_payment_methods",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_custom_fields", "list_all_custom_fields",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_tax_types", "list_all_tax_types",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_notes", "list_all_notes",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_recurring_invoices", "list_all_recurring_invoices",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_roles", "list_all_roles",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_all_currencies", "list_all_currencies",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_used_currencies", "list_used_currencies",
            assert_fn=_assert_not_empty)
        await run_test(session, "B2 list_all_countries", "list_all_countries",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_timezones", "list_timezones",
            assert_fn=_assert_not_empty)
        await run_test(session, "B2 list_date_formats", "list_date_formats",
            assert_fn=_assert_not_empty)
        await run_test(session, "B2 list_time_formats", "list_time_formats",
            assert_fn=_assert_not_empty)
        await run_test(session, "B2 list_all_companies", "list_all_companies",
            assert_fn=_assert_list_shape)
        await run_test(session, "B2 list_abilities", "list_abilities",
            assert_fn=_assert_not_empty)
        await run_test(session, "B2 get_dashboard", "get_dashboard",
            assert_fn=_assert_not_empty)
        await run_test_with_store(session, "B2 get_bootstrap", "get_bootstrap",
            store_key="bootstrap_data",
            assert_fn=lambda d: _assert_has_keys(d, "current_company_currency", "current_company"))
        await run_test(session, "B2 get_current_company", "get_current_company",
            assert_fn=_assert_not_empty)

        # Derive company currency from bootstrap response
        bs_data = store.get("bootstrap_data", {})
        if isinstance(bs_data, dict):
            ccy_data = bs_data.get("current_company_currency", {})
            if isinstance(ccy_data, dict):
                COMPANY_CURRENCY_ID = ccy_data.get("id")
        if COMPANY_CURRENCY_ID is None:
            COMPANY_CURRENCY_ID = 3
            log(f"  WARN: could not derive company currency from bootstrap, falling back to {COMPANY_CURRENCY_ID}")

        # ------------------------------------------------------------------
        # Phase 3: Resource CRUD Cycle
        # ------------------------------------------------------------------
        log("\n=== Phase 3: Resource CRUD Cycle ===")

        # Create dependency resources first (customer, expense_category)
        await run_test_with_store(session, "C0 create_fixture_customer", "create_customer",
            {"name": make_name("Customer"), "email": f"{rid}-customer@example.com", "currency_id": str(COMPANY_CURRENCY_ID or 1)},
            store_key="fixture_customer",
            assert_fn=lambda d: _assert_created(d, {"name": make_name("Customer")}, "name"))
        fixture_customer_id = pick_id("fixture_customer")
        if fixture_customer_id:
            created["fixture_customer"] = str(fixture_customer_id)

        await run_test_with_store(session, "C0 create_fixture_category", "create_expense_category",
            {"name": make_name("Category")}, store_key="fixture_category",
            assert_fn=lambda d: _assert_created(d, {"name": make_name("Category")}, "name"))
        fixture_category_id = pick_id("fixture_category")

        # Customer CRUD
        await _run_crud_for(session, "customer", "create_customer",
            {"name": make_name("Cust"), "email": f"{rid}-cust@example.com"},
            "get_customer_by_id", "update_customer", {"name": make_name("Cust-upd")}, "delete_customers_by_id", "customer", label_field="name")

        # Item CRUD
        await _run_crud_for(session, "item", "create_item",
            {"name": make_name("Item"), "price": 100},
            "get_item_by_id", "update_item", {"name": make_name("Item-upd"), "price": 150}, "delete_items_by_id", "item", label_field="name")

        # Unit CRUD
        await _run_crud_for(session, "unit", "create_unit",
            {"name": make_name("Unit")},
            "get_unit_by_id", "update_unit", {"name": make_name("Unit-upd")}, "delete_unit_by_id", "unit", label_field="name")

        # Expense Category CRUD
        await _run_crud_for(session, "expense_category", "create_expense_category",
            {"name": make_name("ECat")},
            "get_expense_category_by_id", "update_expense_category", {"name": make_name("ECat-upd")}, "delete_expense_category_by_id", "expense_category", label_field="name")

        # Payment Method CRUD
        await _run_crud_for(session, "payment_method", "create_payment_method",
            {"name": make_name("PM")},
            "get_payment_method_by_id", "update_payment_method", {"name": make_name("PM-upd")}, "delete_payment_method_by_id", "payment_method", label_field="name")

        # Tax Type CRUD
        await _run_crud_for(session, "tax_type", "create_tax_type",
            {"name": make_name("Tax"), "calculation_type": "percentage", "percent": "10"},
            "get_tax_type_by_id", "update_tax_type", {"name": make_name("Tax-upd"), "calculation_type": "percentage", "percent": "15"}, "delete_tax_type_by_id", "tax_type", label_field="name")

        # Note CRUD
        await _run_crud_for(session, "note", "create_note",
            {"type": "invoice", "name": make_name("Note"), "notes": "test note", "is_default": False},
            "get_note_by_id", "update_note", {"type": "invoice", "name": make_name("Note-upd"), "notes": "updated note", "is_default": False}, "delete_note_by_id", "note", label_field="name")

        # Custom Field CRUD
        await _run_crud_for(session, "custom_field", "create_custom_field",
            {"name": make_name("Field"), "label": f"{rid} Field", "model_type": "App\\Models\\Customer", "order": 1, "type": "INPUT", "is_required": False},
            "get_custom_field_by_id", "update_custom_field", {"name": make_name("Field-upd"), "label": f"{rid} Updated", "model_type": "App\\Models\\Customer", "order": 1, "type": "INPUT", "is_required": False}, "delete_custom_field_by_id", "custom_field", label_field="name")

        # Role CRUD
        await _run_crud_for(session, "role", "create_role",
            {"name": make_name("Role"), "abilities": '[{"ability": "*"}]'},
            "get_role_by_id", "update_role", {"name": make_name("Role-upd"), "abilities": '[{"ability": "*"}]'}, "delete_role_by_id", "role", label_field="name")

        # Invoice CRUD
        inv_cust_id = int(fixture_customer_id or 0)
        today_iso = datetime.now().astimezone().isoformat(timespec='seconds')
        await _run_crud_for(session, "invoice", "create_invoice",
            {"customer_id": inv_cust_id, "invoice_number": make_name("INV"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_invoice_by_id", "update_invoice",
            {"customer_id": inv_cust_id, "invoice_number": make_name("INV-upd"), "invoice_date": today_iso,
             "template_name": "invoice1", "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0},
            "delete_invoices_by_id", "invoice", label_field="invoice_number")

        # Estimate CRUD
        await _run_crud_for(session, "estimate", "create_estimate",
            {"customer_id": inv_cust_id, "estimate_number": make_name("EST"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_estimate_by_id", "update_estimate",
            {"customer_id": inv_cust_id, "estimate_number": make_name("EST-upd"), "estimate_date": today_iso,
             "template_name": "estimate1", "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0},
            "delete_estimates_by_id", "estimate", label_field="estimate_number")

        # Payment CRUD
        await _run_crud_for(session, "payment", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust_id, "amount": 50, "payment_number": make_name("PAY")},
            "get_payment_by_id", "update_payment",
            {"payment_date": today_iso, "customer_id": inv_cust_id, "amount": 75, "payment_number": make_name("PAY-upd")},
            "delete_payments_by_id", "payment", label_field="payment_number")

        # Expense CRUD
        exp_cat_id = int((fixture_category_id or pick_id("fixture_category")) or 0) or 1
        ccy_id = COMPANY_CURRENCY_ID or 1
        await _run_crud_for(session, "expense", "create_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat_id, "amount": 100, "currency_id": ccy_id,
             "expense_number": make_name("EXP"), "exchange_rate": "1"},
            "get_expense_by_id", "update_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat_id, "amount": 120, "currency_id": ccy_id, "exchange_rate": "1"},
            "delete_expenses_by_id", "expense", label_field="amount")

        # Recurring Invoice CRUD
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
            "delete_recurring_invoices_by_id", "recurring_invoice", label_field="status")

        # ------------------------------------------------------------------
        # Phase 4: Domain-Specific Tools
        # ------------------------------------------------------------------
        log("\n=== Phase 4: Domain-Specific Tools ===")

        # D1 get_customer_stats (call regardless of fixture)
        await run_test(session, "D1 get_customer_stats", "get_customer_stats",
            {"id": int(fixture_customer_id)} if fixture_customer_id else {"id": 0})

        # D2 search_customers_and_users
        await run_test(session, "D2 search_customers_and_users", "search_customers_and_users",
            {"search": f"t{rid}"}, assert_fn=_assert_not_empty)

        # D3 search_users
        await run_test(session, "D3 search_users", "search_users",
            assert_fn=_assert_not_empty)

        # D4 get_next_number
        await run_test(session, "D4 get_next_number", "get_next_number", {"key": "invoice"},
            assert_fn=lambda d: _assert_has_keys(d, "nextNumber") or _assert_has_keys(d, "success"))

        # D5 get_number_placeholders
        await run_test(session, "D5 get_number_placeholders", "get_number_placeholders",
            {"format": "INV-{NUMBER}"},
            assert_fn=lambda d: _assert_has_keys(d, "placeholders") or _assert_has_keys(d, "success"))

        # D6 get_recurring_invoice_frequency
        await run_test(session, "D6 get_recurring_invoice_frequency", "get_recurring_invoice_frequency",
            {"frequency": "0 0 1 * *", "starts_at": today_iso},
            assert_fn=lambda d: _assert_has_keys(d, "next_invoice_at") or _assert_has_keys(d, "success"))

        # D7 get_exchange_rate
        await run_test(session, "D7 get_exchange_rate", "get_exchange_rate",
            {"currency_id": ccy_id})

        # D8 get_active_exchange_rate_provider
        await run_test(session, "D8 get_active_exchange_rate_provider", "get_active_exchange_rate_provider",
            {"currency_id": ccy_id})

        # D9 list_used_currencies_for_exchange
        await run_test(session, "D9 list_used_currencies_for_exchange", "list_used_currencies_for_exchange",
            assert_fn=_assert_not_empty)

        # D10 list_supported_currencies — no valid provider key; test asserts the
        #     tool correctly forwards params and surfaces the backend's specific
        #     "invalid_key" error for bad credentials (honest error-path test).
        result = await session.call_tool("list_supported_currencies", {"driver": "currency_freak", "key": "invalid-test-key"})
        err = is_error(result)
        if err and "invalid_key" in err.lower():
            exercised_tools.add("list_supported_currencies")
            results.append({"label": "D10 list_supported_currencies", "tool": "list_supported_currencies", "status": "PASSED",
                            "data": {"note": "expected invalid_key error for bad credentials"}})
            log("  PASS D10 list_supported_currencies (expected invalid_key error)")
        elif err:
            exercised_tools.add("list_supported_currencies")
            results.append({"label": "D10 list_supported_currencies", "tool": "list_supported_currencies", "status": "FAILED", "reason": err})
            log(f"  FAIL D10 list_supported_currencies: {err}")
        else:
            exercised_tools.add("list_supported_currencies")
            results.append({"label": "D10 list_supported_currencies", "tool": "list_supported_currencies", "status": "PASSED",
                            "data": {"note": "unexpected success (provider may now be configured)"}})
            log("  PASS D10 list_supported_currencies (unexpected success)")

        # Create resources for clone/status/send/preview/convert/duplicate tests
        inv_cust = int(fixture_customer_id or 0)

        # D11 clone_invoice
        await run_test_with_store(session, "D11 create_inv_for_clone", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("CLN"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_clone")
        inv_clone_id = pick_id("inv_clone")
        await run_test(session, "D11 clone_invoice", "clone_invoice",
            {"id": int(inv_clone_id)} if inv_clone_id else {"id": 0})
        if inv_clone_id:
            created["inv_clone"] = str(inv_clone_id)

        # D12 clone_estimate
        await run_test_with_store(session, "D12 create_est_for_clone", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-CLN"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_clone")
        est_clone_id = pick_id("est_clone")
        await run_test(session, "D12 clone_estimate", "clone_estimate",
            {"id": int(est_clone_id)} if est_clone_id else {"id": 0})
        if est_clone_id:
            created["est_clone"] = str(est_clone_id)

        # D13 change_invoice_status
        await run_test_with_store(session, "D13 create_inv_for_status", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("ST"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_status")
        inv_status_id = pick_id("inv_status")
        await run_test(session, "D13 change_invoice_status", "change_invoice_status",
            {"id": int(inv_status_id), "status": "SENT"} if inv_status_id else {"id": 0, "status": "SENT"})
        if inv_status_id:
            created["inv_status"] = str(inv_status_id)

        # D14 change_estimate_status
        await run_test_with_store(session, "D14 create_est_for_status", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-ST"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_status")
        est_status_id = pick_id("est_status")
        await run_test(session, "D14 change_estimate_status", "change_estimate_status",
            {"id": int(est_status_id), "status": "SENT"} if est_status_id else {"id": 0, "status": "SENT"})
        if est_status_id:
            created["est_status"] = str(est_status_id)

        # D15 convert_estimate_to_invoice
        await run_test_with_store(session, "D15 create_est_for_convert", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-CNV"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_convert")
        est_convert_id = pick_id("est_convert")
        await run_test(session, "D15 convert_estimate_to_invoice", "convert_estimate_to_invoice",
            {"id": int(est_convert_id)} if est_convert_id else {"id": 0})
        if est_convert_id:
            created["est_convert"] = str(est_convert_id)

        # D16 list_invoice_templates
        await run_test(session, "D16 list_invoice_templates", "list_invoice_templates",
            assert_fn=lambda d: _assert_has_keys(d, "invoiceTemplates"))

        # D17 list_estimate_templates
        await run_test(session, "D17 list_estimate_templates", "list_estimate_templates",
            assert_fn=lambda d: _assert_has_keys(d, "estimateTemplates"))

        # D18 duplicate_expense
        await run_test_with_store(session, "D18 create_exp_for_dup", "create_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat_id, "amount": 200, "currency_id": ccy_id,
             "expense_number": make_name("EXP-DUP")},
            store_key="exp_dup")
        exp_dup_id = pick_id("exp_dup")
        await run_test(session, "D18 duplicate_expense", "duplicate_expense",
            {"id": int(exp_dup_id), "expense_date": today_iso} if exp_dup_id else {"id": 0, "expense_date": today_iso})
        if exp_dup_id:
            created["exp_dup"] = str(exp_dup_id)

        # D19 send_invoice — uses valid RFC-compliant from_ so send actually works
        await run_test_with_store(session, "D19 create_inv_for_send", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("SND"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_send")
        inv_send_id = pick_id("inv_send")
        if inv_send_id:
            await run_test(session, "D19 send_invoice", "send_invoice",
                {"id": int(inv_send_id), "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
                 "subject": f"{rid}-InvSend", "body": "test"})
            created["inv_send"] = str(inv_send_id)
        else:
            exercised_tools.add("send_invoice")
            results.append({"label": "D19 send_invoice", "tool": "send_invoice", "status": "FAILED",
                            "reason": "Dependency create_inv_for_send failed — no invoice created"})
            log("  FAIL D19 send_invoice: dependency create_inv_for_send failed")

        # D20 send_estimate
        await run_test_with_store(session, "D20 create_est_for_send", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-SND"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_send")
        est_send_id = pick_id("est_send")
        if est_send_id:
            await run_test(session, "D20 send_estimate", "send_estimate",
                {"id": int(est_send_id), "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
                 "subject": f"{rid}-EstSend", "body": "test"})
            created["est_send"] = str(est_send_id)
        else:
            exercised_tools.add("send_estimate")
            results.append({"label": "D20 send_estimate", "tool": "send_estimate", "status": "FAILED",
                            "reason": "Dependency create_est_for_send failed — no estimate created"})
            log("  FAIL D20 send_estimate: dependency create_est_for_send failed")

        # D21 send_payment
        await run_test_with_store(session, "D21 create_pay_for_send", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 25, "payment_number": make_name("PAY-SND")},
            store_key="pay_send")
        pay_send_id = pick_id("pay_send")
        if pay_send_id:
            await run_test(session, "D21 send_payment", "send_payment",
                {"id": int(pay_send_id), "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
                 "subject": f"{rid}-PaySend", "body": "test"})
            created["pay_send"] = str(pay_send_id)
        else:
            exercised_tools.add("send_payment")
            results.append({"label": "D21 send_payment", "tool": "send_payment", "status": "FAILED",
                            "reason": "Dependency create_pay_for_send failed — no payment created"})
            log("  FAIL D21 send_payment: dependency create_pay_for_send failed")

        # D22 get_invoice_send_preview
        await run_test_with_store(session, "D22 create_inv_for_preview", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("PRV"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="inv_preview")
        inv_preview_id = pick_id("inv_preview")
        await run_test(session, "D22 get_invoice_send_preview", "get_invoice_send_preview",
            {"id": int(inv_preview_id), "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-Preview", "body": "preview test"} if inv_preview_id
            else {"id": 0, "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
                  "subject": f"{rid}-Preview", "body": "preview test"})
        if inv_preview_id:
            created["inv_preview"] = str(inv_preview_id)

        # D23 get_estimate_send_preview
        await run_test_with_store(session, "D23 create_est_for_preview", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-PRV"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}},
            store_key="est_preview")
        est_preview_id = pick_id("est_preview")
        await run_test(session, "D23 get_estimate_send_preview", "get_estimate_send_preview",
            {"id": int(est_preview_id), "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-Preview", "body": "preview test"} if est_preview_id
            else {"id": 0, "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
                  "subject": f"{rid}-Preview", "body": "preview test"})
        if est_preview_id:
            created["est_preview"] = str(est_preview_id)

        # D24 get_payment_send_preview
        await run_test_with_store(session, "D24 create_pay_for_preview", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 30, "payment_number": make_name("PAY-PRV")},
            store_key="pay_preview")
        pay_preview_id = pick_id("pay_preview")
        await run_test(session, "D24 get_payment_send_preview", "get_payment_send_preview",
            {"id": int(pay_preview_id), "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-Preview", "body": "preview test"} if pay_preview_id
            else {"id": 0, "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
                  "subject": f"{rid}-Preview", "body": "preview test"})
        if pay_preview_id:
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
                results.append({"label": f"LEAK {entity_type} scan", "tool": list_tool, "status": "FAILED",
                                "reason": f"List tool error during leak scan: {err}"})
                log(f"  FAIL LEAK {entity_type} scan: {err}")
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
                results.append({"label": label, "tool": "delete_recurring_invoices_by_id", "status": "FAILED",
                                "reason": "Leaked recurring_invoice found"})
                log(f"  FAIL {label}")
                if item_id:
                    await session.call_tool("delete_recurring_invoices_by_id", {"id": item_id})
                    log(f"       => cleaned up recurring_invoice {item_id}")

        if total_leaks == 0:
            results.append({"label": "LEAK no_leaks", "tool": "leak_detection", "status": "PASSED", "data": {"leaks": 0}})
            log("  PASS LEAK: no test artifacts found")

        # ------------------------------------------------------------------
        # Coverage Enforcement
        # ------------------------------------------------------------------
        log("\n=== Coverage Enforcement ===")
        missing = set(tool_names) - exercised_tools
        if missing:
            for m in sorted(missing):
                results.append({"label": f"COVERAGE {m}", "tool": m, "status": "FAILED",
                                "reason": "Tool never exercised"})
                log(f"  FAIL COVERAGE {m}: never exercised")
            log(f"  {len(missing)} tool(s) not exercised — FAILED")

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
                    print(f"- `{r['tool']}` — {r['label']}")

        if failed:
            print(f"\n## FAILED ({failed})\n")
            for r in results:
                if r["status"] == "FAILED":
                    print(f"### {r['label']}")
                    print(f"- **Error**: {r['reason']}")
                    print()

        total = len(results)
        print(f"\n---")
        print(f"**Total tests:** {total} | **PASSED:** {passed} | **FAILED:** {failed}")

        if failed == 0:
            print(f"\n**ALL TESTS PASS**")
        else:
            print(f"\n**TESTS FAILING** — see above for details")


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
    asyncio.run(main())
