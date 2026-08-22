# Shopify bulk-export → Supabase loader

Loads a Shopify bulk-operation JSONL export into Supabase project
`wgnrfautxbhkfyltqkuf`, tables `shopify_orders` and `shopify_order_line_items`.

Flow: **parse + validate → staging tables → verify counts → merge (upsert)**.
Nothing touches the live tables until the staged row counts match the export exactly.

## Run it

```bash
export SUPABASE_DB_URL='postgresql://postgres.wgnrfautxbhkfyltqkuf:<password>@<host>:5432/postgres'
pip install psycopg2-binary
python3 scripts/shopify_bulk_load/load.py path/to/bulk-export.jsonl
```

The connection string is in the Supabase dashboard under *Project Settings → Database →
Connection string*. Flags: `--dry-run` (parse and validate only), `--staging-only`
(load and verify staging, stop before the merge).

Without the database password there is a slower PostgREST fallback:

```bash
export SUPABASE_URL='https://wgnrfautxbhkfyltqkuf.supabase.co'
export SUPABASE_SERVICE_ROLE_KEY='<service-role-key>'
python3 scripts/shopify_bulk_load/load.py export.jsonl --rest
```

`--rest` can only fill the staging tables — run `merge.sql` from the SQL editor afterwards.

## Files

| File | Purpose |
| --- | --- |
| `transform.py` | JSONL → typed rows, plus pre-flight validation |
| `load.py` | staging load, count verification, merge driver |
| `merge.sql` | the upsert; runnable on its own from the SQL editor |

## Record shape

Lines **without** `__parentId` are orders. Lines **with** one are line items whose
`__parentId` is the parent order's GID.

## Field mapping

### `shopify_orders`

| Column | Source | Notes |
| --- | --- | --- |
| `order_id` | `id` | GID stripped to bare numeric |
| `order_number` | `name` | e.g. `#580827756` |
| `created_at_utc` / `processed_at_utc` / `cancelled_at_utc` | `createdAt` / `processedAt` / `cancelledAt` | |
| `customer_email` | `email` | |
| `customer_id` | `customer.id` | GID stripped |
| `financial_status` | `displayFinancialStatus` | enum left UPPERCASE |
| `fulfillment_status` | `displayFulfillmentStatus` | enum left UPPERCASE |
| `cancel_reason` | `cancelReason` | enum left UPPERCASE |
| `subtotal` / `shipping_charged` / `tax` / `discount` / `total` | `subtotalPriceSet` / `totalShippingPriceSet` / `totalTaxSet` / `totalDiscountsSet` / `totalPriceSet` | `shopMoney.amount`, 2dp |
| `source_name` | `sourceName` | |
| `landing_site` / `referring_site` | `customerJourneySummary.lastVisit.landingPage` / `.referrerUrl` | |
| `utm_source` / `utm_medium` / `utm_campaign` | `...lastVisit.utmParameters.*` | |
| `tags` | `tags[]` | joined with `", "`; `''` when empty, never NULL |
| `raw_json` | — | left NULL, matching the existing rows |

### `shopify_order_line_items`

| Column | Source | Notes |
| --- | --- | --- |
| `line_item_id` | `id` | **full `gid://shopify/LineItem/...` string kept** |
| `order_id` | `__parentId` | GID stripped |
| `sku` / `title` / `quantity` | `sku` / `title` / `quantity` | |
| `product_id` / `variant_id` | `product.id` / `variant.id` | GID stripped |
| `unit_price` | `originalUnitPriceSet` | per-unit list price |
| `unit_discount` | `originalUnitPriceSet − discountedUnitPriceSet` | per-unit reduction |
| `line_total` | `discountedTotalSet` | net line total |
| `fulfillable_quantity` | `unfulfilledQuantity` | |
| `fulfillment_status` | — | not in the bulk export; left NULL |
| `raw_json` | — | left NULL, matching the existing rows |

These formats were read off the ~90 days of data already in the tables so the two sets
join cleanly. Verified in the source export: `totalDiscountSet == (originalUnitPrice −
discountedUnitPrice) × quantity` and `discountedTotal == discountedUnitPrice × quantity`
for every row, so `line_total == quantity × (unit_price − unit_discount)` holds.

## Safety properties

* **Upsert, never plain insert** — `ON CONFLICT (pk) DO UPDATE`, so the overlap with the
  existing 90 days resolves in place instead of duplicating or erroring.
* **Orders merge before line items**, because `shopify_order_line_items.order_id` has a
  foreign key to `shopify_orders.order_id`.
* **Staging is verified first.** A count mismatch, a duplicate id, an orphan line item or
  a `line_total` that fails the arithmetic check aborts before anything is merged.
* **Idempotent.** Re-running the same export is a no-op on row counts.

## Validation performed on `f436f65d-stagingbulk6901246427378.jsonl`

9,953 orders and 12,498 line items; no duplicate ids, no orphan line items, no order
without line items; `createdAt` spans 2023-08-21 → 2026-08-21. Loaded against a local
PostgreSQL mirror of the live schema seeded with a production-like 90-day overlap:
945 orders / 1,222 line items updated in place, 9,008 / 11,276 inserted, final counts
9,953 / 12,498. All 22,451 rows were then compared field-by-field against the source
JSONL with zero mismatches, plus 400 independently re-derived spot checks.

The PostgREST (`--rest`) path is a convenience fallback and has not been exercised
end-to-end.
