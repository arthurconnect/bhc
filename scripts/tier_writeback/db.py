"""Supabase reads and customer_tier_state writes.

customer_tiers is the truth. customer_tier_state is a mirror of what Shopify is
believed to be carrying, and is only ever advanced from a confirmed API
response - never optimistically before the call.
"""

import psycopg2
import psycopg2.extras

# The spec's source query. Ordered by customer_id so that a --limit batch is
# deterministic: re-running --limit 10 hits the same ten customers until they
# are written, instead of wandering across the base.
PENDING_SQL = """
select
  t.customer_id,
  t.customer_email,
  t.tier,
  t.is_repeat_customer,
  t.engagement_state,
  s.tier_written,
  s.star_written,
  s.engagement_written
from customer_tiers t
left join customer_tier_state s on s.customer_id = t.customer_id
where s.customer_id is null
   or s.tier_written       is distinct from t.tier
   or s.star_written       is distinct from t.is_repeat_customer
   or s.engagement_written is distinct from t.engagement_state
order by t.customer_id
"""


ALL_SQL = """
select
  t.customer_id,
  t.customer_email,
  t.tier,
  t.is_repeat_customer,
  t.engagement_state,
  s.tier_written,
  s.star_written,
  s.engagement_written
from customer_tiers t
left join customer_tier_state s on s.customer_id = t.customer_id
order by t.customer_id
"""


def connect(dsn):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True          # each customer's state lands on its own
    return conn


def fetch_pending(conn, limit=None):
    sql = PENDING_SQL + ("\nlimit %s" % int(limit) if limit else "")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(row) for row in cur.fetchall()]


def fetch_all(conn, limit=None):
    """Every customer in the view, regardless of what state says.

    A tag retired from the scheme has to reach customers whose tier state is
    already correct, and the diff query by definition skips those.
    """
    sql = ALL_SQL + ("\nlimit %s" % int(limit) if limit else "")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(row) for row in cur.fetchall()]


def record_success(conn, customer_id, tier, star, engagement):
    """Mark a customer written. Only ever called after Shopify has confirmed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into customer_tier_state
              (customer_id, tier_written, star_written, engagement_written,
               written_at, last_error)
            values (%s, %s, %s, %s, now(), null)
            on conflict (customer_id) do update set
              tier_written       = excluded.tier_written,
              star_written       = excluded.star_written,
              engagement_written = excluded.engagement_written,
              written_at         = now(),
              last_error         = null
            """,
            (int(customer_id), tier, star, engagement),
        )


def record_failure(conn, customer_id, error):
    """Record the error without advancing the *_written columns.

    Leaving them alone is what makes the next run retry this customer: the
    source query still sees state disagreeing with truth.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into customer_tier_state (customer_id, last_error)
            values (%s, %s)
            on conflict (customer_id) do update set last_error = excluded.last_error
            """,
            (int(customer_id), str(error)[:2000]),
        )


def record_success_many(conn, rows, chunk=500):
    """Batched record_success. rows: (customer_id, tier, star, engagement).

    The bulk path confirms thousands of customers at once, and one round trip
    each would dominate the run. Same discipline: only confirmed customers are
    ever passed in here.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        for start in range(0, len(rows), chunk):
            psycopg2.extras.execute_values(
                cur,
                """
                insert into customer_tier_state
                  (customer_id, tier_written, star_written, engagement_written,
                   written_at, last_error)
                values %s
                on conflict (customer_id) do update set
                  tier_written       = excluded.tier_written,
                  star_written       = excluded.star_written,
                  engagement_written = excluded.engagement_written,
                  written_at         = now(),
                  last_error         = null
                """,
                [(int(cid), tier, star, engagement)
                 for cid, tier, star, engagement in rows[start:start + chunk]],
                template="(%s, %s, %s, %s, now(), null)",
            )


def record_failure_many(conn, rows, chunk=500):
    """Batched record_failure. rows: (customer_id, error)."""
    if not rows:
        return
    with conn.cursor() as cur:
        for start in range(0, len(rows), chunk):
            psycopg2.extras.execute_values(
                cur,
                """
                insert into customer_tier_state (customer_id, last_error)
                values %s
                on conflict (customer_id) do update set
                  last_error = excluded.last_error
                """,
                [(int(cid), str(error)[:2000])
                 for cid, error in rows[start:start + chunk]],
            )


def confirmed_written_count(conn):
    """Customers with a real confirmed write behind them.

    Used to tell whether the mandatory test batch has actually run: a row that
    only carries last_error doesn't count.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from customer_tier_state where tier_written is not null"
        )
        return cur.fetchone()[0]


def written_distribution(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            select tier_written, count(*) from customer_tier_state
            where tier_written is not null group by 1 order by 2 desc
            """
        )
        return cur.fetchall()


def drift_count(conn):
    """State rows that disagree with truth. Must be zero after a full pass."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) from customer_tiers t
            join customer_tier_state s using (customer_id)
            where s.tier_written is distinct from t.tier
            """
        )
        return cur.fetchone()[0]
