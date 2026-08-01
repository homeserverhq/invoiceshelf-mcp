import json
import os
import sys
from contextvars import ContextVar
from typing import Any, Optional

from fastmcp import FastMCP, Context
from pydantic import BaseModel, Field
from toon_mcp import json_to_toon

from .client import InvoiceShelfClient

_current_user_token: ContextVar[Optional[str]] = ContextVar("current_user_token", default=None)


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            if auth_header.startswith("Bearer "):
                _current_user_token.set(auth_header[7:])
        await self.app(scope, receive, send)


mcp = FastMCP("InvoiceShelf-mcp-server")

_client: Optional[InvoiceShelfClient] = None


def get_client() -> InvoiceShelfClient:
    global _client
    if _client is None:
        _client = InvoiceShelfClient()
    return _client


def get_user_token() -> Optional[str]:
    return _current_user_token.get()


ALLOW_ALL_AGGREGATE = os.getenv("ALLOW_ALL_AGGREGATE", "false").lower() in ("true", "1", "yes")
IS_STATEFUL = os.getenv("IS_STATEFUL", "false").lower() in ("true", "1", "yes")


def _ensure_payload(payload: dict, defaults: dict) -> dict:
    for k, v in defaults.items():
        payload.setdefault(k, v)
    return payload


# =============================================================================
# Pydantic Contract Models
# =============================================================================

class InvoiceLineItem(BaseModel):
    name: str = Field(description="Line item name")
    quantity: float = Field(description="Quantity (numeric)")
    price: float = Field(description="Unit price (numeric)")
    description: str = Field(default="", description="Line item description (optional)")

class InvoiceLineItems(BaseModel):
    items: list[InvoiceLineItem] = Field(default_factory=list, description="List of invoice line items")

class TaxItem(BaseModel):
    tax_type_id: int = Field(description="ID of the tax type")
    name: str = Field(description="Tax name")
    percent: float = Field(description="Tax percentage")
    amount: float = Field(description="Tax amount")

class TaxesParam(BaseModel):
    taxes: list[TaxItem] = Field(default_factory=list, description="List of taxes applied")

class CustomFieldItem(BaseModel):
    custom_field_id: int = Field(description="ID of the custom field")
    value: str = Field(description="Value for the custom field")

class CustomFieldsParam(BaseModel):
    customFields: list[CustomFieldItem] = Field(default_factory=list, description="List of custom field values")

class CustomerAddress(BaseModel):
    name: str = Field(default="", description="Address name")
    address_street_1: str = Field(default="", description="Street address line 1")
    address_street_2: str = Field(default="", description="Street address line 2")
    city: str = Field(default="", description="City")
    state: str = Field(default="", description="State/Province")
    country_id: str = Field(default="", description="Country ID")
    zip: str = Field(default="", description="ZIP/Postal code")
    phone: str = Field(default="", description="Phone number")
    fax: str = Field(default="", description="Fax number")

# =============================================================================
# Domain Tools (21)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def check_server_status(ctx: Context = None) -> dict[str, Any]:
    """Check connectivity to the InvoiceShelf backend."""
    return await get_client().check_server_status(get_user_token())

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_dashboard(previous_year: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get dashboard analytics data.

    Args:
        previous_year: Compare with previous year data when true. Default false.
    """
    return await get_client().get_dashboard(get_user_token(), previous_year=previous_year)

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_bootstrap(ctx: Context = None) -> dict[str, Any]:
    """Get app bootstrap data including company currency info."""
    return await get_client().get_bootstrap(get_user_token())

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def search_customers_and_users(search: str = "", ctx: Context = None) -> dict[str, Any]:
    """Search customers and users.

    Args:
        search: Search keyword to filter customers and users.
    """
    return await get_client().search_customers_and_users(get_user_token(), search=search)

@mcp.tool(tags={"read", "advanced", "invoiceshelf"})
async def search_users(email: str = "", ctx: Context = None) -> dict[str, Any]:
    """Search users by email.

    Args:
        email: Email address to search for.
    """
    return await get_client().search_users(get_user_token(), email=email)

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_currencies(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all currencies.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_currencies(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def list_used_currencies(ctx: Context = None) -> dict[str, Any]:
    """List currencies that have been used in transactions."""
    return await get_client().list_used_currencies(get_user_token())

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_countries(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all countries.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_countries(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_timezones(ctx: Context = None) -> dict[str, Any]:
    """List all timezones."""
    return await get_client().list_timezones(get_user_token())

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_date_formats(ctx: Context = None) -> dict[str, Any]:
    """List available date formats."""
    return await get_client().list_date_formats(get_user_token())

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_time_formats(ctx: Context = None) -> dict[str, Any]:
    """List available time formats."""
    return await get_client().list_time_formats(get_user_token())

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def get_next_number(key: str, user_id: str = "", model_id: str = "", ctx: Context = None) -> dict[str, Any]:
    """Get the next number for invoice, estimate, or payment.

    Args:
        key: invoice, estimate, or payment.
        user_id: Optional user ID for scoping.
        model_id: Optional model ID for scoping.
    """
    return await get_client().get_next_number(get_user_token(), key=key, user_id=user_id, model_id=model_id)

@mcp.tool(tags={"read", "advanced", "invoiceshelf"})
async def get_number_placeholders(format: str = "", ctx: Context = None) -> dict[str, Any]:
    """Get number format placeholders.

    Args:
        format: Format string to check (e.g. INV-{NUMBER}).
    """
    return await get_client().get_number_placeholders(get_user_token(), format_str=format)

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_current_company(ctx: Context = None) -> dict[str, Any]:
    """Get current company information."""
    return await get_client().get_current_company(get_user_token())

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def list_all_companies(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all companies the current user belongs to.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_companies(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def list_abilities(ctx: Context = None) -> dict[str, Any]:
    """List all available role abilities."""
    return await get_client().list_abilities(get_user_token())

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def get_recurring_invoice_frequency(frequency: str, starts_at: str, ctx: Context = None) -> dict[str, Any]:
    """Get recurrence dates for a recurring invoice frequency.

    Args:
        frequency: Cron expression for frequency (e.g. 0 0 1 * *).
        starts_at: ISO 8601 format (2026-06-22T15:00:00-04:00).
    """
    return await get_client().get_recurring_invoice_frequency(get_user_token(), frequency=frequency, starts_at=starts_at)

@mcp.tool(tags={"read", "advanced", "invoiceshelf"})
async def get_exchange_rate(currency_id: int, ctx: Context = None) -> dict[str, Any]:
    """Get exchange rate for a currency.

    Args:
        currency_id: ID of the currency to get the exchange rate for.
    """
    return await get_client().get_exchange_rate(currency_id, get_user_token())

@mcp.tool(tags={"read", "advanced", "invoiceshelf"})
async def get_active_exchange_rate_provider(currency_id: int, ctx: Context = None) -> dict[str, Any]:
    """Get active exchange rate provider for a currency.

    Args:
        currency_id: ID of the currency to check.
    """
    return await get_client().get_active_exchange_rate_provider(currency_id, get_user_token())

@mcp.tool(tags={"read", "advanced", "invoiceshelf"})
async def list_used_currencies_for_exchange(provider_id: str = "", ctx: Context = None) -> dict[str, Any]:
    """List currencies used for exchange rate lookups.

    Args:
        provider_id: Optional exchange rate provider ID to filter by.
    """
    return await get_client().list_used_currencies_for_exchange(get_user_token(), provider_id=provider_id)

@mcp.tool(tags={"read", "advanced", "invoiceshelf"})
async def list_supported_currencies(driver: str, key: str, ctx: Context = None) -> dict[str, Any]:
    """List supported currencies for an exchange rate driver.

    Args:
        driver: Exchange rate driver name (e.g. currency_freak).
        key: API key for the exchange rate driver.
    """
    return await get_client().list_supported_currencies(get_user_token(), driver=driver, key=key)

# =============================================================================
# Customers (6 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_customers(include_all_fields: bool = False, page: int = 0, page_size: int = 10, ctx: Context = None) -> dict[str, Any]:
    """List all customer records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
        page: Page number for pagination (0 for default).
        page_size: Number of records per page (0 for all).
    """
    data = await get_client().list_all_customers(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False, page=page, page_size=page_size)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_customer_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single customer by ID.

    Args:
        id: The unique ID of the customer.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_customer_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_customer(name: str, email: str, password: str = "", phone: str = "", company_name: str = "", contact_name: str = "", website: str = "", prefix: str = "", tax_id: str = "", enable_portal: bool = False, currency_id: str = "", billing: CustomerAddress = None, shipping: CustomerAddress = None, ctx: Context = None) -> dict[str, Any]:
    """Create a new customer.

    Args:
        name: Name of the customer.
        email: Email address of the customer.
        password: Portal access password.
        phone: Phone number.
        company_name: Company name.
        contact_name: Contact person name.
        website: Website URL.
        prefix: Customer number prefix.
        tax_id: Tax ID / VAT number.
        enable_portal: Enable customer portal access. Default false.
        currency_id: Currency ID.
        billing: CustomerAddress object for billing address.
        shipping: CustomerAddress object for shipping address.
    """
    payload = {"name": name, "email": email}
    if password:
        payload["password"] = password
    for k, v in [("phone", phone), ("company_name", company_name), ("contact_name", contact_name), ("website", website), ("prefix", prefix), ("tax_id", tax_id), ("enable_portal", enable_portal), ("currency_id", currency_id)]:
        if v:
            payload[k] = v
    if billing:
        payload["billing"] = billing.model_dump(exclude_unset=True, exclude_none=True)
    if shipping:
        payload["shipping"] = shipping.model_dump(exclude_unset=True, exclude_none=True)
    return await get_client().create_customer(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_customer(id: int, name: str = None, email: str = None, password: str = None, phone: str = None, company_name: str = None, contact_name: str = None, website: str = None, prefix: str = None, tax_id: str = None, enable_portal: bool = None, currency_id: str = None, billing: CustomerAddress = None, shipping: CustomerAddress = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing customer.

    Args:
        id: The unique ID of the customer to update.
        name: Updated name.
        email: Updated email address.
        password: Updated portal password.
        phone: Updated phone number.
        company_name: Updated company name.
        contact_name: Updated contact name.
        website: Updated website URL.
        prefix: Updated prefix.
        tax_id: Updated tax ID.
        enable_portal: Enable or disable portal access.
        currency_id: Updated currency ID.
        billing: Updated billing address (CustomerAddress).
        shipping: Updated shipping address (CustomerAddress).
    """
    payload = {"id": id}
    for k, v in [("name", name), ("email", email), ("password", password), ("phone", phone), ("company_name", company_name), ("contact_name", contact_name), ("website", website), ("prefix", prefix), ("tax_id", tax_id), ("currency_id", currency_id)]:
        if v is not None:
            payload[k] = v
    if enable_portal is not None:
        payload["enable_portal"] = enable_portal
    if billing:
        payload["billing"] = billing.model_dump(exclude_unset=True, exclude_none=True)
    if shipping:
        payload["shipping"] = shipping.model_dump(exclude_unset=True, exclude_none=True)
    return await get_client().update_customer(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_customers_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a customer by ID.

    Args:
        id: The unique ID of the customer to delete.
    """
    await get_client().delete_customers([id], get_user_token())
    return {"deleted": True, "id": id}

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def get_customer_stats(id: int, previous_year: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get customer statistics.

    Args:
        id: The unique ID of the customer.
        previous_year: Compare with previous year data. Default false.
    """
    return await get_client().get_customer_stats(id, get_user_token(), previous_year=previous_year)

# =============================================================================
# Items (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_items(include_all_fields: bool = False, page: int = 0, page_size: int = 10, ctx: Context = None) -> dict[str, Any]:
    """List all item records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
        page: Page number (0 for default).
        page_size: Records per page (0 for all).
    """
    data = await get_client().list_all_items(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False, page=page, page_size=page_size)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_item_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single item by ID.

    Args:
        id: The unique ID of the item.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_item_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_item(name: str, price: float, unit_id: str = "", description: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new item.

    Args:
        name: Name of the item.
        price: Price of the item.
        unit_id: ID of the unit for this item.
        description: Description of the item.
    """
    payload = _ensure_payload({"name": name, "price": price}, {})
    if unit_id:
        payload["unit_id"] = unit_id
    if description:
        payload["description"] = description
    return await get_client().create_item(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_item(id: int, name: str = None, price: float = None, unit_id: str = None, description: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing item.

    Args:
        id: The unique ID of the item to update.
        name: Updated name.
        price: Updated price.
        unit_id: Updated unit ID.
        description: Updated description.
    """
    payload = {"id": id}
    for k, v in [("name", name), ("price", price), ("unit_id", unit_id), ("description", description)]:
        if v is not None:
            payload[k] = v
    return await get_client().update_item(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_items_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete an item by ID.

    Args:
        id: The unique ID of the item to delete.
    """
    await get_client().delete_items([id], get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Units (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_units(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all unit records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_units(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_unit_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single unit by ID.

    Args:
        id: The unique ID of the unit.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_unit_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_unit(name: str, ctx: Context = None) -> dict[str, Any]:
    """Create a new unit.

    Args:
        name: Name of the unit.
    """
    return await get_client().create_unit({"name": name}, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_unit(id: int, name: str, ctx: Context = None) -> dict[str, Any]:
    """Update an existing unit.

    Args:
        id: The unique ID of the unit to update.
        name: Updated name.
    """
    return await get_client().update_unit(id, {"name": name}, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_unit_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a unit by ID.

    Args:
        id: The unique ID of the unit to delete.
    """
    await get_client().delete_unit_by_id(id, get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Invoices (10 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_invoices(include_all_fields: bool = False, page: int = 0, page_size: int = 10, ctx: Context = None) -> dict[str, Any]:
    """List all invoice records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
        page: Page number (0 for default).
        page_size: Records per page (0 for all).
    """
    data = await get_client().list_all_invoices(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False, page=page, page_size=page_size)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_invoice_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single invoice by ID.

    Args:
        id: The unique ID of the invoice.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_invoice_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_invoice(customer_id: int, invoice_number: str, invoice_date: str, template_name: str, items: InvoiceLineItems, due_date: str = "", discount: float = 0, discount_val: int = 0, sub_total: float = 0, total: float = 0, tax: float = 0, exchange_rate: str = "", notes: str = "", taxes: TaxesParam = None, custom_fields: CustomFieldsParam = None, discount_type: str = "", tax_included: str = "", currency_id: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new invoice.

    Args:
        customer_id: ID of the customer.
        invoice_number: Unique invoice number.
        invoice_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        template_name: Invoice template name (e.g. invoice1).
        items: InvoiceLineItems object with list of line items.
        due_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        discount: Discount amount. Default 0.
        discount_val: Discount value integer. Default 0.
        sub_total: Sub total. Default 0 (recomputed server-side).
        total: Total. Default 0 (recomputed server-side).
        tax: Tax amount. Default 0 (recomputed server-side).
        exchange_rate: Exchange rate.
        notes: Invoice notes.
        taxes: TaxesParam object with list of taxes.
        custom_fields: CustomFieldsParam object with list of custom field values.
        discount_type: Discount type.
        tax_included: Whether tax is included.
        currency_id: Currency ID.
    """
    payload = _ensure_payload({
        "customer_id": customer_id, "invoice_number": invoice_number, "invoice_date": invoice_date,
        "template_name": template_name, "discount": discount, "discount_val": discount_val,
        "sub_total": sub_total, "total": total, "tax": tax,
    }, {"exchange_rate": "1"})
    items_list = items.model_dump(exclude_unset=True)["items"] if isinstance(items, InvoiceLineItems) else (items or [])
    for it in items_list:
        it.setdefault("discount_val", 0)
        it.setdefault("discount_type", "none")
        it.setdefault("tax", 0)
    payload["items"] = items_list
    for k, v in [("due_date", due_date), ("exchange_rate", exchange_rate), ("notes", notes), ("discount_type", discount_type), ("tax_included", tax_included), ("currency_id", currency_id)]:
        if v:
            payload[k] = v
    if taxes:
        payload["taxes"] = taxes.model_dump(exclude_unset=True).get("taxes", [])
    if custom_fields:
        payload["customFields"] = custom_fields.model_dump(exclude_unset=True).get("customFields", [])
    return await get_client().create_invoice(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_invoice(id: int, customer_id: int = None, invoice_number: str = None, invoice_date: str = None, template_name: str = None, items: InvoiceLineItems = None, due_date: str = None, discount: float = None, discount_val: int = None, sub_total: float = None, total: float = None, tax: float = None, exchange_rate: str = None, notes: str = None, taxes: TaxesParam = None, custom_fields: CustomFieldsParam = None, discount_type: str = None, tax_included: str = None, currency_id: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing invoice.

    Args:
        id: The unique ID of the invoice to update.
        customer_id: Updated customer ID.
        invoice_number: Updated invoice number.
        invoice_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        template_name: Updated template name.
        items: Updated line items (InvoiceLineItems).
        due_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        discount: Updated discount amount.
        discount_val: Updated discount value.
        sub_total: Updated sub total.
        total: Updated total.
        tax: Updated tax.
        exchange_rate: Updated exchange rate.
        notes: Updated notes.
        taxes: Updated taxes (TaxesParam).
        custom_fields: Updated custom fields (CustomFieldsParam).
        discount_type: Updated discount type.
        tax_included: Updated tax included flag.
        currency_id: Updated currency ID.
    """
    current = await get_client().get_invoice_by_id(id, get_user_token(), include_all_fields=True)
    payload = {}
    for k, v in [("customer_id", customer_id), ("invoice_number", invoice_number), ("invoice_date", invoice_date), ("template_name", template_name), ("due_date", due_date), ("discount", discount), ("discount_val", discount_val), ("sub_total", sub_total), ("total", total), ("tax", tax), ("exchange_rate", exchange_rate), ("notes", notes), ("discount_type", discount_type), ("tax_included", tax_included), ("currency_id", currency_id)]:
        if v is not None:
            payload[k] = v
    if items:
        items_list = items.model_dump(exclude_unset=True)["items"]
        for it in items_list:
            it.setdefault("discount_val", 0)
            it.setdefault("discount_type", "none")
            it.setdefault("tax", 0)
        payload["items"] = items_list
    if taxes:
        payload["taxes"] = taxes.model_dump(exclude_unset=True).get("taxes", [])
    if custom_fields:
        payload["customFields"] = custom_fields.model_dump(exclude_unset=True).get("customFields", [])
    if isinstance(current, dict):
        for k in ("customer_id", "invoice_number", "invoice_date", "template_name", "discount", "discount_val", "sub_total", "total", "tax"):
            if k not in payload:
                v = current.get(k)
                if v is not None:
                    payload[k] = v
        if "items" not in payload and current.get("items"):
            items_fallback = []
            for i in current["items"]:
                if isinstance(i, dict):
                    item = {"name": i["name"], "quantity": float(i.get("quantity", 1)), "price": float(i.get("price", 0)), "description": i.get("description", "")}
                    item.setdefault("discount_val", 0)
                    item.setdefault("discount_type", "none")
                    item.setdefault("tax", 0)
                    items_fallback.append(item)
            payload["items"] = items_fallback
    return await get_client().update_invoice(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_invoices_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete an invoice by ID.

    Args:
        id: The unique ID of the invoice to delete.
    """
    await get_client().delete_invoices([id], get_user_token())
    return {"deleted": True, "id": id}

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def send_invoice(id: int, to: str, from_: str, subject: str, body: str, cc: str = "", bcc: str = "", ctx: Context = None) -> dict[str, Any]:
    """Send an invoice via email.

    Args:
        id: The unique ID of the invoice to send.
        to: Recipient email address.
        from_: Sender email address.
        subject: Email subject.
        body: Email body.
        cc: CC recipient.
        bcc: BCC recipient.
    """
    payload = {"to": to, "from": from_, "subject": subject, "body": body}
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    return await get_client().send_invoice(id, payload, get_user_token())

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def clone_invoice(id: int, ctx: Context = None) -> dict[str, Any]:
    """Clone an invoice into a new draft.

    Args:
        id: The unique ID of the invoice to clone.
    """
    return await get_client().clone_invoice(id, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def change_invoice_status(id: int, status: str, ctx: Context = None) -> dict[str, Any]:
    """Change invoice status.

    Args:
        id: The unique ID of the invoice.
        status: SENT or COMPLETED.
    """
    return await get_client().change_invoice_status(id, {"status": status}, get_user_token())

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_invoice_templates(ctx: Context = None) -> dict[str, Any]:
    """List available invoice templates."""
    return await get_client().list_invoice_templates(get_user_token())

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def get_invoice_send_preview(id: int, to: str, from_: str, subject: str, body: str, cc: str = "", bcc: str = "", ctx: Context = None) -> dict[str, Any]:
    """Get invoice email preview HTML.

    Args:
        id: The unique ID of the invoice.
        to: Recipient email address.
        from_: Sender email address.
        subject: Email subject.
        body: Email body.
        cc: CC recipient.
        bcc: BCC recipient.
    """
    payload = {"to": to, "from": from_, "subject": subject, "body": body}
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    return await get_client().get_invoice_send_preview(id, payload, get_user_token())

# =============================================================================
# Estimates (11 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_estimates(include_all_fields: bool = False, page: int = 0, page_size: int = 10, ctx: Context = None) -> dict[str, Any]:
    """List all estimate records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
        page: Page number (0 for default).
        page_size: Records per page (0 for all).
    """
    data = await get_client().list_all_estimates(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False, page=page, page_size=page_size)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_estimate_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single estimate by ID.

    Args:
        id: The unique ID of the estimate.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_estimate_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_estimate(customer_id: int, estimate_number: str, estimate_date: str, template_name: str, items: InvoiceLineItems, expiry_date: str = "", discount: float = 0, discount_val: int = 0, sub_total: float = 0, total: float = 0, tax: float = 0, exchange_rate: str = "", notes: str = "", taxes: TaxesParam = None, custom_fields: CustomFieldsParam = None, discount_type: str = "", tax_included: str = "", currency_id: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new estimate.

    Args:
        customer_id: ID of the customer.
        estimate_number: Unique estimate number.
        estimate_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        template_name: Estimate template name (e.g. estimate1).
        items: InvoiceLineItems with list of line items.
        expiry_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        discount: Discount amount. Default 0.
        discount_val: Discount value. Default 0.
        sub_total: Sub total (recomputed server-side). Default 0.
        total: Total (recomputed server-side). Default 0.
        tax: Tax amount (recomputed server-side). Default 0.
        exchange_rate: Exchange rate.
        notes: Estimate notes.
        taxes: TaxesParam object.
        custom_fields: CustomFieldsParam object.
        discount_type: Discount type.
        tax_included: Whether tax is included.
        currency_id: Currency ID.
    """
    payload = _ensure_payload({
        "customer_id": customer_id, "estimate_number": estimate_number, "estimate_date": estimate_date,
        "template_name": template_name, "discount": discount, "discount_val": discount_val,
        "sub_total": sub_total, "total": total, "tax": tax,
    }, {"exchange_rate": "1"})
    items_list = items.model_dump(exclude_unset=True)["items"] if isinstance(items, InvoiceLineItems) else (items or [])
    for it in items_list:
        it.setdefault("discount_val", 0)
        it.setdefault("discount_type", "none")
        it.setdefault("tax", 0)
    payload["items"] = items_list
    for k, v in [("expiry_date", expiry_date), ("exchange_rate", exchange_rate), ("notes", notes), ("discount_type", discount_type), ("tax_included", tax_included), ("currency_id", currency_id)]:
        if v:
            payload[k] = v
    if taxes:
        payload["taxes"] = taxes.model_dump(exclude_unset=True).get("taxes", [])
    if custom_fields:
        payload["customFields"] = custom_fields.model_dump(exclude_unset=True).get("customFields", [])
    return await get_client().create_estimate(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_estimate(id: int, customer_id: int = None, estimate_number: str = None, estimate_date: str = None, template_name: str = None, items: InvoiceLineItems = None, expiry_date: str = None, discount: float = None, discount_val: int = None, sub_total: float = None, total: float = None, tax: float = None, exchange_rate: str = None, notes: str = None, taxes: TaxesParam = None, custom_fields: CustomFieldsParam = None, discount_type: str = None, tax_included: str = None, currency_id: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing estimate.

    Args:
        id: The unique ID of the estimate to update.
        customer_id: Updated customer ID.
        estimate_number: Updated estimate number.
        estimate_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        template_name: Updated template name.
        items: Updated line items (InvoiceLineItems).
        expiry_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        discount: Updated discount.
        discount_val: Updated discount value.
        sub_total: Updated sub total.
        total: Updated total.
        tax: Updated tax.
        exchange_rate: Updated exchange rate.
        notes: Updated notes.
        taxes: Updated taxes (TaxesParam).
        custom_fields: Updated custom fields (CustomFieldsParam).
        discount_type: Updated discount type.
        tax_included: Updated tax included flag.
        currency_id: Updated currency ID.
    """
    current = await get_client().get_estimate_by_id(id, get_user_token(), include_all_fields=True)
    payload = {}
    for k, v in [("customer_id", customer_id), ("estimate_number", estimate_number), ("estimate_date", estimate_date), ("template_name", template_name), ("expiry_date", expiry_date), ("discount", discount), ("discount_val", discount_val), ("sub_total", sub_total), ("total", total), ("tax", tax), ("exchange_rate", exchange_rate), ("notes", notes), ("discount_type", discount_type), ("tax_included", tax_included), ("currency_id", currency_id)]:
        if v is not None:
            payload[k] = v
    if items:
        items_list = items.model_dump(exclude_unset=True)["items"]
        for it in items_list:
            it.setdefault("discount_val", 0)
            it.setdefault("discount_type", "none")
            it.setdefault("tax", 0)
        payload["items"] = items_list
    if taxes:
        payload["taxes"] = taxes.model_dump(exclude_unset=True).get("taxes", [])
    if custom_fields:
        payload["customFields"] = custom_fields.model_dump(exclude_unset=True).get("customFields", [])
    if isinstance(current, dict):
        for k in ("customer_id", "estimate_number", "estimate_date", "template_name", "discount", "discount_val", "sub_total", "total", "tax"):
            if k not in payload:
                v = current.get(k)
                if v is not None:
                    payload[k] = v
        if "items" not in payload and current.get("items"):
            items_fallback = []
            for i in current["items"]:
                if isinstance(i, dict):
                    item = {"name": i["name"], "quantity": float(i.get("quantity", 1)), "price": float(i.get("price", 0)), "description": i.get("description", "")}
                    item.setdefault("discount_val", 0)
                    item.setdefault("discount_type", "none")
                    item.setdefault("tax", 0)
                    items_fallback.append(item)
            payload["items"] = items_fallback
    return await get_client().update_estimate(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_estimates_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete an estimate by ID.

    Args:
        id: The unique ID of the estimate to delete.
    """
    await get_client().delete_estimates([id], get_user_token())
    return {"deleted": True, "id": id}

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def send_estimate(id: int, to: str, from_: str, subject: str, body: str, cc: str = "", bcc: str = "", ctx: Context = None) -> dict[str, Any]:
    """Send an estimate via email.

    Args:
        id: The unique ID of the estimate to send.
        to: Recipient email address.
        from_: Sender email address.
        subject: Email subject.
        body: Email body.
        cc: CC recipient.
        bcc: BCC recipient.
    """
    payload = {"to": to, "from": from_, "subject": subject, "body": body}
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    return await get_client().send_estimate(id, payload, get_user_token())

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def clone_estimate(id: int, ctx: Context = None) -> dict[str, Any]:
    """Clone an estimate into a new draft.

    Args:
        id: The unique ID of the estimate to clone.
    """
    return await get_client().clone_estimate(id, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def change_estimate_status(id: int, status: str, ctx: Context = None) -> dict[str, Any]:
    """Change estimate status.

    Args:
        id: The unique ID of the estimate.
        status: SENT, ACCEPTED, or REJECTED.
    """
    return await get_client().change_estimate_status(id, {"status": status}, get_user_token())

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def convert_estimate_to_invoice(id: int, ctx: Context = None) -> dict[str, Any]:
    """Convert an estimate to an invoice.

    Args:
        id: The unique ID of the estimate to convert.
    """
    return await get_client().convert_estimate_to_invoice(id, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_estimate_templates(ctx: Context = None) -> dict[str, Any]:
    """List available estimate templates."""
    return await get_client().list_estimate_templates(get_user_token())

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def get_estimate_send_preview(id: int, to: str, from_: str, subject: str, body: str, cc: str = "", bcc: str = "", ctx: Context = None) -> dict[str, Any]:
    """Get estimate email preview HTML.

    Args:
        id: The unique ID of the estimate.
        to: Recipient email address.
        from_: Sender email address.
        subject: Email subject.
        body: Email body.
        cc: CC recipient.
        bcc: BCC recipient.
    """
    payload = {"to": to, "from": from_, "subject": subject, "body": body}
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    return await get_client().get_estimate_send_preview(id, payload, get_user_token())

# =============================================================================
# Expenses (6 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_expenses(include_all_fields: bool = False, page: int = 0, page_size: int = 10, ctx: Context = None) -> dict[str, Any]:
    """List all expense records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
        page: Page number (0 for default).
        page_size: Records per page (0 for all).
    """
    data = await get_client().list_all_expenses(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False, page=page, page_size=page_size)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_expense_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single expense by ID.

    Args:
        id: The unique ID of the expense.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_expense_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_expense(expense_date: str, expense_category_id: int, amount: float, currency_id: int, expense_number: str = "", payment_method_id: str = "", customer_id: str = "", notes: str = "", exchange_rate: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new expense.

    Args:
        expense_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        expense_category_id: ID of the expense category.
        amount: Expense amount.
        currency_id: ID of the currency.
        expense_number: Expense number.
        payment_method_id: ID of the payment method.
        customer_id: ID of the customer.
        notes: Expense notes.
        exchange_rate: Exchange rate.
    """
    payload = _ensure_payload({"expense_date": expense_date, "expense_category_id": expense_category_id, "amount": amount, "currency_id": currency_id}, {"exchange_rate": "1"})
    for k, v in [("expense_number", expense_number), ("payment_method_id", payment_method_id), ("customer_id", customer_id), ("notes", notes), ("exchange_rate", exchange_rate)]:
        if v:
            payload[k] = v
    return await get_client().create_expense(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_expense(id: int, expense_date: str = None, expense_category_id: int = None, amount: float = None, currency_id: int = None, expense_number: str = None, payment_method_id: str = None, customer_id: str = None, notes: str = None, exchange_rate: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing expense.

    Args:
        id: The unique ID of the expense to update.
        expense_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        expense_category_id: Updated category ID.
        amount: Updated amount.
        currency_id: Updated currency ID.
        expense_number: Updated expense number.
        payment_method_id: Updated payment method ID.
        customer_id: Updated customer ID.
        notes: Updated notes.
        exchange_rate: Updated exchange rate.
    """
    payload = {}
    for k, v in [("expense_date", expense_date), ("expense_category_id", expense_category_id), ("amount", amount), ("currency_id", currency_id), ("expense_number", expense_number), ("payment_method_id", payment_method_id), ("customer_id", customer_id), ("notes", notes), ("exchange_rate", exchange_rate)]:
        if v is not None:
            payload[k] = v
    return await get_client().update_expense(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_expenses_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete an expense by ID.

    Args:
        id: The unique ID of the expense to delete.
    """
    await get_client().delete_expenses([id], get_user_token())
    return {"deleted": True, "id": id}

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def duplicate_expense(id: int, expense_date: str, ctx: Context = None) -> dict[str, Any]:
    """Duplicate an expense with a new date.

    Args:
        id: The unique ID of the expense to duplicate.
        expense_date: ISO 8601 format (2026-06-22T15:00:00-04:00) for the duplicate.
    """
    return await get_client().duplicate_expense(id, {"expense_date": expense_date}, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

# =============================================================================
# Expense Categories (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_expense_categories(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all expense categories.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_expense_categories(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_expense_category_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single expense category by ID.

    Args:
        id: The unique ID of the category.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_expense_category_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_expense_category(name: str, description: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new expense category.

    Args:
        name: Name of the category.
        description: Description of the category.
    """
    payload = {"name": name}
    if description:
        payload["description"] = description
    return await get_client().create_expense_category(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_expense_category(id: int, name: str = None, description: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing expense category.

    Args:
        id: The unique ID of the category to update.
        name: Updated name.
        description: Updated description.
    """
    payload = {}
    for k, v in [("name", name), ("description", description)]:
        if v is not None:
            payload[k] = v
    return await get_client().update_expense_category(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_expense_category_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete an expense category by ID.

    Args:
        id: The unique ID of the category to delete.
    """
    await get_client().delete_expense_category_by_id(id, get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Payments (7 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_payments(include_all_fields: bool = False, page: int = 0, page_size: int = 10, ctx: Context = None) -> dict[str, Any]:
    """List all payment records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
        page: Page number (0 for default).
        page_size: Records per page (0 for all).
    """
    data = await get_client().list_all_payments(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False, page=page, page_size=page_size)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_payment_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single payment by ID.

    Args:
        id: The unique ID of the payment.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_payment_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_payment(payment_date: str, customer_id: int, amount: float, payment_number: str, invoice_id: str = "", payment_method_id: str = "", notes: str = "", currency_id: str = "", exchange_rate: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new payment.

    Args:
        payment_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        customer_id: ID of the customer.
        amount: Payment amount.
        payment_number: Unique payment number.
        invoice_id: ID of the invoice to apply payment to.
        payment_method_id: ID of the payment method.
        notes: Payment notes.
        currency_id: Currency ID.
        exchange_rate: Exchange rate.
    """
    payload = _ensure_payload({"payment_date": payment_date, "customer_id": customer_id, "amount": amount, "payment_number": payment_number}, {})
    for k, v in [("invoice_id", invoice_id), ("payment_method_id", payment_method_id), ("notes", notes), ("currency_id", currency_id), ("exchange_rate", exchange_rate)]:
        if v:
            payload[k] = v
    return await get_client().create_payment(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_payment(id: int, payment_date: str = None, customer_id: int = None, amount: float = None, payment_number: str = None, invoice_id: str = None, payment_method_id: str = None, notes: str = None, currency_id: str = None, exchange_rate: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing payment.

    Args:
        id: The unique ID of the payment to update.
        payment_date: ISO 8601 format (2026-06-22T15:00:00-04:00).
        customer_id: Updated customer ID.
        amount: Updated amount.
        payment_number: Updated payment number.
        invoice_id: Updated invoice ID.
        payment_method_id: Updated payment method ID.
        notes: Updated notes.
        currency_id: Updated currency ID.
        exchange_rate: Updated exchange rate.
    """
    payload = {}
    for k, v in [("payment_date", payment_date), ("customer_id", customer_id), ("amount", amount), ("payment_number", payment_number), ("invoice_id", invoice_id), ("payment_method_id", payment_method_id), ("notes", notes), ("currency_id", currency_id), ("exchange_rate", exchange_rate)]:
        if v is not None:
            payload[k] = v
    return await get_client().update_payment(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_payments_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a payment by ID.

    Args:
        id: The unique ID of the payment to delete.
    """
    await get_client().delete_payments([id], get_user_token())
    return {"deleted": True, "id": id}

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def send_payment(id: int, to: str, from_: str, subject: str, body: str, cc: str = "", bcc: str = "", ctx: Context = None) -> dict[str, Any]:
    """Send a payment receipt via email.

    Args:
        id: The unique ID of the payment.
        to: Recipient email address.
        from_: Sender email address.
        subject: Email subject.
        body: Email body.
        cc: CC recipient.
        bcc: BCC recipient.
    """
    payload = {"to": to, "from": from_, "subject": subject, "body": body}
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    return await get_client().send_payment(id, payload, get_user_token())

@mcp.tool(tags={"read", "primary", "invoiceshelf"})
async def get_payment_send_preview(id: int, to: str, from_: str, subject: str, body: str, cc: str = "", bcc: str = "", ctx: Context = None) -> dict[str, Any]:
    """Get payment email preview HTML.

    Args:
        id: The unique ID of the payment.
        to: Recipient email address.
        from_: Sender email address.
        subject: Email subject.
        body: Email body.
        cc: CC recipient.
        bcc: BCC recipient.
    """
    payload = {"to": to, "from": from_, "subject": subject, "body": body}
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    return await get_client().get_payment_send_preview(id, payload, get_user_token())

# =============================================================================
# Payment Methods (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_payment_methods(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all payment method records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_payment_methods(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_payment_method_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single payment method by ID.

    Args:
        id: The unique ID of the payment method.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_payment_method_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_payment_method(name: str, ctx: Context = None) -> dict[str, Any]:
    """Create a new payment method.

    Args:
        name: Name of the payment method.
    """
    return await get_client().create_payment_method({"name": name}, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_payment_method(id: int, name: str, ctx: Context = None) -> dict[str, Any]:
    """Update an existing payment method.

    Args:
        id: The unique ID of the payment method to update.
        name: Updated name.
    """
    return await get_client().update_payment_method(id, {"name": name}, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_payment_method_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a payment method by ID.

    Args:
        id: The unique ID of the payment method to delete.
    """
    await get_client().delete_payment_method_by_id(id, get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Custom Fields (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_custom_fields(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all custom field records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_custom_fields(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_custom_field_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single custom field by ID.

    Args:
        id: The unique ID of the custom field.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_custom_field_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_custom_field(name: str, label: str, model_type: str, order: int, type: str, is_required: bool, options: str = "", placeholder: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new custom field.

    Args:
        name: Field name.
        label: Display label.
        model_type: App\Models\Customer, Invoice, Estimate, Expense, Payment, or Item.
        order: Display order index.
        type: INPUT, NUMBER, TEXT, SELECT, CHECKBOX, DATE, TIME, or DATETIME.
        is_required: Whether the field is required.
        options: Comma-separated options for SELECT type.
        placeholder: Placeholder text.
    """
    payload = _ensure_payload({"name": name, "label": label, "model_type": model_type, "order": order, "type": type, "is_required": is_required}, {})
    for k, v in [("options", options), ("placeholder", placeholder)]:
        if v:
            payload[k] = v
    return await get_client().create_custom_field(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_custom_field(id: int, name: str = None, label: str = None, model_type: str = None, order: int = None, type: str = None, is_required: bool = None, options: str = None, placeholder: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing custom field.

    Args:
        id: The unique ID of the custom field to update.
        name: Updated name.
        label: Updated label.
        model_type: Updated model type.
        order: Updated order.
        type: Updated type.
        is_required: Updated required flag.
        options: Updated options.
        placeholder: Updated placeholder.
    """
    payload = {}
    for k, v in [("name", name), ("label", label), ("model_type", model_type), ("order", order), ("type", type), ("is_required", is_required), ("options", options), ("placeholder", placeholder)]:
        if v is not None:
            payload[k] = v
    return await get_client().update_custom_field(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_custom_field_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a custom field by ID.

    Args:
        id: The unique ID of the custom field to delete.
    """
    await get_client().delete_custom_field_by_id(id, get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Tax Types (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_tax_types(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all tax type records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_tax_types(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_tax_type_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single tax type by ID.

    Args:
        id: The unique ID of the tax type.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_tax_type_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_tax_type(name: str, calculation_type: str, percent: str = "", fixed_amount: str = "", description: str = "", compound_tax: str = "", collective_tax: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new tax type.

    Args:
        name: Name of the tax type.
        calculation_type: percentage or fixed.
        percent: Tax percentage (required if calculation_type=percentage).
        fixed_amount: Fixed tax amount (required if calculation_type=fixed).
        description: Description of the tax type.
        compound_tax: Compound tax setting.
        collective_tax: Collective tax setting.
    """
    payload = {"name": name, "calculation_type": calculation_type}
    for k, v in [("percent", percent), ("fixed_amount", fixed_amount), ("description", description), ("compound_tax", compound_tax), ("collective_tax", collective_tax)]:
        if v:
            payload[k] = v
    return await get_client().create_tax_type(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_tax_type(id: int, name: str = None, calculation_type: str = None, percent: str = None, fixed_amount: str = None, description: str = None, compound_tax: str = None, collective_tax: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing tax type.

    Args:
        id: The unique ID of the tax type to update.
        name: Updated name.
        calculation_type: Updated calculation type.
        percent: Updated percentage.
        fixed_amount: Updated fixed amount.
        description: Updated description.
        compound_tax: Updated compound tax.
        collective_tax: Updated collective tax.
    """
    payload = {}
    for k, v in [("name", name), ("calculation_type", calculation_type), ("percent", percent), ("fixed_amount", fixed_amount), ("description", description), ("compound_tax", compound_tax), ("collective_tax", collective_tax)]:
        if v is not None:
            payload[k] = v
    return await get_client().update_tax_type(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_tax_type_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a tax type by ID.

    Args:
        id: The unique ID of the tax type to delete.
    """
    await get_client().delete_tax_type_by_id(id, get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Notes (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_notes(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all note records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_notes(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_note_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single note by ID.

    Args:
        id: The unique ID of the note.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_note_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_note(type: str, name: str, notes: str, is_default: bool, ctx: Context = None) -> dict[str, Any]:
    """Create a new note.

    Args:
        type: invoice, estimate, or payment.
        name: Name of the note.
        notes: Note content.
        is_default: Whether this is the default note for the type.
    """
    payload = {"type": type, "name": name, "notes": notes, "is_default": is_default}
    return await get_client().create_note(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_note(id: int, type: str = None, name: str = None, notes: str = None, is_default: bool = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing note.

    Args:
        id: The unique ID of the note to update.
        type: Updated type (invoice/estimate/payment).
        name: Updated name.
        notes: Updated content.
        is_default: Updated default flag.
    """
    payload = {}
    for k, v in [("type", type), ("name", name), ("notes", notes), ("is_default", is_default)]:
        if v is not None:
            payload[k] = v
    return await get_client().update_note(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_note_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a note by ID.

    Args:
        id: The unique ID of the note to delete.
    """
    await get_client().delete_note_by_id(id, get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Recurring Invoices (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_recurring_invoices(include_all_fields: bool = False, page: int = 0, page_size: int = 10, ctx: Context = None) -> dict[str, Any]:
    """List all recurring invoice records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
        page: Page number (0 for default).
        page_size: Records per page (0 for all).
    """
    data = await get_client().list_all_recurring_invoices(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False, page=page, page_size=page_size)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_recurring_invoice_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single recurring invoice by ID.

    Args:
        id: The unique ID of the recurring invoice.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_recurring_invoice_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_recurring_invoice(customer_id: int, starts_at: str, frequency: str, status: str, limit_by: str, send_automatically: bool, items: InvoiceLineItems, limit_count: str = "", limit_date: str = "", exchange_rate: str = "", discount: float = 0, discount_val: int = 0, sub_total: float = 0, total: float = 0, tax: float = 0, taxes: TaxesParam = None, currency_id: str = "", notes: str = "", ctx: Context = None) -> dict[str, Any]:
    """Create a new recurring invoice template.

    Args:
        customer_id: ID of the customer.
        starts_at: ISO 8601 format (2026-06-22T15:00:00-04:00).
        frequency: Cron expression for frequency (e.g. 0 0 1 * *).
        status: DRAFT or ACTIVE.
        limit_by: COUNT or DATE.
        send_automatically: Whether to send automatically.
        items: InvoiceLineItems object.
        limit_count: Required if limit_by=COUNT.
        limit_date: ISO 8601 format. Required if limit_by=DATE.
        exchange_rate: Exchange rate.
        discount: Discount amount. Default 0.
        discount_val: Discount value. Default 0.
        sub_total: Sub total (recomputed server-side). Default 0.
        total: Total (recomputed server-side). Default 0.
        tax: Tax amount (recomputed server-side). Default 0.
        taxes: TaxesParam object.
        currency_id: Currency ID.
        notes: Notes.
    """
    payload = _ensure_payload({
        "customer_id": customer_id, "starts_at": starts_at, "frequency": frequency, "status": status,
        "limit_by": limit_by, "send_automatically": send_automatically, "discount": discount,
        "discount_val": discount_val, "sub_total": sub_total, "total": total, "tax": tax,
    }, {"exchange_rate": "1"})
    items_list = items.model_dump(exclude_unset=True)["items"] if isinstance(items, InvoiceLineItems) else (items or [])
    for it in items_list:
        it.setdefault("discount_val", 0)
        it.setdefault("discount_type", "none")
        it.setdefault("tax", 0)
    payload["items"] = items_list
    for k, v in [("limit_count", limit_count), ("limit_date", limit_date), ("exchange_rate", exchange_rate), ("currency_id", currency_id), ("notes", notes)]:
        if v:
            payload[k] = v
    if taxes:
        payload["taxes"] = taxes.model_dump(exclude_unset=True).get("taxes", [])
    return await get_client().create_recurring_invoice(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_recurring_invoice(id: int, customer_id: int = None, starts_at: str = None, frequency: str = None, status: str = None, limit_by: str = None, send_automatically: bool = None, items: InvoiceLineItems = None, limit_count: str = None, limit_date: str = None, exchange_rate: str = None, discount: float = None, discount_val: int = None, sub_total: float = None, total: float = None, tax: float = None, taxes: TaxesParam = None, currency_id: str = None, notes: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing recurring invoice.

    Args:
        id: The unique ID of the recurring invoice to update.
        customer_id: Updated customer ID.
        starts_at: ISO 8601 format.
        frequency: Updated cron frequency.
        status: Updated status.
        limit_by: Updated limit method.
        send_automatically: Updated auto-send flag.
        items: Updated items (InvoiceLineItems).
        limit_count: Updated limit count.
        limit_date: Updated limit date.
        exchange_rate: Updated exchange rate.
        discount: Updated discount.
        discount_val: Updated discount value.
        sub_total: Updated sub total.
        total: Updated total.
        tax: Updated tax.
        taxes: Updated taxes (TaxesParam).
        currency_id: Updated currency ID.
        notes: Updated notes.
    """
    current = await get_client().get_recurring_invoice_by_id(id, get_user_token(), include_all_fields=True)
    payload = {}
    for k, v in [("customer_id", customer_id), ("starts_at", starts_at), ("frequency", frequency), ("status", status), ("limit_by", limit_by), ("send_automatically", send_automatically), ("limit_count", limit_count), ("limit_date", limit_date), ("exchange_rate", exchange_rate), ("discount", discount), ("discount_val", discount_val), ("sub_total", sub_total), ("total", total), ("tax", tax), ("currency_id", currency_id), ("notes", notes)]:
        if v is not None:
            payload[k] = v
    if items:
        items_list = items.model_dump(exclude_unset=True)["items"]
        for it in items_list:
            it.setdefault("discount_val", 0)
            it.setdefault("discount_type", "none")
            it.setdefault("tax", 0)
        payload["items"] = items_list
    if taxes:
        payload["taxes"] = taxes.model_dump(exclude_unset=True).get("taxes", [])
    if isinstance(current, dict):
        for k in ("customer_id", "starts_at", "frequency", "status", "limit_by", "send_automatically", "discount", "discount_val", "sub_total", "total", "tax"):
            if k not in payload:
                v = current.get(k)
                if v is not None:
                    payload[k] = v
        if "items" not in payload and current.get("items"):
            items_fallback = []
            for i in current["items"]:
                if isinstance(i, dict):
                    item = {"name": i["name"], "quantity": float(i.get("quantity", 1)), "price": float(i.get("price", 0)), "description": i.get("description", "")}
                    item.setdefault("discount_val", 0)
                    item.setdefault("discount_type", "none")
                    item.setdefault("tax", 0)
                    items_fallback.append(item)
            payload["items"] = items_fallback
    return await get_client().update_recurring_invoice(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_recurring_invoices_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a recurring invoice by ID.

    Args:
        id: The unique ID of the recurring invoice to delete.
    """
    await get_client().delete_recurring_invoices([id], get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Roles (5 tools)
# =============================================================================

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def list_all_roles(include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """List all role records.

    Args:
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    data = await get_client().list_all_roles(get_user_token(), include_all_fields=include_all_fields if ALLOW_ALL_AGGREGATE else False)
    return {"items": json_to_toon(data)}

@mcp.tool(tags={"read", "basic", "invoiceshelf"})
async def get_role_by_id(id: int, include_all_fields: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Get a single role by ID.

    Args:
        id: The unique ID of the role.
        include_all_fields: Default False (common fields only). Set True for all fields.
    """
    return await get_client().get_role_by_id(id, get_user_token(), include_all_fields=include_all_fields)

@mcp.tool(tags={"write", "basic", "invoiceshelf"})
async def create_role(name: str, abilities: str, ctx: Context = None) -> dict[str, Any]:
    """Create a new role with abilities.

    Args:
        name: Name of the role.
        abilities: JSON array of ability objects, e.g. [{"ability": "customers.create"}, {"ability": "invoices.view"}].
    """
    try:
        parsed = json.loads(abilities)
    except json.JSONDecodeError:
        parsed = []
    payload = {"name": name, "abilities": parsed}
    return await get_client().create_role(payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def update_role(id: int, name: str = None, abilities: str = None, ctx: Context = None) -> dict[str, Any]:
    """Update an existing role.

    Args:
        id: The unique ID of the role to update.
        name: Updated name.
        abilities: Updated abilities as JSON array.
    """
    payload = {}
    if name is not None:
        payload["name"] = name
    if abilities is not None:
        try:
            parsed = json.loads(abilities)
        except json.JSONDecodeError:
            parsed = []
        payload["abilities"] = parsed
    return await get_client().update_role(id, payload, get_user_token(), include_all_fields=ALLOW_ALL_AGGREGATE)

@mcp.tool(tags={"write", "primary", "invoiceshelf"})
async def delete_role_by_id(id: int, ctx: Context = None) -> dict[str, Any]:
    """Delete a role by ID.

    Args:
        id: The unique ID of the role to delete.
    """
    await get_client().delete_role_by_id(id, get_user_token())
    return {"deleted": True, "id": id}

# =============================================================================
# Entry Point
# =============================================================================

def main():
    if not os.getenv("INVOICESHELF_BASE_URL"):
        print("ERROR: INVOICESHELF_BASE_URL environment variable is required", file=sys.stderr)
        print("Example: export INVOICESHELF_BASE_URL=http://invoiceshelf-app:8080", file=sys.stderr)
        sys.exit(1)

    port_env = os.getenv("MCP_SERVER_PORT")
    if not port_env:
        print("ERROR: MCP_SERVER_PORT environment variable is required", file=sys.stderr)
        print("Example: export MCP_SERVER_PORT=5821", file=sys.stderr)
        sys.exit(1)

    host = "0.0.0.0"
    port = int(port_env)
    path = "/mcp"
    if IS_STATEFUL:
        app = mcp.http_app(path=path)
    else:
        app = mcp.http_app(path=path, stateless_http=True)
    app = AuthMiddleware(app)
    print(f"Starting InvoiceShelf MCP server on http://{host}:{port}{path}", file=sys.stderr)
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
