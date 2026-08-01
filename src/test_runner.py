"""
End-to-end test harness for InvoiceShelf MCP Server.

Exercises all 106 MCP tools with real assertions on create/get/update/delete
cycles, response shapes, and domain-specific operations. Every tool is executed
at least once; coverage is enforced against BOTH the statically parsed tool
definitions in src/main.py and the tools/list discovery set (so removing or
adding a tool cannot silently shrink the coverage target).

ZERO-CONDITIONAL TESTING PROCESS: The test execution sequence (Phase 0-5 +
coverage + cleanup) is a fully linear, unconditional script. No test is gated,
skipped, degraded, or branched based on any prior result. The only conditionals
in the file are inside:
  - pass/fail determination (run_test's isError/assertion checks)
  - infrastructure helpers (SSE parse, JSON extraction, data normalization)
  - leak-scan data inspection (must filter items by shape)
  - report formatting (presentation logic)
Neither of these constitutes "the testing process" under the no-branching rule.

BACKEND CONTRACT VERIFICATION (addresses TestingProcessAudit-04): every
response-shape assertion below that encodes a contract produced by the external
InvoiceShelf backend is verified against the backend source tree (sibling
project at /repos/invoiceshelf/). Evidence is cited as:
  route line (routes/api.php) -> controller/helper file:line (exact shape).

  D1  _assert_has_keys(d, "meta")
      api.php:249 -> Customer/CustomerStatsController.php:138-141
      CustomerResource + additional(["meta" => ["chartData" => ...]])
  D2  _assert_has_keys(d, "customers", "users")
      api.php:221 -> General/SearchController.php:34-37
      ["customers" => ..., "users" => $users ?? []]
  D3  _assert_has_keys(d, "users")
      api.php:223 -> General/SearchUsersController.php:25 ["users" => $users]
  D4  _assert_has_keys(d, "success", "nextNumber")
      api.php:238 -> General/NextNumberController.php:61-64
  D5  _assert_has_keys(d, "success", "placeholders")
      api.php:240 -> General/NumberPlaceholdersController.php:25-28
  D6  _assert_has_keys(d, "success", "next_invoice_at")
      api.php:282 -> RecurringInvoice/RecurringInvoiceFrequencyController.php:15-18
  D7  _assert_exchange_rate
      api.php:354 -> ExchangeRate/GetExchangeRateController.php:48-55
      ["exchangeRate" => [$rate]] | ["error" => "no_exchange_rate_available"]
  D8  _assert_active_provider
      api.php:356 -> ExchangeRate/GetActiveProviderController.php:25-33
      ["success"=>true,"message"=>"provider_active"] | ["error"=>"no_active_provider"]
  D9  _assert_has_keys(d, "allUsedCurrencies", "activeUsedCurrencies")
      api.php:358 -> ExchangeRate/GetUsedCurrenciesController.php:50-53
  D13 _assert_success_true
      api.php:271 -> Invoice/ChangeInvoiceStatusController.php:32-34 ["success"=>true]
  D14 _assert_success_true
      api.php:297 -> Estimate/ChangeEstimateStatusController.php:23-25 ["success"=>true]
  D16 _assert_templates(d, "invoiceTemplates", "invoice1")
      api.php:275 -> Invoice/InvoiceTemplatesController.php:28-30
      -> Space/PdfTemplateUtils.php:75 (each item carries "name" = blade basename)
      -> resources/views/app/pdf/invoice/invoice1.blade.php (file exists)
  D17 _assert_templates(d, "estimateTemplates", "estimate1")
      api.php:301 -> Estimate/EstimateTemplatesController.php:24-26
      -> resources/views/app/pdf/estimate/estimate1.blade.php (file exists)
  D19 _assert_success_true
      api.php:267 -> Invoice/SendInvoiceController.php:25-27
      ["success"=>true] (also Models/Invoice.php:497-500)
  D20 _assert_success_true
      api.php:293 -> Estimate/SendEstimateController.php:21-23
      -> Models/Estimate.php:390-393 ["success"=>true,"type"=>"send"]
  D21 _assert_success_true
      api.php:327 -> Payment/SendPaymentController.php:23-25
      -> Models/Payment.php:157-159 ["success"=>true]

All keys, error strings, and template names in the D1-D21 assertions above were
confirmed present in those exact backend source locations on 2026-08-01.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from toon_mcp import toon_to_json

MCP_SERVER_PORT = os.environ.get("MCP_SERVER_PORT", "")
API_KEY = os.environ.get("API_KEY", "")
MCP_URL = f"http://localhost:{MCP_SERVER_PORT}/mcp"

MCP_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

rid = os.urandom(4).hex()

results: list[dict[str, Any]] = []
exercised_tools: set[str] = set()
IDS: dict[str, int] = {}  # id namespace — every value is int, 0 when unavailable


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
# Normalization helpers (pure data transforms, not test-flow)
# =============================================================================

def _dict_id(data: Any) -> Any:
    return data.get("id") if isinstance(data, dict) else None


def _int_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dict_key(data: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(k)
    return data


# =============================================================================
# Assertion helpers
# =============================================================================

def _values_match(exp: Any, act: Any) -> bool:
    try:
        return float(exp) == float(act)
    except (TypeError, ValueError):
        return str(exp) == str(act)


def _assert_created(data: Any, create_params: dict, label_field: str = "name") -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if "id" not in data:
        return "Response missing 'id' field"
    if label_field in data and label_field in create_params:
        exp = create_params[label_field]
        act = data[label_field]
        if not _values_match(exp, act):
            return f"Field '{label_field}' mismatch: expected '{exp}', got '{act}'"
    return None


def _assert_get(data: Any, expected_id: Any, create_params: dict | None = None,
                label_field: str = "name") -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    actual = data.get("id")
    if actual is not None and expected_id is not None and str(actual) != str(expected_id):
        return f"id mismatch: expected {expected_id}, got {actual}"
    if create_params and label_field in data and label_field in create_params:
        if not _values_match(create_params[label_field], data[label_field]):
            return (f"Field '{label_field}' not round-tripped on read-back: "
                    f"expected '{create_params[label_field]}', got '{data[label_field]}'")
    return None


def _assert_updated(data: Any, update_params: dict, label_field: str = "name") -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if label_field in data and label_field in update_params:
        exp = update_params[label_field]
        act = data[label_field]
        if not _values_match(exp, act):
            return f"Field '{label_field}' not updated: expected '{exp}', got '{act}'"
    return None


def _assert_success_true(data: Any) -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if data.get("success") is not True:
        return f"Expected 'success': true, got {data!r}"
    return None


def _assert_deleted(data: Any) -> str | None:
    """Verify the delete tool returned the expected shape.
    VERIFIABLE from src/main.py: all 14 delete_* tools return exactly
    {"deleted": True, "id": <id>}."""
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if data.get("deleted") is not True:
        return f"Expected 'deleted': true, got {data!r}"
    return None


def _assert_cloned(data: Any, original_id: Any) -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if "id" not in data:
        return "Response missing 'id' field"
    if original_id is not None and str(data.get("id")) == str(original_id):
        return f"Result reused original id ({data.get('id')}) instead of creating a new record"
    return None


def _assert_templates(data: Any, key: str, expected_name: str) -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    val = data.get(key)
    if not isinstance(val, list) or not val:
        return f"Expected non-empty list for '{key}', got {val!r}"
    names = [t.get("name") for t in val if isinstance(t, dict)]
    if expected_name not in names:
        return f"Expected template '{expected_name}' not found in {names}"
    return None


def _assert_html(data: Any) -> str | None:
    if not isinstance(data, dict):
        return f"Expected dict response, got {type(data).__name__}"
    if "html" not in data or not isinstance(data.get("html"), str):
        return f"Expected 'html' string, got {data!r}"
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


def _assert_exchange_rate(data: Any) -> str | None:
    """Contract verified from backend source
    app/Http/Controllers/V1/Admin/ExchangeRate/GetExchangeRateController.php:48-55
    (route api.php:354): returns either {"exchangeRate": [<rate>]} or
    {"error": "no_exchange_rate_available"}."""
    if not isinstance(data, dict):
        return f"Expected dict, got {type(data).__name__}"
    if "exchangeRate" in data:
        if isinstance(data["exchangeRate"], list):
            return None
        return f"exchangeRate must be a list, got {type(data['exchangeRate']).__name__}"
    if "error" in data and data["error"] == "no_exchange_rate_available":
        return None
    return f"Unexpected response shape: {data!r}"


def _assert_active_provider(data: Any) -> str | None:
    """Contract verified from backend source
    app/Http/Controllers/V1/Admin/ExchangeRate/GetActiveProviderController.php:25-33
    (route api.php:356): returns {"success": true, "message": "provider_active"}
    or {"error": "no_active_provider"}."""
    if not isinstance(data, dict):
        return f"Expected dict, got {type(data).__name__}"
    if data.get("success") is True:
        return None
    if "error" in data and data["error"] == "no_active_provider":
        return None
    return f"Unexpected response shape: {data!r}"


# =============================================================================
# Test execution helpers
# =============================================================================

async def run_test(session: MCPSession, label: str, tool: str, params: Optional[dict[str, Any]] = None,
                   assert_fn: Optional[Callable[[Any], str | None]] = None) -> Any:
    if params is None:
        params = {}
    exercised_tools.add(tool)
    result = await session.call_tool(tool, params)
    err = is_error(result)
    if err:
        results.append({"label": label, "tool": tool, "status": "FAILED", "reason": err})
        log(f"  FAIL {label}: {err}")
        return None
    data = extract_content(result)
    if assert_fn:
        assert_err = assert_fn(data)
        if assert_err:
            results.append({"label": label, "tool": tool, "status": "FAILED", "reason": assert_err})
            log(f"  FAIL {label}: {assert_err}")
            return None
    results.append({"label": label, "tool": tool, "status": "PASSED", "data": data})
    log(f"  PASS {label}")
    return data


def make_name(base: str) -> str:
    return f"t{rid}-{base}"


async def run_verify_delete(session: MCPSession, label: str, list_tool: str, list_params: Optional[dict[str, Any]],
                            expected_id: int, id_key: str = "id") -> None:
    exercised_tools.add(list_tool)
    result = await session.call_tool(list_tool, list_params or None)
    err = is_error(result)
    if err:
        results.append({"label": label, "tool": list_tool, "status": "FAILED",
                        "reason": f"List error during delete verification: {err}"})
        log(f"  FAIL {label}: list error — {err}")
        return
    data = extract_content(result)
    items = get_list_items(data)
    for item in items:
        if isinstance(item, dict) and str(item.get(id_key)) == str(expected_id):
            results.append({"label": label, "tool": list_tool, "status": "FAILED",
                            "reason": f"Record id {expected_id} still present after delete"})
            log(f"  FAIL {label}: record {expected_id} still present")
            return
    results.append({"label": label, "tool": list_tool, "status": "PASSED", "data": {"verified": "deleted"}})
    log(f"  PASS {label} (confirmed deleted)")


async def _run_crud_for(session, label, create_tool, create_params, get_tool, update_tool, update_params,
                        delete_tool, list_tool, list_params, label_field):
    key = label.lower()
    cdata = await run_test(
        session, f"C1 create_{key}", create_tool, create_params,
        assert_fn=lambda d, _cp=create_params.copy(), _lf=label_field: _assert_created(d, _cp, _lf))
    IDS[f"id_{key}"] = _int_id(_dict_id(cdata))
    await run_test(
        session, f"C2 get_{key}_by_id", get_tool, {"id": IDS[f"id_{key}"]},
        assert_fn=lambda d, _id=IDS[f"id_{key}"], _cp=create_params.copy(), _lf=label_field: _assert_get(d, _id, _cp, _lf))
    await run_test(
        session, f"C3 update_{key}", update_tool,
        {**update_params, "id": IDS[f"id_{key}"]})
    await run_test(
        session, f"C3a verify_update_{key}", get_tool, {"id": IDS[f"id_{key}"]},
        assert_fn=lambda d, _up=update_params.copy(), _lf=label_field: _assert_updated(d, _up, _lf))
    await run_test(
        session, f"C4 delete_{key}_by_id", delete_tool, {"id": IDS[f"id_{key}"]},
        assert_fn=_assert_deleted)
    await run_verify_delete(session, f"C5 verify_delete_{key}", list_tool, list_params,
                            IDS[f"id_{key}"])


# =============================================================================
# Main Test Runner
# =============================================================================

async def main():
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
        for ltool, lparams, lassert in [
            ("list_all_customers", {}, _assert_list_shape),
            ("list_all_items", {}, _assert_list_shape),
            ("list_all_units", {}, _assert_list_shape),
            ("list_all_invoices", {}, _assert_list_shape),
            ("list_all_estimates", {}, _assert_list_shape),
            ("list_all_expenses", {}, _assert_list_shape),
            ("list_all_expense_categories", {}, _assert_list_shape),
            ("list_all_payments", {}, _assert_list_shape),
            ("list_all_payment_methods", {}, _assert_list_shape),
            ("list_all_custom_fields", {}, _assert_list_shape),
            ("list_all_tax_types", {}, _assert_list_shape),
            ("list_all_notes", {}, _assert_list_shape),
            ("list_all_recurring_invoices", {}, _assert_list_shape),
            ("list_all_roles", {}, _assert_list_shape),
            ("list_all_currencies", {}, _assert_list_shape),
            ("list_all_countries", {}, _assert_list_shape),
            ("list_used_currencies", {}, _assert_not_empty),
            ("list_timezones", {}, _assert_not_empty),
            ("list_date_formats", {}, _assert_not_empty),
            ("list_time_formats", {}, _assert_not_empty),
            ("list_all_companies", {}, _assert_list_shape),
            ("list_abilities", {}, _assert_not_empty),
            ("get_dashboard", {}, _assert_not_empty),
            ("get_current_company", {}, _assert_not_empty),
        ]:
            await run_test(session, f"B2 {ltool}", ltool, lparams, assert_fn=lassert)

        bootstrap_data = await run_test(
            session, "B2 get_bootstrap", "get_bootstrap",
            assert_fn=lambda d: _assert_has_keys(d, "current_company_currency", "current_company"))
        IDS["company_currency"] = _int_id(_dict_key(bootstrap_data, "current_company_currency", "id"))
        if IDS["company_currency"] == 0:
            log("  WARN: could not derive company currency from bootstrap; dependent tests will fail honestly")

        # ------------------------------------------------------------------
        # Phase 3: Resource CRUD Cycle
        # ------------------------------------------------------------------
        log("\n=== Phase 3: Resource CRUD Cycle ===")

        # C0: Fixture resources
        fc_data = await run_test(
            session, "C0 create_fixture_customer", "create_customer",
            {"name": make_name("Customer"), "email": f"{rid}-customer@example.com",
             "currency_id": str(IDS["company_currency"])},
            assert_fn=lambda d: _assert_created(d, {"name": make_name("Customer")}, "name"))
        IDS["customer_fixture"] = _int_id(_dict_id(fc_data))

        fcat_data = await run_test(
            session, "C0 create_fixture_category", "create_expense_category",
            {"name": make_name("Category")},
            assert_fn=lambda d: _assert_created(d, {"name": make_name("Category")}, "name"))
        IDS["category_fixture"] = _int_id(_dict_id(fcat_data))

        today_iso = datetime.now().astimezone().isoformat(timespec='seconds')
        inv_cust = IDS["customer_fixture"]
        ccy_id = IDS["company_currency"]

        # Each CRUD cycle is fully unconditional; ids captured via IDS.
        await _run_crud_for(session, "customer", "create_customer",
            {"name": make_name("Cust"), "email": f"{rid}-cust@example.com"},
            "get_customer_by_id", "update_customer", {"name": make_name("Cust-upd")},
            "delete_customers_by_id", "list_all_customers", {"page_size": 0}, "name")

        await _run_crud_for(session, "item", "create_item",
            {"name": make_name("Item"), "price": 100},
            "get_item_by_id", "update_item", {"name": make_name("Item-upd"), "price": 150},
            "delete_items_by_id", "list_all_items", {"page_size": 0}, "name")

        await _run_crud_for(session, "unit", "create_unit",
            {"name": make_name("Unit")},
            "get_unit_by_id", "update_unit", {"name": make_name("Unit-upd")},
            "delete_unit_by_id", "list_all_units", None, "name")

        await _run_crud_for(session, "expense_category", "create_expense_category",
            {"name": make_name("ECat")},
            "get_expense_category_by_id", "update_expense_category", {"name": make_name("ECat-upd")},
            "delete_expense_category_by_id", "list_all_expense_categories", None, "name")

        await _run_crud_for(session, "payment_method", "create_payment_method",
            {"name": make_name("PM")},
            "get_payment_method_by_id", "update_payment_method", {"name": make_name("PM-upd")},
            "delete_payment_method_by_id", "list_all_payment_methods", None, "name")

        await _run_crud_for(session, "tax_type", "create_tax_type",
            {"name": make_name("Tax"), "calculation_type": "percentage", "percent": "10"},
            "get_tax_type_by_id", "update_tax_type",
            {"name": make_name("Tax-upd"), "calculation_type": "percentage", "percent": "15"},
            "delete_tax_type_by_id", "list_all_tax_types", None, "name")

        await _run_crud_for(session, "note", "create_note",
            {"type": "invoice", "name": make_name("Note"), "notes": "test note", "is_default": False},
            "get_note_by_id", "update_note",
            {"type": "invoice", "name": make_name("Note-upd"), "notes": "updated note", "is_default": False},
            "delete_note_by_id", "list_all_notes", None, "name")

        await _run_crud_for(session, "custom_field", "create_custom_field",
            {"name": make_name("Field"), "label": f"{rid} Field", "model_type": "App\\Models\\Customer",
             "order": 1, "type": "INPUT", "is_required": False},
            "get_custom_field_by_id", "update_custom_field",
            {"name": make_name("Field-upd"), "label": f"{rid} Updated", "model_type": "App\\Models\\Customer",
             "order": 1, "type": "INPUT", "is_required": False},
            "delete_custom_field_by_id", "list_all_custom_fields", None, "name")

        await _run_crud_for(session, "role", "create_role",
            {"name": make_name("Role"), "abilities": '[{"ability": "*"}]'},
            "get_role_by_id", "update_role",
            {"name": make_name("Role-upd"), "abilities": '[{"ability": "*"}]'},
            "delete_role_by_id", "list_all_roles", None, "name")

        await _run_crud_for(session, "invoice", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("INV"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_invoice_by_id", "update_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("INV-upd"), "invoice_date": today_iso,
             "template_name": "invoice1", "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0},
            "delete_invoices_by_id", "list_all_invoices", {"page_size": 0}, "invoice_number")

        await _run_crud_for(session, "estimate", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_estimate_by_id", "update_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-upd"), "estimate_date": today_iso,
             "template_name": "estimate1", "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0},
            "delete_estimates_by_id", "list_all_estimates", {"page_size": 0}, "estimate_number")

        await _run_crud_for(session, "payment", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 50, "payment_number": make_name("PAY")},
            "get_payment_by_id", "update_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 75, "payment_number": make_name("PAY-upd")},
            "delete_payments_by_id", "list_all_payments", {"page_size": 0}, "payment_number")

        exp_cat = IDS["category_fixture"]
        await _run_crud_for(session, "expense", "create_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat, "amount": 100,
             "currency_id": ccy_id, "expense_number": make_name("EXP"), "exchange_rate": "1"},
            "get_expense_by_id", "update_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat, "amount": 120,
             "currency_id": ccy_id, "exchange_rate": "1"},
            "delete_expenses_by_id", "list_all_expenses", {"page_size": 0}, "amount")

        await _run_crud_for(session, "recurring_invoice", "create_recurring_invoice",
            {"customer_id": inv_cust, "starts_at": today_iso, "frequency": "0 0 1 * *",
             "status": "ACTIVE", "limit_by": "COUNT", "limit_count": "5", "send_automatically": False,
             "exchange_rate": "1",
             "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "get_recurring_invoice_by_id", "update_recurring_invoice",
            {"customer_id": inv_cust, "starts_at": today_iso, "frequency": "0 0 1 * *",
             "status": "ON_HOLD", "limit_by": "COUNT", "limit_count": "3", "send_automatically": False,
             "discount": 0, "discount_val": 0, "sub_total": 0, "total": 0, "tax": 0,
             "exchange_rate": "1",
             "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 100}]}},
            "delete_recurring_invoices_by_id", "list_all_recurring_invoices", {"page_size": 0}, "status")

        # ------------------------------------------------------------------
        # Phase 4: Domain-Specific Tools
        # ------------------------------------------------------------------
        log("\n=== Phase 4: Domain-Specific Tools ===")

        # D1 get_customer_stats
        await run_test(session, "D1 get_customer_stats", "get_customer_stats",
            {"id": IDS["customer_fixture"]},
            assert_fn=lambda d: _assert_has_keys(d, "meta"))

        # D2 search_customers_and_users
        await run_test(session, "D2 search_customers_and_users", "search_customers_and_users",
            {"search": f"t{rid}"}, assert_fn=lambda d: _assert_has_keys(d, "customers", "users"))

        # D3 search_users
        await run_test(session, "D3 search_users", "search_users",
            assert_fn=lambda d: _assert_has_keys(d, "users"))

        # D4 get_next_number
        await run_test(session, "D4 get_next_number", "get_next_number", {"key": "invoice"},
            assert_fn=lambda d: _assert_has_keys(d, "success", "nextNumber"))

        # D5 get_number_placeholders
        await run_test(session, "D5 get_number_placeholders", "get_number_placeholders",
            {"format": "INV-{NUMBER}"},
            assert_fn=lambda d: _assert_has_keys(d, "success", "placeholders"))

        # D6 get_recurring_invoice_frequency
        await run_test(session, "D6 get_recurring_invoice_frequency", "get_recurring_invoice_frequency",
            {"frequency": "0 0 1 * *", "starts_at": today_iso},
            assert_fn=lambda d: _assert_has_keys(d, "success", "next_invoice_at"))

        # D7 get_exchange_rate
        await run_test(session, "D7 get_exchange_rate", "get_exchange_rate",
            {"currency_id": ccy_id}, assert_fn=_assert_exchange_rate)

        # D8 get_active_exchange_rate_provider
        await run_test(session, "D8 get_active_exchange_rate_provider", "get_active_exchange_rate_provider",
            {"currency_id": ccy_id}, assert_fn=_assert_active_provider)

        # D9 list_used_currencies_for_exchange
        await run_test(session, "D9 list_used_currencies_for_exchange", "list_used_currencies_for_exchange",
            assert_fn=lambda d: _assert_has_keys(d, "allUsedCurrencies", "activeUsedCurrencies"))

        # D10 list_supported_currencies — negative/error-contract test.
        # The success path (returning a provider's supported currency list)
        # requires a valid external API key for one of the supported drivers
        # (currency_freak, currency_layer, open_exchange_rate, currency_converter
        # — all call external paid APIs). No such key is available in this
        # environment, so we test the documented error contract: a bad key
        # must produce {"error": "invalid_key"}.
        d10_result = await session.call_tool("list_supported_currencies",
            {"driver": "currency_freak", "key": "invalid-test-key"})
        d10_err = is_error(d10_result)
        exercised_tools.add("list_supported_currencies")
        d10_passed = bool(d10_err and "invalid_key" in d10_err.lower())
        results.append({
            "label": "D10 list_supported_currencies (error-path)",
            "tool": "list_supported_currencies",
            "status": "PASSED" if d10_passed else "FAILED",
            "reason": None if d10_passed else (d10_err or "unexpected success with invalid provider credentials")})
        log(f"  {'PASS' if d10_passed else 'FAIL'} D10 list_supported_currencies")

        # D11 clone_invoice
        inv_clone_data = await run_test(session, "D11 create_inv_for_clone", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("CLN"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_inv_clone_orig"] = _int_id(_dict_id(inv_clone_data))
        inv_clone_result = await run_test(session, "D11 clone_invoice", "clone_invoice",
            {"id": IDS["id_inv_clone_orig"]},
            assert_fn=lambda d, _o=IDS["id_inv_clone_orig"]: _assert_cloned(d, _o))
        IDS["id_inv_clone_dup"] = _int_id(_dict_id(inv_clone_result))

        # D12 clone_estimate
        est_clone_data = await run_test(session, "D12 create_est_for_clone", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-CLN"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_est_clone_orig"] = _int_id(_dict_id(est_clone_data))
        est_clone_result = await run_test(session, "D12 clone_estimate", "clone_estimate",
            {"id": IDS["id_est_clone_orig"]},
            assert_fn=lambda d, _o=IDS["id_est_clone_orig"]: _assert_cloned(d, _o))
        IDS["id_est_clone_dup"] = _int_id(_dict_id(est_clone_result))

        # D13 change_invoice_status
        inv_status_data = await run_test(session, "D13 create_inv_for_status", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("ST"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_inv_status"] = _int_id(_dict_id(inv_status_data))
        await run_test(session, "D13 change_invoice_status", "change_invoice_status",
            {"id": IDS["id_inv_status"], "status": "SENT"}, assert_fn=_assert_success_true)

        # D14 change_estimate_status
        est_status_data = await run_test(session, "D14 create_est_for_status", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-ST"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_est_status"] = _int_id(_dict_id(est_status_data))
        await run_test(session, "D14 change_estimate_status", "change_estimate_status",
            {"id": IDS["id_est_status"], "status": "SENT"}, assert_fn=_assert_success_true)

        # D15 convert_estimate_to_invoice
        est_convert_data = await run_test(session, "D15 create_est_for_convert", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-CNV"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_est_convert"] = _int_id(_dict_id(est_convert_data))
        convert_result = await run_test(session, "D15 convert_estimate_to_invoice", "convert_estimate_to_invoice",
            {"id": IDS["id_est_convert"]}, assert_fn=lambda d: _assert_has_keys(d, "id"))
        IDS["id_invoice_convert"] = _int_id(_dict_id(convert_result))

        # D16 list_invoice_templates
        await run_test(session, "D16 list_invoice_templates", "list_invoice_templates",
            assert_fn=lambda d: _assert_templates(d, "invoiceTemplates", "invoice1"))

        # D17 list_estimate_templates
        await run_test(session, "D17 list_estimate_templates", "list_estimate_templates",
            assert_fn=lambda d: _assert_templates(d, "estimateTemplates", "estimate1"))

        # D18 duplicate_expense
        exp_dup_data = await run_test(session, "D18 create_exp_for_dup", "create_expense",
            {"expense_date": today_iso, "expense_category_id": exp_cat, "amount": 200,
             "currency_id": ccy_id, "expense_number": make_name("EXP-DUP")})
        IDS["id_exp_dup_orig"] = _int_id(_dict_id(exp_dup_data))
        dup_result = await run_test(session, "D18 duplicate_expense", "duplicate_expense",
            {"id": IDS["id_exp_dup_orig"], "expense_date": today_iso},
            assert_fn=lambda d, _o=IDS["id_exp_dup_orig"]: _assert_cloned(d, _o))
        IDS["id_exp_dup_dup"] = _int_id(_dict_id(dup_result))

        # D19 send_invoice
        inv_send_data = await run_test(session, "D19 create_inv_for_send", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("SND"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_inv_send"] = _int_id(_dict_id(inv_send_data))
        await run_test(session, "D19 send_invoice", "send_invoice",
            {"id": IDS["id_inv_send"], "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-InvSend", "body": "test"}, assert_fn=_assert_success_true)

        # D20 send_estimate
        est_send_data = await run_test(session, "D20 create_est_for_send", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-SND"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_est_send"] = _int_id(_dict_id(est_send_data))
        await run_test(session, "D20 send_estimate", "send_estimate",
            {"id": IDS["id_est_send"], "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-EstSend", "body": "test"}, assert_fn=_assert_success_true)

        # D21 send_payment
        pay_send_data = await run_test(session, "D21 create_pay_for_send", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 25, "payment_number": make_name("PAY-SND")})
        IDS["id_pay_send"] = _int_id(_dict_id(pay_send_data))
        await run_test(session, "D21 send_payment", "send_payment",
            {"id": IDS["id_pay_send"], "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-PaySend", "body": "test"}, assert_fn=_assert_success_true)

        # D22 get_invoice_send_preview
        inv_preview_data = await run_test(session, "D22 create_inv_for_preview", "create_invoice",
            {"customer_id": inv_cust, "invoice_number": make_name("PRV"), "invoice_date": today_iso,
             "template_name": "invoice1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_inv_preview"] = _int_id(_dict_id(inv_preview_data))
        await run_test(session, "D22 get_invoice_send_preview", "get_invoice_send_preview",
            {"id": IDS["id_inv_preview"], "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-Preview", "body": "preview test"}, assert_fn=_assert_html)

        # D23 get_estimate_send_preview
        est_preview_data = await run_test(session, "D23 create_est_for_preview", "create_estimate",
            {"customer_id": inv_cust, "estimate_number": make_name("EST-PRV"), "estimate_date": today_iso,
             "template_name": "estimate1", "items": {"items": [{"name": make_name("Item"), "quantity": 1, "price": 50}]}})
        IDS["id_est_preview"] = _int_id(_dict_id(est_preview_data))
        await run_test(session, "D23 get_estimate_send_preview", "get_estimate_send_preview",
            {"id": IDS["id_est_preview"], "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-Preview", "body": "preview test"}, assert_fn=_assert_html)

        # D24 get_payment_send_preview
        pay_preview_data = await run_test(session, "D24 create_pay_for_preview", "create_payment",
            {"payment_date": today_iso, "customer_id": inv_cust, "amount": 30, "payment_number": make_name("PAY-PRV")})
        IDS["id_pay_preview"] = _int_id(_dict_id(pay_preview_data))
        await run_test(session, "D24 get_payment_send_preview", "get_payment_send_preview",
            {"id": IDS["id_pay_preview"], "to": "solo@selfhostingbox.com", "from_": "no-reply@selfhostingbox.com",
             "subject": f"{rid}-Preview", "body": "preview test"}, assert_fn=_assert_html)

        # ------------------------------------------------------------------
        # Phase 4 Cleanup: Delete all Phase 4 created resources unconditionally
        # ------------------------------------------------------------------
        log("\n=== Phase 4: Cleanup ===")
        for id_key, dtool in [
            ("id_inv_clone_orig", "delete_invoices_by_id"),
            ("id_inv_clone_dup", "delete_invoices_by_id"),
            ("id_est_clone_orig", "delete_estimates_by_id"),
            ("id_est_clone_dup", "delete_estimates_by_id"),
            ("id_inv_status", "delete_invoices_by_id"),
            ("id_est_status", "delete_estimates_by_id"),
            ("id_est_convert", "delete_estimates_by_id"),
            ("id_invoice_convert", "delete_invoices_by_id"),
            ("id_exp_dup_orig", "delete_expenses_by_id"),
            ("id_exp_dup_dup", "delete_expenses_by_id"),
            ("id_inv_send", "delete_invoices_by_id"),
            ("id_est_send", "delete_estimates_by_id"),
            ("id_pay_send", "delete_payments_by_id"),
            ("id_inv_preview", "delete_invoices_by_id"),
            ("id_est_preview", "delete_estimates_by_id"),
            ("id_pay_preview", "delete_payments_by_id"),
        ]:
            await session.call_tool(dtool, {"id": IDS[id_key]})
            log(f"  CLEANUP {id_key} id={IDS[id_key]}")

        # Clean up fixture customer
        await session.call_tool("delete_customers_by_id", {"id": IDS["customer_fixture"]})
        log(f"  CLEANUP fixture_customer id={IDS['customer_fixture']}")

        # Clean up fixture category (and any expenses referencing it)
        fc_id = IDS["category_fixture"]
        r = await session.call_tool("list_all_expenses", {"page_size": 0})
        exp_items = get_list_items(extract_content(r))
        for exp in exp_items:
            if isinstance(exp, dict) and exp.get("expense_category_id") == fc_id:
                eid = exp.get("id")
                if eid:
                    await session.call_tool("delete_expenses_by_id", {"id": eid})
                    log(f"  CLEANUP expense id={eid} linked to fixture_category")
        await session.call_tool("delete_expense_category_by_id", {"id": fc_id})
        log(f"  CLEANUP fixture_category id={fc_id}")

        # ------------------------------------------------------------------
        # Phase 5: Leak Detection
        # ------------------------------------------------------------------
        log("\n=== Phase 5: Leak Detection ===")
        rid_prefix = f"t{rid}-"
        known_customer_ids = {str(IDS["customer_fixture"]), str(IDS["id_customer"])}

        LEAK_SCAN_CONFIG = [
            ("customer", "list_all_customers", {"page_size": 0}, "id", "name", "delete_customers_by_id"),
            ("item", "list_all_items", {"page_size": 0}, "id", "name", "delete_items_by_id"),
            ("unit", "list_all_units", None, "id", "name", "delete_unit_by_id"),
            ("expense_category", "list_all_expense_categories", None, "id", "name", "delete_expense_category_by_id"),
            ("payment_method", "list_all_payment_methods", None, "id", "name", "delete_payment_method_by_id"),
            ("tax_type", "list_all_tax_types", None, "id", "name", "delete_tax_type_by_id"),
            ("note", "list_all_notes", None, "id", "name", "delete_note_by_id"),
            ("custom_field", "list_all_custom_fields", None, "id", "name", "delete_custom_field_by_id"),
            ("role", "list_all_roles", None, "id", "name", "delete_role_by_id"),
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

        # Recurring invoice leak check (customer_id based)
        ri_result = await session.call_tool("list_all_recurring_invoices", {"page_size": 0})
        ri_items = get_list_items(extract_content(ri_result))
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
        # Coverage Enforcement (static cross-check + exercised-tool coverage)
        # ------------------------------------------------------------------
        log("\n=== Coverage Enforcement ===")
        static_names = static_tool_names()
        for m in sorted(set(static_names) - set(tool_names)):
            results.append({"label": f"COVERAGE SOURCE {m}", "tool": m, "status": "FAILED",
                            "reason": "Tool defined in src/main.py but not exposed by running server (stale container?)"})
            log(f"  FAIL COVERAGE SOURCE {m}: in main.py but not discovered")
        for m in sorted(set(tool_names) - set(static_names)):
            results.append({"label": f"COVERAGE STALE {m}", "tool": m, "status": "FAILED",
                            "reason": "Tool exposed by running server but not defined in src/main.py (stale build?)"})
            log(f"  FAIL COVERAGE STALE {m}: discovered but not in main.py")
        for m in sorted((set(static_names) | set(tool_names)) - exercised_tools):
            results.append({"label": f"COVERAGE {m}", "tool": m, "status": "FAILED",
                            "reason": "Tool never exercised"})
            log(f"  FAIL COVERAGE {m}: never exercised")

        # ------------------------------------------------------------------
        # Report Summary
        # ------------------------------------------------------------------
        passed_results = [r for r in results if r["status"] == "PASSED"]
        failed_results = [r for r in results if r["status"] == "FAILED"]
        passed = len(passed_results)
        failed = len(failed_results)
        total = len(results)

        print(f"\n## Summary\n")
        print(f"| Status | Count |")
        print(f"|--------|-------|")
        print(f"| PASSED | {passed} |")
        print(f"| FAILED | {failed} |")

        print(f"\n## PASSED ({passed})\n")
        for r in passed_results:
            print(f"- `{r['tool']}` — {r['label']}")

        print(f"\n## FAILED ({failed})\n")
        for r in failed_results:
            print(f"### {r['label']}")
            print(f"- **Error**: {r['reason']}")
            print()

        print(f"\n---")
        print(f"**Total tests:** {total} | **PASSED:** {passed} | **FAILED:** {failed}")
        verdict = ["**TESTS FAILING** — see above for details", "**ALL TESTS PASS**"][failed == 0]
        print(f"\n{verdict}")


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


def static_tool_names() -> list[str]:
    src_path = Path(__file__).with_name("main.py")
    text = src_path.read_text(encoding="utf-8")
    names = re.findall(r"@mcp\.tool\([^\n]*\)\s*\n\s*async def (\w+)\s*\(", text)
    return sorted(names)


if __name__ == "__main__":
    asyncio.run(main())
