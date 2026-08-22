-- Merge staged Shopify bulk-export rows into the live tables.
-- Upsert on primary key (never a plain INSERT) so the overlap with the existing
-- ~90 days of data resolves in place instead of duplicating or erroring.
-- Orders go first: shopify_order_line_items.order_id has an FK to shopify_orders.

begin;

insert into public.shopify_orders as t (
  order_id, order_number, created_at_utc, processed_at_utc, cancelled_at_utc,
  customer_email, customer_id, financial_status, fulfillment_status, cancel_reason,
  subtotal, shipping_charged, tax, discount, total, source_name,
  landing_site, referring_site, utm_source, utm_medium, utm_campaign, tags, synced_at
)
select
  s.order_id, s.order_number, s.created_at_utc, s.processed_at_utc, s.cancelled_at_utc,
  s.customer_email, s.customer_id, s.financial_status, s.fulfillment_status, s.cancel_reason,
  s.subtotal, s.shipping_charged, s.tax, s.discount, s.total, s.source_name,
  s.landing_site, s.referring_site, s.utm_source, s.utm_medium, s.utm_campaign,
  coalesce(s.tags, ''), now()   -- a CSV import turns an empty tag list into NULL; the table's convention is ''
from public.staging_shopify_orders s
on conflict (order_id) do update set
  order_number       = excluded.order_number,
  created_at_utc     = excluded.created_at_utc,
  processed_at_utc   = excluded.processed_at_utc,
  cancelled_at_utc   = excluded.cancelled_at_utc,
  customer_email     = excluded.customer_email,
  customer_id        = excluded.customer_id,
  financial_status   = excluded.financial_status,
  fulfillment_status = excluded.fulfillment_status,
  cancel_reason      = excluded.cancel_reason,
  subtotal           = excluded.subtotal,
  shipping_charged   = excluded.shipping_charged,
  tax                = excluded.tax,
  discount           = excluded.discount,
  total              = excluded.total,
  source_name        = excluded.source_name,
  landing_site       = excluded.landing_site,
  referring_site     = excluded.referring_site,
  utm_source         = excluded.utm_source,
  utm_medium         = excluded.utm_medium,
  utm_campaign       = excluded.utm_campaign,
  tags               = excluded.tags,
  synced_at          = now();

insert into public.shopify_order_line_items as t (
  line_item_id, order_id, sku, product_id, variant_id, title,
  quantity, unit_price, unit_discount, line_total,
  fulfillment_status, fulfillable_quantity, synced_at
)
select
  s.line_item_id, s.order_id, s.sku, s.product_id, s.variant_id, s.title,
  s.quantity, s.unit_price, s.unit_discount, s.line_total,
  s.fulfillment_status, s.fulfillable_quantity, now()
from public.staging_shopify_order_line_items s
on conflict (line_item_id) do update set
  order_id             = excluded.order_id,
  sku                  = excluded.sku,
  product_id           = excluded.product_id,
  variant_id           = excluded.variant_id,
  title                = excluded.title,
  quantity             = excluded.quantity,
  unit_price           = excluded.unit_price,
  unit_discount        = excluded.unit_discount,
  line_total           = excluded.line_total,
  fulfillment_status   = excluded.fulfillment_status,
  fulfillable_quantity = excluded.fulfillable_quantity,
  synced_at            = now();

commit;
