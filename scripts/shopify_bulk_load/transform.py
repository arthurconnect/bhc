"""Transform a Shopify bulk-operation JSONL export into rows for the Supabase
staging tables.

Lines WITHOUT __parentId are orders. Lines WITH __parentId are line items whose
__parentId is the parent order's GID.

Formats are chosen to match the ~90 days of data already in shopify_orders /
shopify_order_line_items so the two sets join cleanly:

  * order_id / customer_id / product_id / variant_id -> bare numerics (GID stripped)
  * line_item_id                                     -> full "gid://shopify/LineItem/..." string
  * GraphQL enums                                    -> left UPPERCASE (PAID, UNFULFILLED, ...)
  * tags                                             -> ", "-joined string, "" when empty (never NULL)
  * title                                            -> lineItem.name (product + variant), matching Make
  * attribution                                      -> customerJourneySummary.firstVisit for all four
                                                        of landing_site / referring_site / utm_*
"""

import decimal
import json

CENTS = decimal.Decimal("0.01")

ORDER_COLUMNS = (
    "order_id", "order_number", "created_at_utc", "processed_at_utc", "cancelled_at_utc",
    "customer_email", "customer_id", "financial_status", "fulfillment_status", "cancel_reason",
    "subtotal", "shipping_charged", "tax", "discount", "total", "source_name",
    "landing_site", "referring_site", "utm_source", "utm_medium", "utm_campaign", "tags",
)

LINE_ITEM_COLUMNS = (
    "line_item_id", "order_id", "sku", "product_id", "variant_id", "title",
    "quantity", "unit_price", "unit_discount", "line_total", "fulfillment_status",
    "fulfillable_quantity",
)


def gid_to_num(gid):
    """gid://shopify/Order/5315992420594 -> 5315992420594. None-safe."""
    return None if not gid else int(gid.rsplit("/", 1)[1])


def dig(obj, *path):
    """Walk nested dicts, returning None if any hop is missing or not a dict."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def money(obj, *path):
    """Read a MoneyBag shopMoney amount as a 2dp decimal string."""
    amount = dig(obj, *path, "shopMoney", "amount")
    return None if amount is None else str(decimal.Decimal(amount).quantize(CENTS))


def build_order(node):
    # First-touch attribution for all four fields. Note that Order.landingPageUrl /
    # Order.referrerUrl are deprecated aliases of lastVisit, so using them alongside
    # firstVisit UTMs (as the Make scenario used to) mixes two opposite models.
    first_visit = dig(node, "customerJourneySummary", "firstVisit")
    # ~500 orders carry no visit data at all but still have the deprecated
    # order-level fields, so fall back to those rather than blanking attribution.
    landing = dig(first_visit, "landingPage") or node.get("landingPageUrl")
    referrer = dig(first_visit, "referrerUrl") or node.get("referrerUrl")
    return (
        gid_to_num(node["id"]),
        node.get("name"),
        node.get("createdAt"),
        node.get("processedAt"),
        node.get("cancelledAt"),
        node.get("email"),
        gid_to_num(dig(node, "customer", "id")),
        node.get("displayFinancialStatus"),
        node.get("displayFulfillmentStatus"),
        node.get("cancelReason"),
        money(node, "subtotalPriceSet"),
        money(node, "totalShippingPriceSet"),
        money(node, "totalTaxSet"),
        money(node, "totalDiscountsSet"),
        money(node, "totalPriceSet"),
        node.get("sourceName"),
        landing,
        referrer,
        dig(first_visit, "utmParameters", "source"),
        dig(first_visit, "utmParameters", "medium"),
        dig(first_visit, "utmParameters", "campaign"),
        ", ".join(node.get("tags") or []),
    )


def line_item_fulfillment_status(node, order_cancelled):
    """Derive per-line fulfillment state. Mirrors the expression in the Make
    scenario exactly, so backfilled rows and synced rows always agree.

    unfulfilledQuantity == 0 means "nothing left to fulfill", which is also true
    of a cancelled order - hence the explicit CANCELLED branch first, so a
    cancelled line is never mislabelled as shipped.
    """
    if order_cancelled:
        return "CANCELLED"
    unfulfilled = node.get("unfulfilledQuantity")
    if unfulfilled is None:
        return None
    if unfulfilled == 0:
        return "FULFILLED"
    if unfulfilled >= node["quantity"]:
        return "UNFULFILLED"
    return "PARTIALLY_FULFILLED"


def build_line_item(node, order_cancelled=False):
    # The export carries per-unit list price, per-unit discounted price and the
    # line's discounted total. unit_discount is the per-unit reduction, so the
    # invariant line_total == quantity * (unit_price - unit_discount) holds.
    original = decimal.Decimal(dig(node, "originalUnitPriceSet", "shopMoney", "amount"))
    discounted = decimal.Decimal(dig(node, "discountedUnitPriceSet", "shopMoney", "amount"))
    return (
        node["id"],                                   # full GID, kept verbatim
        gid_to_num(node["__parentId"]),
        node.get("sku"),
        gid_to_num(dig(node, "product", "id")),
        gid_to_num(dig(node, "variant", "id")),
        node.get("name") or node.get("title"),   # lineItem.name: product + variant
        node["quantity"],
        str(original.quantize(CENTS)),
        str((original - discounted).quantize(CENTS)),
        money(node, "discountedTotalSet"),
        line_item_fulfillment_status(node, order_cancelled),
        node.get("unfulfilledQuantity"),
    )


def parse(path):
    """Return (orders, line_items) as lists of tuples in *_COLUMNS order."""
    orders, line_items = [], []
    cancelled = {}          # order GID -> whether the order is cancelled
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                node = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if "__parentId" in node:
                line_items.append(
                    build_line_item(node, cancelled.get(node["__parentId"], False))
                )
            else:
                cancelled[node["id"]] = bool(node.get("cancelledAt"))
                orders.append(build_order(node))
    return orders, line_items


def check(orders, line_items):
    """Fail fast on anything that would corrupt the merge."""
    problems = []

    order_ids = [row[0] for row in orders]
    if len(set(order_ids)) != len(order_ids):
        problems.append("duplicate order_id in export")

    item_ids = [row[0] for row in line_items]
    if len(set(item_ids)) != len(item_ids):
        problems.append("duplicate line_item_id in export")

    # Every line item must have its parent order in the same file, otherwise the
    # FK on shopify_order_line_items.order_id would reject it.
    known = set(order_ids)
    orphans = {row[1] for row in line_items} - known
    if orphans:
        problems.append(f"{len(orphans)} line-item parent order(s) missing from export")

    for row in line_items:
        qty = row[LINE_ITEM_COLUMNS.index("quantity")]
        price = decimal.Decimal(row[LINE_ITEM_COLUMNS.index("unit_price")])
        disc = decimal.Decimal(row[LINE_ITEM_COLUMNS.index("unit_discount")])
        total = decimal.Decimal(row[LINE_ITEM_COLUMNS.index("line_total")])
        if abs(qty * (price - disc) - total) > decimal.Decimal("0.011"):
            problems.append(f"line_total mismatch on {row[0]}")
            break

    if problems:
        raise SystemExit("Export failed validation:\n  " + "\n  ".join(problems))
