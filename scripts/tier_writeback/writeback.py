#!/usr/bin/env python3
"""Write BHC ICP tier, star and engagement tags onto Shopify customer records.

Reads the desired tag state from the Supabase customer_tiers view, compares it
to what customer_tier_state believes Shopify already has, and writes only the
difference.

    customer_tiers (truth)  --+
                              +--> diff --> tagsAdd / tagsRemove --> customer_tier_state
    customer_tier_state ------+

Safety properties, in order of importance:

  1. Dry run is the default. Without --execute nothing is written to Shopify and
     nothing is written to customer_tier_state.
  2. A test batch runs first. A full --execute pass refuses to start until
     customer_tier_state holds at least one confirmed write, so `--limit 10
     --execute` and an inspection in Shopify admin cannot be skipped.
  3. Nothing is recorded as written until Shopify confirms it. Every state row
     is written from an API response carrying that customer's own id back -
     never before the call, and never from a bare "the operation completed".
  4. A missing or rejected Shopify credential is fatal immediately, in dry run
     as well as in execute, so a bad token surfaces before the real pass.

Usage:
    export SUPABASE_DB_URL='postgresql://postgres.<ref>:<password>@<host>:5432/postgres'
    export SHOPIFY_SHOP='the-birdhouse-chick-2.myshopify.com'

    # Dev Dashboard app (current): the client credentials grant
    export SHOPIFY_CLIENT_ID='...'
    export SHOPIFY_CLIENT_SECRET='...'

    # or, for a legacy admin-created custom app:
    export SHOPIFY_ADMIN_TOKEN='shpat_...'

    python3 writeback.py                      # dry run, whole pending set
    python3 writeback.py --limit 10           # dry run, first ten
    python3 writeback.py --limit 10 --execute # the mandatory test batch
    python3 writeback.py --execute            # the full pass
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db          # noqa: E402
import tags        # noqa: E402
from shopify_api import (  # noqa: E402
    DEFAULT_API_VERSION,
    TAGS_ADD_MUTATION,
    TAGS_REMOVE_MUTATION,
    CredentialError,
    ShopifyAdmin,
    ShopifyError,
    parse_bulk_results,
)

# Above this many customers the per-customer path would be thousands of
# throttled calls, so the bulk import path is used instead.
BULK_THRESHOLD = 200

# A run at most this size counts as a test batch and is exempt from the
# "inspect the first ten" gate. Anything larger is a real pass.
TEST_BATCH_MAX = 50


def log(message=""):
    print(message, flush=True)


# ---------------------------------------------------------------- credentials


def choose_credentials(environ):
    """Pick the Shopify credential out of the environment.

    Returns (token, client_id, client_secret, note).

    The client credentials grant wins when both are present. A stale
    SHOPIFY_ADMIN_TOKEN still exported in a shell - editing the env file does
    not unset what an earlier `source` already put there - is otherwise
    invisible, and silently overrides a perfectly good client id and secret
    with a 401 that reads as though the credentials themselves are wrong.
    """
    token = environ.get("SHOPIFY_ADMIN_TOKEN") or None
    client_id = environ.get("SHOPIFY_CLIENT_ID") or None
    client_secret = environ.get("SHOPIFY_CLIENT_SECRET") or None
    note = None
    if token and client_id and client_secret:
        note = ("SHOPIFY_ADMIN_TOKEN is set as well; using the client "
                "credentials grant and ignoring it. Run `unset "
                "SHOPIFY_ADMIN_TOKEN` if it is left over from an earlier shell.")
        token = None
    return token, client_id, client_secret, note


# ------------------------------------------------------------------- planning


def build_plan(rows):
    """Attach the desired tag set to each row, failing fast on unknown values.

    An unmapped tier or engagement_state means the view has changed shape. That
    is not a per-customer error to be logged and skipped: tagging 8,600
    customers correctly while silently skipping the rest leaves the data in a
    state nobody can reason about. Better to stop before writing anything.
    """
    plan, unmapped = [], {}
    for row in rows:
        try:
            desired = tags.desired_tags(
                row["tier"], row["is_repeat_customer"], row["engagement_state"]
            )
        except tags.UnmappedValue as exc:
            unmapped[str(exc)] = unmapped.get(str(exc), 0) + 1
            continue
        plan.append({
            "customer_id": int(row["customer_id"]),
            "gid": tags.gid(row["customer_id"]),
            "email": row.get("customer_email"),
            "tier": row["tier"],
            "star": bool(row["is_repeat_customer"]),
            "engagement": row["engagement_state"],
            "state": row,
            "desired": desired,
        })

    if unmapped:
        detail = "\n  ".join(f"{value} ({count} customers)"
                             for value, count in sorted(unmapped.items()))
        raise SystemExit(
            "customer_tiers contains values the tag scheme has no entry for:\n  "
            + detail
            + "\n\nNothing was written. Add the mapping to tags.py and re-run."
        )
    return plan


def plan_from_state(plan):
    """Compute add/remove from customer_tier_state (the bulk path)."""
    for entry in plan:
        row = entry["state"]
        known = tags.tags_from_state(
            row.get("tier_written"), row.get("star_written"),
            row.get("engagement_written"),
        )
        entry["add"], entry["remove"] = tags.diff(known, entry["desired"])
        entry["source"] = "state"
    return plan


def plan_from_shopify(api, plan):
    """Compute add/remove from the customer's real tags (the per-customer path).

    Strictly better than diffing against state, because it self-corrects: a tag
    removed by hand in admin, or a removal that failed on an earlier run, is
    seen and fixed. Only affordable at per-customer scale, which is why the
    bulk path still diffs against state.
    """
    total = len(plan)
    for index, entry in enumerate(plan, 1):
        if index % 25 == 0 or index == total:
            log(f"  read tags for {index}/{total}")
        try:
            current = api.customer_tags(entry["gid"])
        except ShopifyError as exc:
            entry["error"] = f"reading tags: {exc}"
            entry["add"], entry["remove"] = [], []
            entry["source"] = "error"
            continue
        if current is None:
            entry["error"] = "customer does not exist in Shopify"
            entry["add"], entry["remove"] = [], []
            entry["source"] = "error"
            continue
        entry["current"] = current
        entry["add"], entry["remove"] = tags.diff(current, entry["desired"])
        entry["source"] = "shopify"
    return plan


# -------------------------------------------------------------------- reports


def describe_plan(plan, mode, show_detail):
    changed = [e for e in plan if e["add"] or e["remove"]]
    unchanged = [e for e in plan if not e["add"] and not e["remove"]
                 and not e.get("error")]
    broken = [e for e in plan if e.get("error")]

    log()
    log(f"selected by the diff query : {len(plan)}")
    log(f"  need a change            : {len(changed)}")
    log(f"  already correct          : {len(unchanged)}")
    if broken:
        log(f"  could not be read        : {len(broken)}")
    log(f"execution path             : {mode}")

    by_tier = {}
    for entry in changed:
        by_tier[entry["tier"]] = by_tier.get(entry["tier"], 0) + 1
    if by_tier:
        log()
        log("changes by tier:")
        for tier, count in sorted(by_tier.items(), key=lambda kv: -kv[1]):
            log(f"  {tier:<30} {count:>6}   ({tags.TIER_TAGS[tier]})")

    adds, removes = {}, {}
    for entry in changed:
        for tag in entry["add"]:
            adds[tag] = adds.get(tag, 0) + 1
        for tag in entry["remove"]:
            removes[tag] = removes.get(tag, 0) + 1
    if adds:
        log()
        log("tags to add:")
        for tag, count in sorted(adds.items(), key=lambda kv: -kv[1]):
            log(f"  +{tag:<24} {count:>6}")
    if removes:
        log()
        log("tags to remove:")
        for tag, count in sorted(removes.items(), key=lambda kv: -kv[1]):
            log(f"  -{tag:<24} {count:>6}")
    else:
        log()
        log("tags to remove: none "
            "(expected on the first pass - there is no stale tag yet)")

    if broken:
        log()
        log("cannot be written:")
        for entry in broken[:20]:
            log(f"  {entry['customer_id']}  {entry['error']}")
        if len(broken) > 20:
            log(f"  ... and {len(broken) - 20} more")

    if show_detail and changed:
        log()
        log("per customer:")
        for entry in changed:
            parts = [f"+{t}" for t in entry["add"]] + [f"-{t}" for t in entry["remove"]]
            log(f"  {entry['customer_id']:<16} {entry['tier']:<28} {' '.join(parts)}")
    return changed, unchanged, broken


def admin_links(shop, entries):
    store = shop.split(".")[0]
    log()
    log("inspect these in Shopify admin before the full pass:")
    for entry in entries:
        log(f"  https://admin.shopify.com/store/{store}/customers/{entry['customer_id']}")


# ------------------------------------------------------------------ execution


def execute_single(api, conn, plan):
    """One customer at a time. Used for the test batch and ongoing monthly runs."""
    succeeded, failed, reconciled = 0, 0, 0
    total = len(plan)
    for index, entry in enumerate(plan, 1):
        if entry.get("error"):
            db.record_failure(conn, entry["customer_id"], entry["error"])
            failed += 1
            continue

        if not entry["add"] and not entry["remove"]:
            # Shopify's own tags were read back and already match. That is a
            # confirmation, so the state row can be advanced.
            db.record_success(conn, entry["customer_id"], entry["tier"],
                              entry["star"], entry["engagement"])
            reconciled += 1
            continue

        label = f"[{index}/{total}] {entry['customer_id']}"
        try:
            # Add first, then remove. If the removal fails the customer briefly
            # carries both the correct tag and a stale one and is not recorded
            # as written, so the next run retries the removal. Removing first
            # and failing the add would drop them out of every segment instead.
            if entry["add"]:
                api.tags_add(entry["gid"], entry["add"])
            if entry["remove"]:
                api.tags_remove(entry["gid"], entry["remove"])
        except ShopifyError as exc:
            log(f"{label} FAILED: {exc}")
            db.record_failure(conn, entry["customer_id"], str(exc))
            failed += 1
            continue

        db.record_success(conn, entry["customer_id"], entry["tier"],
                          entry["star"], entry["engagement"])
        succeeded += 1
        parts = [f"+{t}" for t in entry["add"]] + [f"-{t}" for t in entry["remove"]]
        log(f"{label} {' '.join(parts)}")
    return succeeded, failed, reconciled


def _run_bulk_operation(api, kind, mutation, field, entries, poll_timeout):
    """One bulk operation over `entries`, returning (confirmed_ids, failures).

    A bulk mutation runs exactly one mutation, so adds and removes are two
    separate operations and Shopify runs one bulk mutation at a time per shop -
    hence the sequential calls from execute_bulk.
    """
    payload_key = "add" if kind == "add" else "remove"
    lines = [
        json.dumps({"id": entry["gid"], "tags": entry[payload_key]})
        for entry in entries
    ]
    content = ("\n".join(lines) + "\n").encode()
    filename = f"bhc_tier_tags_{kind}.jsonl"

    log()
    log(f"bulk {kind}: {len(entries)} customers, {len(content)} bytes of JSONL")
    log(f"  example line: {lines[0]}")

    target = api.staged_upload_target(filename)
    staged_path = api.upload_jsonl(target, content, filename)
    log(f"  uploaded, stagedUploadPath={staged_path}")

    operation = api.run_bulk_mutation(mutation, staged_path)
    log(f"  started {operation['id']}")
    operation = api.poll_bulk(operation["id"], timeout=poll_timeout)

    status = operation.get("status")
    results_url = operation.get("url")
    if status != "COMPLETED":
        log(f"  bulk operation {status}"
            f" (errorCode={operation.get('errorCode')})")
        results_url = operation.get("partialDataUrl")
        if not results_url:
            raise ShopifyError(
                f"bulk {kind} ended {status} with no results file; "
                f"nothing recorded as written"
            )
        log("  reading partial results so confirmed writes are still recorded")

    if not results_url:
        raise ShopifyError(
            f"bulk {kind} reported {status} but produced no results file; "
            f"nothing recorded as written"
        )

    raw = api.download(results_url)
    confirmed, failures, unrecognized = parse_bulk_results(raw, field)
    log(f"  confirmed {len(confirmed)}, failed {len(failures)}, "
        f"unrecognized lines {unrecognized}")
    if unrecognized:
        log(f"  WARNING: {unrecognized} result lines held no recognisable "
            f"{field} payload. Those customers are NOT recorded as written.")
    if not confirmed and entries:
        log(f"  WARNING: the operation processed "
            f"{operation.get('objectCount')} objects but confirmed zero "
            f"{kind}s. Check the JSONL shape before re-running.")
    return confirmed, failures


def execute_bulk(api, conn, plan, poll_timeout):
    running = api.running_bulk_mutation()
    if running:
        raise SystemExit(
            f"A bulk mutation is already running on this shop "
            f"({running['id']}, {running['status']}). Wait for it to finish "
            f"and re-run. Nothing was written."
        )

    adds = [e for e in plan if e["add"]]
    removes = [e for e in plan if e["remove"]]

    confirmed_add, failures_add = (set(), {})
    confirmed_remove, failures_remove = (set(), {})
    if adds:
        confirmed_add, failures_add = _run_bulk_operation(
            api, "add", TAGS_ADD_MUTATION, "tagsAdd", adds, poll_timeout)
    if removes:
        confirmed_remove, failures_remove = _run_bulk_operation(
            api, "remove", TAGS_REMOVE_MUTATION, "tagsRemove", removes,
            poll_timeout)

    written, errored = [], []
    for entry in plan:
        if not entry["add"] and not entry["remove"]:
            continue
        customer_id = entry["customer_id"]
        problems = []
        if entry["add"] and customer_id not in confirmed_add:
            problems.append("add: " + failures_add.get(
                customer_id, "no confirmation returned by Shopify"))
        if entry["remove"] and customer_id not in confirmed_remove:
            problems.append("remove: " + failures_remove.get(
                customer_id, "no confirmation returned by Shopify"))

        if problems:
            errored.append((customer_id, "; ".join(problems)))
        else:
            # Every operation this customer needed came back confirmed.
            written.append((customer_id, entry["tier"], entry["star"],
                            entry["engagement"]))

    db.record_success_many(conn, written)
    db.record_failure_many(conn, errored)
    return len(written), len(errored), 0


# ----------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true",
                        help="actually write to Shopify (default is a dry run)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="only process the first N pending customers")
    parser.add_argument("--mode", choices=("auto", "single", "bulk"), default="auto",
                        help="auto picks bulk above %d customers; --mode bulk with "
                             "--limit 10 smoke-tests the bulk path" % BULK_THRESHOLD)
    parser.add_argument("--poll-timeout", type=int, default=3600, metavar="SECONDS",
                        help="how long to wait for a bulk operation (default 3600)")
    parser.add_argument("--detail", action="store_true",
                        help="list every planned change, not just the totals")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set SUPABASE_DB_URL to the Supabase connection string.")

    # The credential is checked in dry run too: a bad token should surface now,
    # not at the top of the real pass.
    token, client_id, client_secret, note = choose_credentials(os.environ)
    if note:
        log(f"note      : {note}")
    api = ShopifyAdmin(
        shop=os.environ.get("SHOPIFY_SHOP", ""),
        token=token,
        client_id=client_id,
        client_secret=client_secret,
        api_version=os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION,
        log=log,
    )
    try:
        shop = api.check_credential()
    except CredentialError as exc:
        raise SystemExit(f"Shopify credential problem: {exc}\nNothing was written.")
    log(f"shopify   : {shop['name']} ({shop['myshopifyDomain']}) "
        f"api {api.api_version}")
    log(f"auth      : {'client credentials grant' if not api._static_token else 'access token'}"
        + (f", scopes {api.granted_scopes}" if api.granted_scopes else ""))

    conn = db.connect(dsn)
    log("supabase  : connected")
    log(f"mode      : {'EXECUTE - writing to Shopify' if args.execute else 'DRY RUN'}")
    if args.limit:
        log(f"limit     : {args.limit}")

    rows = db.fetch_pending(conn, args.limit)
    if not rows:
        log()
        log("Nothing to do: customer_tier_state already agrees with customer_tiers.")
        return

    plan = build_plan(rows)
    mode = args.mode
    if mode == "auto":
        mode = "bulk" if len(plan) > BULK_THRESHOLD else "single"

    if mode == "single":
        log()
        log("reading current tags from Shopify so the diff is against reality "
            "rather than against customer_tier_state ...")
        plan_from_shopify(api, plan)
    else:
        plan_from_state(plan)

    changed, unchanged, broken = describe_plan(
        plan, mode, args.detail or len(plan) <= 25)

    if not args.execute:
        log()
        log("DRY RUN - nothing was written to Shopify and nothing was recorded "
            "in customer_tier_state.")
        log("Re-run with --execute to write. Start with --limit 10 --execute.")
        return

    # The test batch is not optional. A full pass refuses to run until at least
    # one customer has actually been written and can be inspected in admin.
    is_test_batch = args.limit is not None and args.limit <= TEST_BATCH_MAX
    if not is_test_batch and db.confirmed_written_count(conn) == 0:
        raise SystemExit(
            "\ncustomer_tier_state has no confirmed writes yet, so this would be "
            "the first thing this job ever wrote - and it would write "
            f"{len(changed)} live customer records.\n\n"
            "Run the test batch first:\n\n"
            "    python3 writeback.py --limit 10 --execute\n\n"
            "then inspect those ten customers in Shopify admin. Once they look "
            "right, re-run this command.\n\nNothing was written."
        )

    if not changed and not broken:
        log()
        log("Every selected customer already carries the right tags; "
            "recording that in customer_tier_state.")

    log()
    log(f"writing via the {mode} path ...")
    if mode == "single":
        succeeded, failed, reconciled = execute_single(api, conn, plan)
    else:
        succeeded, failed, reconciled = execute_bulk(
            api, conn, plan, args.poll_timeout)

    attempted = len(changed) + len(broken)
    log()
    log("summary")
    log(f"  attempted : {attempted}")
    log(f"  succeeded : {succeeded}")
    log(f"  failed    : {failed}")
    log(f"  unchanged : {len(unchanged)}"
        + (" (state reconciled from Shopify's own tags)" if reconciled else ""))

    if failed:
        log()
        log("Failed customers were not recorded as written, so the next run "
            "retries them. Their messages are in customer_tier_state.last_error:")
        log("  select customer_id, last_error from customer_tier_state "
            "where last_error is not null;")

    remaining = len(db.fetch_pending(conn))
    log()
    log(f"still pending after this run : {remaining}")
    log(f"tier drift vs truth          : {db.drift_count(conn)} (must be 0 "
        f"once the full pass is done)")

    if succeeded and args.limit and args.limit <= TEST_BATCH_MAX:
        admin_links(api.shop, changed[:args.limit])


if __name__ == "__main__":
    main()
