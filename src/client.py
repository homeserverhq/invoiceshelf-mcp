import copy
import datetime as dt
import os
import re
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx


COMMON_FIELDS: dict[str, set[str]] = {
    "customer": {"id", "name", "email", "currency_id", "phone"},
    "item": {"id", "name", "price", "unit_id", "currency_id"},
    "unit": {"id", "name"},
    "invoice": {"id", "invoice_number", "customer_id", "status", "paid_status", "total"},
    "estimate": {"id", "estimate_number", "customer_id", "status", "total"},
    "expense": {"id", "expense_date", "amount", "expense_category_id", "customer_id"},
    "expense_category": {"id", "name", "description", "company_id"},
    "payment": {"id", "payment_number", "customer_id", "invoice_id", "amount", "payment_date"},
    "payment_method": {"id", "name", "type"},
    "custom_field": {"id", "name", "label", "model_type", "type"},
    "tax_type": {"id", "name", "calculation_type", "percent"},
    "note": {"id", "name", "type", "is_default"},
    "role": {"id", "name", "title"},
    "recurring_invoice": {"id", "customer_id", "frequency", "status", "total"},
    "company": {"id", "name", "slug", "owner_id"},
    "timezone": {"value", "key"},
    "date_format": {"display_date", "carbon_format_value", "moment_format_value"},
    "time_format": {"display_time", "carbon_format_value", "moment_format_value"},
    "ability": {"name", "ability", "model"},
    "invoice_template": {"name"},
    "estimate_template": {"name"},
}

MONEY_FIELDS: set[str] = {
    "price", "amount", "sub_total", "total", "tax", "due_amount",
    "base_price", "base_sub_total", "base_total", "base_tax", "base_due_amount",
    "base_discount_val",
    "total_amount_due", "total_sales", "total_receipts", "total_expenses",
    "total_net_income", "invoice_totals", "expense_totals", "receipt_totals",
    "net_income_totals",
}

PREFIX = "/api/v1"


def _filter_fields(data: Any, common_set: set[str]) -> Any:
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in common_set}
    if isinstance(data, list):
        return [_filter_fields(item, common_set) for item in data]
    return data


def _strip_roles(data: Any) -> Any:
    """Recursively remove the token-heavy 'roles' key from any dict."""
    if isinstance(data, dict):
        return {k: _strip_roles(v) for k, v in data.items() if k != "roles"}
    if isinstance(data, list):
        return [_strip_roles(item) for item in data]
    return data


def _money_amount(value: Any) -> Optional[float]:
    """Return a numeric amount for an int/float or a numeric string, else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if s and re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", s):
            try:
                return float(s)
            except ValueError:
                return None
    return None


def _convert_money(amount: float, factor: float, precision: int, is_outbound: bool) -> Any:
    """Convert a single money amount between minor units (cents) and whole units."""
    if is_outbound:
        return int(round(amount * factor))
    value = round(amount / factor, precision)
    if float(value).is_integer():
        return int(value)
    return value


def _apply_money_factor(data: Any, factor: float, precision: int, is_outbound: bool = False) -> None:
    """Apply factor to MONEY_FIELDS in a single dict node. Does NOT recurse."""
    if not isinstance(data, dict):
        return
    for k, v in list(data.items()):
        money_field = k in MONEY_FIELDS or (
            k == "discount_val"
            and _money_amount(v) is not None
            and data.get("discount_type") == "fixed"
        )
        if not money_field:
            continue
        if isinstance(v, (list, tuple)):
            converted: list[Any] = []
            for x in v:
                amt = _money_amount(x)
                if amt is None:
                    converted.append(x)
                else:
                    converted.append(_convert_money(amt, factor, precision, is_outbound))
            data[k] = converted
        else:
            amt = _money_amount(v)
            if amt is not None:
                data[k] = _convert_money(amt, factor, precision, is_outbound)


def _normalize_datetime(value: str) -> str:
    if re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', value):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        parsed = parsed.astimezone(dt.timezone.utc)
        return parsed.strftime('%Y-%m-%d')
    return value


def _denormalize_datetime(value: str) -> str:
    if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', value):
        parsed = dt.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    return value


def _denormalize_response(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _denormalize_response(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_denormalize_response(item) for item in data]
    if isinstance(data, str):
        return _denormalize_datetime(data)
    return data


class InvoiceShelfClient:

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or os.getenv("INVOICESHELF_BASE_URL", "")).rstrip("/")
        if not self.base_url:
            raise ValueError(
                "InvoiceShelf URL required. Set INVOICESHELF_BASE_URL env var "
                "or pass base_url."
            )
        self.public_url = os.getenv("INVOICESHELF_PUBLIC_URL", "").rstrip("/") or self.base_url
        # When a public URL is configured, forward its origin to the backend via
        # Host / X-Forwarded-Proto so the backend builds links (JSON responses
        # AND email bodies) with the public-facing origin instead of the internal
        # Docker host. The backend derives links from the request Host header.
        self._public_headers: dict[str, str] = {}
        if self.public_url != self.base_url:
            try:
                parts = urlsplit(self.public_url)
                if parts.netloc:
                    self._public_headers["Host"] = parts.netloc
                    self._public_headers["X-Forwarded-Proto"] = (parts.scheme or "http").lower()
                    if parts.port:
                        self._public_headers["X-Forwarded-Port"] = str(parts.port)
            except ValueError:
                self._public_headers = {}
        self._currencies_by_id: dict[int, dict] = {}
        self._currency_seeding: bool = False
        self._bootstrap_seeded: bool = False
        self._company_default_precision: int = 2
        self._company_currency_id: Optional[int] = None

    def _get_headers(self, api_key: Optional[str] = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def request(self, method: str, path: str, api_key: Optional[str] = None, keep_roles: bool = False, convert_money: bool = True, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._get_headers(api_key)
        headers.update(self._public_headers)
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
            if response.status_code >= 400:
                body = response.text[:500]
                raise httpx.HTTPStatusError(
                    f"{response.status_code} {response.reason_phrase} for {method} {path}: {body}",
                    request=response.request, response=response,
                )
            if response.status_code == 204:
                return {}
            if response.headers.get("content-type", "").startswith("application/json"):
                data = response.json()
                # Inbound money conversion on the response
                if convert_money:
                    await self._ensure_currencies(api_key)
                    await self._ensure_company_currency(api_key)
                    self._inbound_money_convert(data, self._company_default_precision)
                if not keep_roles:
                    data = _strip_roles(data)
                data = self._rewrite_public_urls(data)
                return data
            return {"text": response.text}

    async def get(self, path: str, api_key: Optional[str] = None, **kwargs: Any) -> Any:
        return await self.request("GET", path, api_key, **kwargs)

    async def post(self, path: str, api_key: Optional[str] = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, api_key, **kwargs)

    async def put(self, path: str, api_key: Optional[str] = None, **kwargs: Any) -> Any:
        return await self.request("PUT", path, api_key, **kwargs)

    async def delete(self, path: str, api_key: Optional[str] = None, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, api_key, **kwargs)

    def _unwrap(self, data: Any) -> Any:
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def _apply(self, data: Any, api_key: Optional[str] = None, include_all_fields: bool = False, resource_key: str = "") -> Any:
        result = self._unwrap(data)
        if not include_all_fields and resource_key:
            result = _filter_fields(result, COMMON_FIELDS[resource_key])
        return _denormalize_response(result)

    async def _normalize_payload(self, payload: dict[str, Any], api_key: Optional[str] = None) -> dict[str, Any]:
        result = {
            k: _normalize_datetime(v) if isinstance(v, str) else copy.deepcopy(v)
            for k, v in payload.items()
        }
        if api_key:
            await self._ensure_currencies(api_key)
            await self._ensure_company_currency(api_key)
            self._outbound_money_convert(result, self._company_default_precision)
        return result

    def _rewrite_public_urls(self, data: Any) -> Any:
        """Rewrite any client-facing URL built on the internal base origin to the public URL."""
        if self.public_url == self.base_url:
            return data
        if isinstance(data, dict):
            return {k: self._rewrite_public_urls(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._rewrite_public_urls(item) for item in data]
        if isinstance(data, str) and self.base_url in data:
            return data.replace(self.base_url, self.public_url)
        return data

    # =========================================================================
    # Currency-aware money conversion (used by request())
    # =========================================================================

    async def _ensure_currencies(self, api_key: Optional[str] = None) -> None:
        if self._currencies_by_id or self._currency_seeding:
            return
        self._currency_seeding = True
        try:
            data = await self.get(f"{PREFIX}/currencies", api_key)
            for c in (self._unwrap(data) or []):
                if isinstance(c, dict) and "id" in c:
                    cid = int(c["id"])
                    self._currencies_by_id[cid] = c
        except Exception:
            self._currencies_by_id = {}
        finally:
            self._currency_seeding = False

    async def _ensure_company_currency(self, api_key: Optional[str] = None) -> None:
        if self._bootstrap_seeded:
            return
        self._bootstrap_seeded = True
        try:
            data = await self.get(f"{PREFIX}/bootstrap", api_key)
            unwrapped = self._unwrap(data)
            ccc = (unwrapped or {}).get("current_company_currency") or {}
            if ccc.get("precision") is not None:
                self._company_default_precision = int(ccc["precision"])
            if ccc.get("id") is not None:
                self._company_currency_id = int(ccc["id"])
        except Exception:
            self._company_default_precision = 2

    async def resolve_company_currency_id(self, api_key: Optional[str] = None, provided: Any = None) -> int:
        if provided not in (None, "", 0):
            return int(provided)
        await self._ensure_company_currency(api_key)
        if self._company_currency_id is None:
            raise RuntimeError("Unable to resolve company default currency; specify currency_id explicitly")
        return self._company_currency_id

    def _get_precision(self, currency_id: Any) -> int:
        if currency_id is not None:
            c = self._currencies_by_id.get(int(currency_id))
            if c:
                return int(c.get("precision", 2))
        return self._company_default_precision

    def _inbound_money_convert(self, data: Any, default_precision: int) -> None:
        if isinstance(data, dict):
            cid = data.get("currency_id")
            precision = self._get_precision(cid) if cid is not None else default_precision
            _apply_money_factor(data, 10 ** precision, precision, is_outbound=False)
            for v in data.values():
                if isinstance(v, (dict, list)):
                    self._inbound_money_convert(v, precision)
        elif isinstance(data, list):
            for item in data:
                self._inbound_money_convert(item, default_precision)

    def _outbound_money_convert(self, data: Any, default_precision: int) -> None:
        if isinstance(data, dict):
            cid = data.get("currency_id")
            precision = self._get_precision(cid) if cid is not None else default_precision
            _apply_money_factor(data, 10 ** precision, precision, is_outbound=True)
            for k, v in list(data.items()):
                if isinstance(v, dict):
                    self._outbound_money_convert(v, precision)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            self._outbound_money_convert(item, precision)

    # =========================================================================
    # Customers
    # =========================================================================

    async def list_all_customers(self, api_key: Optional[str] = None, include_all_fields: bool = False, page: int = 0, page_size: int = 10) -> Any:
        params = {}
        if page_size > 0:
            params["limit"] = str(page_size)
            params["page"] = str(page if page and page > 0 else 1)
        else:
            params["limit"] = "all"
        data = await self.get(f"{PREFIX}/customers", api_key, params=params or None)
        return self._apply(data, api_key, include_all_fields, "customer")

    async def get_customer_by_id(self, customer_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/customers/{customer_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "customer")

    async def create_customer(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/customers", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "customer")

    async def update_customer(self, customer_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/customers/{customer_id}", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "customer")

    async def delete_customers(self, ids: list[int], api_key: Optional[str] = None) -> Any:
        return await self.post(f"{PREFIX}/customers/delete", api_key, json={"ids": ids})

    async def get_customer_stats(self, customer_id: int, api_key: Optional[str] = None, previous_year: bool = False) -> Any:
        params = {"previous_year": "true" if previous_year else "false"}
        data = await self.get(f"{PREFIX}/customers/{customer_id}/stats", api_key, params=params)
        unwrapped = self._unwrap(data)
        # The backend returns the stats in the top-level "meta" (chartData)
        # alongside the customer resource "data". Preserve meta so the tool
        # actually returns the statistics, not just the customer profile.
        if isinstance(data, dict) and isinstance(unwrapped, dict) and "meta" in data:
            return {**unwrapped, "meta": data["meta"]}
        return unwrapped

    async def get_customer_roles_by_id(self, customer_id: int, api_key: Optional[str] = None) -> Any:
        data = await self.get(f"{PREFIX}/customers/{customer_id}", api_key, keep_roles=True, convert_money=False)
        unwrapped = self._unwrap(data)
        return {"roles": unwrapped.get("roles", []) if isinstance(unwrapped, dict) else []}

    # =========================================================================
    # Items
    # =========================================================================

    async def list_all_items(self, api_key: Optional[str] = None, include_all_fields: bool = False, page: int = 0, page_size: int = 10) -> Any:
        params = {}
        if page_size > 0:
            params["limit"] = str(page_size)
            params["page"] = str(page if page and page > 0 else 1)
        else:
            params["limit"] = "all"
        data = await self.get(f"{PREFIX}/items", api_key, params=params or None)
        return self._apply(data, api_key, include_all_fields, "item")

    async def get_item_by_id(self, item_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/items/{item_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "item")

    async def create_item(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/items", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "item")

    async def update_item(self, item_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/items/{item_id}", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "item")

    async def delete_items(self, ids: list[int], api_key: Optional[str] = None) -> Any:
        return await self.post(f"{PREFIX}/items/delete", api_key, json={"ids": ids})

    # =========================================================================
    # Units
    # =========================================================================

    async def list_all_units(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/units", api_key)
        return self._apply(data, api_key, include_all_fields, "unit")

    async def get_unit_by_id(self, unit_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/units/{unit_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "unit")

    async def create_unit(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/units", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "unit")

    async def update_unit(self, unit_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/units/{unit_id}", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "unit")

    async def delete_unit_by_id(self, unit_id: int, api_key: Optional[str] = None) -> Any:
        return await self.delete(f"{PREFIX}/units/{unit_id}", api_key)

    # =========================================================================
    # Invoices
    # =========================================================================

    async def list_all_invoices(self, api_key: Optional[str] = None, include_all_fields: bool = False, page: int = 0, page_size: int = 10) -> Any:
        params = {}
        if page_size > 0:
            params["limit"] = str(page_size)
            params["page"] = str(page if page and page > 0 else 1)
        else:
            params["limit"] = "all"
        data = await self.get(f"{PREFIX}/invoices", api_key, params=params or None)
        return self._apply(data, api_key, include_all_fields, "invoice")

    async def get_invoice_by_id(self, invoice_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/invoices/{invoice_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "invoice")

    async def create_invoice(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/invoices", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "invoice")

    async def update_invoice(self, invoice_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/invoices/{invoice_id}", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "invoice")

    async def delete_invoices(self, ids: list[int], api_key: Optional[str] = None) -> Any:
        return await self.post(f"{PREFIX}/invoices/delete", api_key, json={"ids": ids})

    async def send_invoice(self, invoice_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        data = await self.post(f"{PREFIX}/invoices/{invoice_id}/send", api_key, json=payload)
        return self._unwrap(data)

    async def clone_invoice(self, invoice_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/invoices/{invoice_id}/clone", api_key)
        return self._apply(data, api_key, include_all_fields, "invoice")

    async def change_invoice_status(self, invoice_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        data = await self.post(f"{PREFIX}/invoices/{invoice_id}/status", api_key, json=payload)
        return self._unwrap(data)

    async def list_invoice_templates(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/invoices/templates", api_key)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict) and isinstance(result.get("invoiceTemplates"), list):
            result["invoiceTemplates"] = _filter_fields(result["invoiceTemplates"], COMMON_FIELDS["invoice_template"])
        return result

    async def get_invoice_send_preview(self, invoice_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        params = {k: v for k, v in payload.items() if v}
        data = await self.get(f"{PREFIX}/invoices/{invoice_id}/send/preview", api_key, params=params)
        return {"html": data.get("text", "") if isinstance(data, dict) else str(data)}

    # =========================================================================
    # Estimates
    # =========================================================================

    async def list_all_estimates(self, api_key: Optional[str] = None, include_all_fields: bool = False, page: int = 0, page_size: int = 10) -> Any:
        params = {}
        if page_size > 0:
            params["limit"] = str(page_size)
            params["page"] = str(page if page and page > 0 else 1)
        else:
            params["limit"] = "all"
        data = await self.get(f"{PREFIX}/estimates", api_key, params=params or None)
        return self._apply(data, api_key, include_all_fields, "estimate")

    async def get_estimate_by_id(self, estimate_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/estimates/{estimate_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "estimate")

    async def create_estimate(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/estimates", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "estimate")

    async def update_estimate(self, estimate_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/estimates/{estimate_id}", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "estimate")

    async def delete_estimates(self, ids: list[int], api_key: Optional[str] = None) -> Any:
        return await self.post(f"{PREFIX}/estimates/delete", api_key, json={"ids": ids})

    async def send_estimate(self, estimate_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        data = await self.post(f"{PREFIX}/estimates/{estimate_id}/send", api_key, json=payload)
        return self._unwrap(data)

    async def clone_estimate(self, estimate_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/estimates/{estimate_id}/clone", api_key)
        return self._apply(data, api_key, include_all_fields, "estimate")

    async def change_estimate_status(self, estimate_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        data = await self.post(f"{PREFIX}/estimates/{estimate_id}/status", api_key, json=payload)
        return self._unwrap(data)

    async def convert_estimate_to_invoice(self, estimate_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/estimates/{estimate_id}/convert-to-invoice", api_key)
        return self._apply(data, api_key, include_all_fields, "invoice")

    async def list_estimate_templates(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/estimates/templates", api_key)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict) and isinstance(result.get("estimateTemplates"), list):
            result["estimateTemplates"] = _filter_fields(result["estimateTemplates"], COMMON_FIELDS["estimate_template"])
        return result

    async def get_estimate_send_preview(self, estimate_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        params = {k: v for k, v in payload.items() if v}
        data = await self.get(f"{PREFIX}/estimates/{estimate_id}/send/preview", api_key, params=params)
        return {"html": data.get("text", "") if isinstance(data, dict) else str(data)}

    # =========================================================================
    # Expenses
    # =========================================================================

    async def list_all_expenses(self, api_key: Optional[str] = None, include_all_fields: bool = False, page: int = 0, page_size: int = 10) -> Any:
        params = {}
        if page_size > 0:
            params["limit"] = str(page_size)
            params["page"] = str(page if page and page > 0 else 1)
        else:
            params["limit"] = "all"
        data = await self.get(f"{PREFIX}/expenses", api_key, params=params or None)
        return self._apply(data, api_key, include_all_fields, "expense")

    async def get_expense_by_id(self, expense_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/expenses/{expense_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "expense")

    async def create_expense(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/expenses", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "expense")

    async def update_expense(self, expense_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/expenses/{expense_id}", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "expense")

    async def delete_expenses(self, ids: list[int], api_key: Optional[str] = None) -> Any:
        return await self.post(f"{PREFIX}/expenses/delete", api_key, json={"ids": ids})

    async def duplicate_expense(self, expense_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/expenses/{expense_id}/duplicate", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "expense")

    # =========================================================================
    # Expense Categories
    # =========================================================================

    async def list_all_expense_categories(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/categories", api_key)
        return self._apply(data, api_key, include_all_fields, "expense_category")

    async def get_expense_category_by_id(self, category_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/categories/{category_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "expense_category")

    async def create_expense_category(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/categories", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "expense_category")

    async def update_expense_category(self, category_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/categories/{category_id}", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "expense_category")

    async def delete_expense_category_by_id(self, category_id: int, api_key: Optional[str] = None) -> Any:
        return await self.delete(f"{PREFIX}/categories/{category_id}", api_key)

    # =========================================================================
    # Payments
    # =========================================================================

    async def list_all_payments(self, api_key: Optional[str] = None, include_all_fields: bool = False, page: int = 0, page_size: int = 10) -> Any:
        params = {}
        if page_size > 0:
            params["limit"] = str(page_size)
            params["page"] = str(page if page and page > 0 else 1)
        else:
            params["limit"] = "all"
        data = await self.get(f"{PREFIX}/payments", api_key, params=params or None)
        return self._apply(data, api_key, include_all_fields, "payment")

    async def get_payment_by_id(self, payment_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/payments/{payment_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "payment")

    async def create_payment(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/payments", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "payment")

    async def update_payment(self, payment_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/payments/{payment_id}", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "payment")

    async def delete_payments(self, ids: list[int], api_key: Optional[str] = None) -> Any:
        return await self.post(f"{PREFIX}/payments/delete", api_key, json={"ids": ids})

    async def send_payment(self, payment_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        data = await self.post(f"{PREFIX}/payments/{payment_id}/send", api_key, json=payload)
        return self._unwrap(data)

    async def get_payment_send_preview(self, payment_id: int, payload: dict[str, Any], api_key: Optional[str] = None) -> Any:
        params = {k: v for k, v in payload.items() if v}
        data = await self.get(f"{PREFIX}/payments/{payment_id}/send/preview", api_key, params=params)
        return {"html": data.get("text", "") if isinstance(data, dict) else str(data)}

    # =========================================================================
    # Payment Methods
    # =========================================================================

    async def list_all_payment_methods(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/payment-methods", api_key)
        return self._apply(data, api_key, include_all_fields, "payment_method")

    async def get_payment_method_by_id(self, method_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/payment-methods/{method_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "payment_method")

    async def create_payment_method(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/payment-methods", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "payment_method")

    async def update_payment_method(self, method_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/payment-methods/{method_id}", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "payment_method")

    async def delete_payment_method_by_id(self, method_id: int, api_key: Optional[str] = None) -> Any:
        return await self.delete(f"{PREFIX}/payment-methods/{method_id}", api_key)

    # =========================================================================
    # Custom Fields
    # =========================================================================

    async def list_all_custom_fields(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/custom-fields", api_key)
        return self._apply(data, api_key, include_all_fields, "custom_field")

    async def get_custom_field_by_id(self, field_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/custom-fields/{field_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "custom_field")

    async def create_custom_field(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/custom-fields", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "custom_field")

    async def update_custom_field(self, field_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/custom-fields/{field_id}", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "custom_field")

    async def delete_custom_field_by_id(self, field_id: int, api_key: Optional[str] = None) -> Any:
        return await self.delete(f"{PREFIX}/custom-fields/{field_id}", api_key)

    # =========================================================================
    # Tax Types
    # =========================================================================

    async def list_all_tax_types(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/tax-types", api_key)
        return self._apply(data, api_key, include_all_fields, "tax_type")

    async def get_tax_type_by_id(self, tax_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/tax-types/{tax_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "tax_type")

    async def create_tax_type(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/tax-types", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "tax_type")

    async def update_tax_type(self, tax_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/tax-types/{tax_id}", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "tax_type")

    async def delete_tax_type_by_id(self, tax_id: int, api_key: Optional[str] = None) -> Any:
        return await self.delete(f"{PREFIX}/tax-types/{tax_id}", api_key)

    # =========================================================================
    # Notes
    # =========================================================================

    async def list_all_notes(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/notes", api_key)
        return self._apply(data, api_key, include_all_fields, "note")

    async def get_note_by_id(self, note_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/notes/{note_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "note")

    async def create_note(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/notes", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "note")

    async def update_note(self, note_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/notes/{note_id}", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "note")

    async def delete_note_by_id(self, note_id: int, api_key: Optional[str] = None) -> Any:
        return await self.delete(f"{PREFIX}/notes/{note_id}", api_key)

    # =========================================================================
    # Recurring Invoices
    # =========================================================================

    async def list_all_recurring_invoices(self, api_key: Optional[str] = None, include_all_fields: bool = False, page: int = 0, page_size: int = 10) -> Any:
        params = {}
        if page_size > 0:
            params["limit"] = str(page_size)
            params["page"] = str(page if page and page > 0 else 1)
        else:
            params["limit"] = "all"
        data = await self.get(f"{PREFIX}/recurring-invoices", api_key, params=params or None)
        return self._apply(data, api_key, include_all_fields, "recurring_invoice")

    async def get_recurring_invoice_by_id(self, ri_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/recurring-invoices/{ri_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "recurring_invoice")

    async def create_recurring_invoice(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/recurring-invoices", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "recurring_invoice")

    async def update_recurring_invoice(self, ri_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/recurring-invoices/{ri_id}", api_key, json=await self._normalize_payload(payload, api_key))
        return self._apply(data, api_key, include_all_fields, "recurring_invoice")

    async def delete_recurring_invoices(self, ids: list[int], api_key: Optional[str] = None) -> Any:
        return await self.post(f"{PREFIX}/recurring-invoices/delete", api_key, json={"ids": ids})

    # =========================================================================
    # Roles
    # =========================================================================

    async def list_all_roles(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/roles", api_key)
        return self._apply(data, api_key, include_all_fields, "role")

    async def get_role_by_id(self, role_id: int, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/roles/{role_id}", api_key)
        return self._apply(data, api_key, include_all_fields, "role")

    async def create_role(self, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.post(f"{PREFIX}/roles", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "role")

    async def update_role(self, role_id: int, payload: dict[str, Any], api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.put(f"{PREFIX}/roles/{role_id}", api_key, json=payload)
        return self._apply(data, api_key, include_all_fields, "role")

    async def delete_role_by_id(self, role_id: int, api_key: Optional[str] = None) -> Any:
        return await self.delete(f"{PREFIX}/roles/{role_id}", api_key)

    # =========================================================================
    # Domain / Read Tools
    # =========================================================================

    async def check_server_status(self, api_key: Optional[str] = None) -> Any:
        ping = await self.get("/api/ping", api_key)
        try:
            ver = await self.get(f"{PREFIX}/app/version", api_key)
            return {**ping, **ver}
        except Exception:
            return ping

    async def get_dashboard(self, api_key: Optional[str] = None, previous_year: bool = False) -> Any:
        params = {"previous_year": "true" if previous_year else "false"}
        data = await self.get(f"{PREFIX}/dashboard", api_key, params=params)
        return self._unwrap(data)

    async def get_bootstrap(self, api_key: Optional[str] = None) -> Any:
        data = await self.get(f"{PREFIX}/bootstrap", api_key)
        return self._unwrap(data)

    async def search_customers_and_users(self, api_key: Optional[str] = None, search: str = "") -> Any:
        params = {"search": search} if search else None
        data = await self.get(f"{PREFIX}/search", api_key, params=params)
        return self._unwrap(data)

    async def search_users(self, api_key: Optional[str] = None, email: str = "") -> Any:
        params = {"email": email} if email else None
        data = await self.get(f"{PREFIX}/search/user", api_key, params=params)
        return self._unwrap(data)

    async def list_all_currencies(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/currencies", api_key)
        result = self._unwrap(data)
        if not include_all_fields:
            result = _filter_fields(result, COMMON_FIELDS.get("currency", {"id", "name", "code"}))
        return _denormalize_response(result)

    async def list_used_currencies(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/currencies/used", api_key)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict) and isinstance(result.get("currencies"), list):
            result["currencies"] = _filter_fields(result["currencies"], {"id", "name", "code"})
        return result

    async def list_all_countries(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/countries", api_key)
        result = self._unwrap(data)
        if not include_all_fields:
            result = _filter_fields(result, {"id", "name", "code"})
        return _denormalize_response(result)

    async def list_timezones(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/timezones", api_key)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict) and isinstance(result.get("time_zones"), list):
            result["time_zones"] = _filter_fields(result["time_zones"], COMMON_FIELDS["timezone"])
        return result

    async def list_date_formats(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/date/formats", api_key)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict) and isinstance(result.get("date_formats"), list):
            result["date_formats"] = _filter_fields(result["date_formats"], COMMON_FIELDS["date_format"])
        return result

    async def list_time_formats(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/time/formats", api_key)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict) and isinstance(result.get("time_formats"), list):
            result["time_formats"] = _filter_fields(result["time_formats"], COMMON_FIELDS["time_format"])
        return result

    async def get_next_number(self, api_key: Optional[str] = None, key: str = "", user_id: str = "", model_id: str = "") -> Any:
        params = {"key": key}
        if user_id:
            params["user_id"] = user_id
        if model_id:
            params["model_id"] = model_id
        data = await self.get(f"{PREFIX}/next-number", api_key, params=params)
        return self._unwrap(data)

    async def get_number_placeholders(self, api_key: Optional[str] = None, format_str: str = "") -> Any:
        params = {"format": format_str} if format_str else None
        data = await self.get(f"{PREFIX}/number-placeholders", api_key, params=params)
        return self._unwrap(data)

    async def get_current_company(self, api_key: Optional[str] = None) -> Any:
        data = await self.get(f"{PREFIX}/current-company", api_key)
        return self._unwrap(data)

    async def get_company_roles_by_id(self, company_id: int, api_key: Optional[str] = None) -> Any:
        data = await self.get(f"{PREFIX}/companies", api_key, keep_roles=True, convert_money=False)
        companies = self._unwrap(data)
        for company in companies if isinstance(companies, list) else []:
            if isinstance(company, dict) and str(company.get("id")) == str(company_id):
                return {"id": company_id, "roles": company.get("roles", [])}
        return {"id": company_id, "roles": [], "error": "company not found"}

    async def get_current_user_roles(self, api_key: Optional[str] = None) -> Any:
        data = await self.get(f"{PREFIX}/bootstrap", api_key, keep_roles=True, convert_money=False)
        unwrapped = self._unwrap(data)
        current_user = unwrapped.get("current_user", {}) if isinstance(unwrapped, dict) else {}
        if not isinstance(current_user, dict):
            current_user = {}
        return {"roles": current_user.get("roles", [])}

    async def list_all_companies(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/companies", api_key)
        result = self._unwrap(data)
        if not include_all_fields:
            result = _filter_fields(result, COMMON_FIELDS["company"])
        return _denormalize_response(result)

    async def list_abilities(self, api_key: Optional[str] = None, include_all_fields: bool = False) -> Any:
        data = await self.get(f"{PREFIX}/abilities", api_key)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict) and isinstance(result.get("abilities"), list):
            result["abilities"] = _filter_fields(result["abilities"], COMMON_FIELDS["ability"])
        return result

    async def get_recurring_invoice_frequency(self, api_key: Optional[str] = None, frequency: str = "", starts_at: str = "") -> Any:
        params = {"frequency": frequency, "starts_at": _normalize_datetime(starts_at) if starts_at else starts_at}
        data = await self.get(f"{PREFIX}/recurring-invoice-frequency", api_key, params=params)
        return self._unwrap(data)

    async def get_exchange_rate(self, currency_id: int, api_key: Optional[str] = None) -> Any:
        data = await self.get(f"{PREFIX}/currencies/{currency_id}/exchange-rate", api_key)
        return self._unwrap(data)

    async def get_active_exchange_rate_provider(self, currency_id: int, api_key: Optional[str] = None) -> Any:
        data = await self.get(f"{PREFIX}/currencies/{currency_id}/active-provider", api_key)
        return self._unwrap(data)

    async def list_used_currencies_for_exchange(self, api_key: Optional[str] = None, include_all_fields: bool = False, provider_id: str = "") -> Any:
        params = {"provider_id": provider_id} if provider_id else None
        data = await self.get(f"{PREFIX}/used-currencies", api_key, params=params)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, dict):
            common = {"id", "name", "code"}
            for k in ("allUsedCurrencies", "activeUsedCurrencies"):
                if isinstance(result.get(k), list):
                    result[k] = _filter_fields(result[k], common)
        return result

    async def list_supported_currencies(self, api_key: Optional[str] = None, include_all_fields: bool = False, driver: str = "", key: str = "") -> Any:
        params = {"driver": driver, "key": key}
        data = await self.get(f"{PREFIX}/supported-currencies", api_key, params=params)
        result = self._unwrap(data)
        if not include_all_fields and isinstance(result, list):
            result = _filter_fields(result, {"id", "name", "code"})
        return result
