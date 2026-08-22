#!/usr/bin/env python3
"""Load a Shopify bulk-operation JSONL export into Supabase.

Flow: parse + validate -> load staging tables -> verify counts -> merge (upsert).
Nothing touches the live tables until staging counts match the export exactly.

Usage:
    export SUPABASE_DB_URL='postgresql://postgres.<ref>:<password>@<host>:5432/postgres'
    python3 load.py /path/to/bulk-export.jsonl

    # or, without the database password (slower, uses PostgREST):
    export SUPABASE_URL='https://<ref>.supabase.co'
    export SUPABASE_SERVICE_ROLE_KEY='<service-role-key>'
    python3 load.py /path/to/bulk-export.jsonl --rest

Options:
    --dry-run       parse and validate only; touch nothing
    --staging-only  load and verify staging, then stop before the merge
"""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transform  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STAGING_ORDERS = "staging_shopify_orders"
STAGING_ITEMS = "staging_shopify_order_line_items"

DDL = """
create table if not exists public.staging_shopify_orders (
  order_id bigint primary key, order_number text, created_at_utc timestamptz not null,
  processed_at_utc timestamptz, cancelled_at_utc timestamptz, customer_email text,
  customer_id bigint, financial_status text, fulfillment_status text, cancel_reason text,
  subtotal numeric not null default 0, shipping_charged numeric not null default 0,
  tax numeric not null default 0, discount numeric not null default 0,
  total numeric not null default 0, source_name text, landing_site text, referring_site text,
  utm_source text, utm_medium text, utm_campaign text, tags text,
  loaded_at timestamptz not null default now()
);
create table if not exists public.staging_shopify_order_line_items (
  line_item_id text primary key, order_id bigint not null, sku text, product_id bigint,
  variant_id bigint, title text, quantity integer not null default 1,
  unit_price numeric not null default 0, unit_discount numeric not null default 0,
  line_total numeric not null default 0, fulfillment_status text,
  fulfillable_quantity integer, loaded_at timestamptz not null default now()
);
alter table public.staging_shopify_orders enable row level security;
alter table public.staging_shopify_order_line_items enable row level security;
"""


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- psycopg


def connect_pg(dsn):
    try:
        import psycopg2  # noqa: F401
        import psycopg2.extras
        return __import__("psycopg2").connect(dsn)
    except ImportError:
        pass
    try:
        import psycopg
        return psycopg.connect(dsn)
    except ImportError:
        raise SystemExit("Need psycopg2-binary or psycopg. pip install psycopg2-binary")


def copy_rows(cur, table, columns, rows):
    """Stream rows into `table` with COPY ... FROM STDIN (text format)."""
    buf = io.StringIO()
    for row in rows:
        fields = []
        for value in row:
            if value is None:
                fields.append(r"\N")
            else:
                text = str(value)
                text = (text.replace("\\", "\\\\").replace("\t", "\\t")
                            .replace("\n", "\\n").replace("\r", "\\r"))
                fields.append(text)
        buf.write("\t".join(fields) + "\n")
    buf.seek(0)
    sql = f"copy public.{table} ({','.join(columns)}) from stdin"
    try:                                  # psycopg3
        with cur.copy(sql) as copy:
            copy.write(buf.read())
    except AttributeError:                # psycopg2
        cur.copy_expert(sql, buf)


def run_pg(dsn, orders, items, staging_only):
    conn = connect_pg(dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)

            cur.execute("select count(*) from public.shopify_orders")
            before_orders = cur.fetchone()[0]
            cur.execute("select count(*) from public.shopify_order_line_items")
            before_items = cur.fetchone()[0]
            log(f"live tables before: orders={before_orders} line_items={before_items}")

            log("loading staging ...")
            cur.execute(f"truncate table public.{STAGING_ITEMS}")
            cur.execute(f"truncate table public.{STAGING_ORDERS}")
            copy_rows(cur, STAGING_ORDERS, transform.ORDER_COLUMNS, orders)
            copy_rows(cur, STAGING_ITEMS, transform.LINE_ITEM_COLUMNS, items)

            cur.execute(f"select count(*) from public.{STAGING_ORDERS}")
            staged_orders = cur.fetchone()[0]
            cur.execute(f"select count(*) from public.{STAGING_ITEMS}")
            staged_items = cur.fetchone()[0]
            log(f"staged: orders={staged_orders} line_items={staged_items}")

            if (staged_orders, staged_items) != (len(orders), len(items)):
                conn.rollback()
                raise SystemExit(
                    f"staging count mismatch: expected {len(orders)}/{len(items)}, "
                    f"got {staged_orders}/{staged_items} - nothing merged"
                )

            # Any staged line item whose order is in neither staging nor the live
            # table would violate the FK, so catch it before the merge.
            cur.execute(f"""
                select count(*) from public.{STAGING_ITEMS} s
                where not exists (select 1 from public.{STAGING_ORDERS} o where o.order_id = s.order_id)
                  and not exists (select 1 from public.shopify_orders o where o.order_id = s.order_id)
            """)
            orphans = cur.fetchone()[0]
            if orphans:
                conn.rollback()
                raise SystemExit(f"{orphans} staged line item(s) have no parent order - nothing merged")

            conn.commit()
            log("staging verified.")

            if staging_only:
                log("--staging-only: stopping before merge.")
                return

            log("merging ...")
            with open(os.path.join(HERE, "merge.sql"), encoding="utf-8") as fh:
                # merge.sql manages its own begin/commit
                merge_sql = fh.read()
            conn.autocommit = True
            cur.execute(merge_sql)
            conn.autocommit = False

            cur.execute("select count(*) from public.shopify_orders")
            after_orders = cur.fetchone()[0]
            cur.execute("select count(*) from public.shopify_order_line_items")
            after_items = cur.fetchone()[0]
            log(f"live tables after:  orders={after_orders} line_items={after_items}")
            log(f"  orders    +{after_orders - before_orders} new, "
                f"{len(orders) - (after_orders - before_orders)} updated in place")
            log(f"  line items +{after_items - before_items} new, "
                f"{len(items) - (after_items - before_items)} updated in place")
    finally:
        conn.close()


# ----------------------------------------------------------------------------- REST


def rest_call(base, key, method, path, payload=None, prefer=None):
    req = urllib.request.Request(f"{base}{path}", method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(req, body, timeout=300) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:500]}")


def rest_count(base, key, table):
    req = urllib.request.Request(f"{base}/rest/v1/{table}?select=*&limit=1", method="GET")
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Prefer", "count=exact")
    req.add_header("Range", "0-0")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return int(resp.headers.get("Content-Range", "0-0/0").split("/")[-1])


def run_rest(base, key, orders, items, staging_only):
    base = base.rstrip("/")
    log(f"live tables before: orders={rest_count(base, key, 'shopify_orders')} "
        f"line_items={rest_count(base, key, 'shopify_order_line_items')}")
    log("NOTE: --rest cannot create tables or run the merge. Run the DDL at the top of "
        "this script and merge.sql from the SQL editor.")

    for table, columns, rows in (
        (STAGING_ORDERS, transform.ORDER_COLUMNS, orders),
        (STAGING_ITEMS, transform.LINE_ITEM_COLUMNS, items),
    ):
        log(f"loading {table} ({len(rows)} rows) ...")
        for start in range(0, len(rows), 1000):
            batch = [dict(zip(columns, row)) for row in rows[start:start + 1000]]
            rest_call(base, key, "POST", f"/rest/v1/{table}?on_conflict={columns[0]}",
                      batch, prefer="resolution=merge-duplicates,return=minimal")
        log(f"  {table}: {rest_count(base, key, table)} rows")

    if not staging_only:
        log("Staging loaded. Now run scripts/shopify_bulk_load/merge.sql in the SQL editor.")


# ----------------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jsonl", help="Shopify bulk-operation JSONL export")
    parser.add_argument("--rest", action="store_true", help="use PostgREST instead of a direct DB connection")
    parser.add_argument("--dry-run", action="store_true", help="parse and validate only")
    parser.add_argument("--staging-only", action="store_true", help="stop before the merge")
    args = parser.parse_args()

    log(f"parsing {args.jsonl} ...")
    orders, items = transform.parse(args.jsonl)
    transform.check(orders, items)
    log(f"parsed: orders={len(orders)} line_items={len(items)} (validation OK)")

    if args.dry_run:
        log("--dry-run: nothing written.")
        return

    if args.rest:
        base = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not base or not key:
            raise SystemExit("--rest needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        run_rest(base, key, orders, items, args.staging_only)
    else:
        dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise SystemExit("Set SUPABASE_DB_URL (or DATABASE_URL), or pass --rest")
        run_pg(dsn, orders, items, args.staging_only)


if __name__ == "__main__":
    main()
