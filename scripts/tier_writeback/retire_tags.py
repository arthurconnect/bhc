#!/usr/bin/env python3
"""One-off: strip retired tags from customers the tier write-back cannot reach.

writeback.py walks customer_tiers - customers with an order inside the 36-month
window. A dormant customer still carrying `VIP Betty` is invisible to it, which
is why the first pass cleared 582 retired tags and left 694 behind.

This works off a full Shopify customer export instead, so it reaches everyone in
the store. It removes nothing except the tags in tags.RETIRED_TAGS, touches no
other tag, and never writes to customer_tier_state - those customers are not in
the census and do not belong in a mirror of it.

    export SHOPIFY_SHOP='the-birdhouse-chick-2.myshopify.com'
    export SHOPIFY_CLIENT_ID='...'
    export SHOPIFY_CLIENT_SECRET='...'

    python3 retire_tags.py             # dry run, the default
    python3 retire_tags.py --execute   # actually remove them
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tags                                          # noqa: E402
from shopify_api import (                            # noqa: E402
    DEFAULT_API_VERSION,
    TAGS_REMOVE_MUTATION,
    CredentialError,
    ShopifyAdmin,
    ShopifyError,
    parse_bulk_results,
)
from writeback import choose_credentials             # noqa: E402

_RETIRED_NORM = {tag.casefold(): tag for tag in tags.RETIRED_TAGS}


def log(message=""):
    print(message, flush=True)


def find_retired(actual_by_customer):
    """{customer_id: [exact tag strings to remove]} for everyone carrying one.

    Matching is case-insensitive because Shopify dedupes tags that way, but the
    strings handed to tagsRemove are the ones Shopify actually holds - removing
    "VIP Betty" would not shift a stray "vip betty".
    """
    found = {}
    for customer_id, current in actual_by_customer.items():
        doomed = sorted(
            tag for tag in current
            if (tag or "").strip().casefold() in _RETIRED_NORM
        )
        if doomed:
            found[customer_id] = doomed
    return found


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true",
                        help="actually remove the tags (default is a dry run)")
    parser.add_argument("--poll-timeout", type=int, default=3600, metavar="SECONDS")
    args = parser.parse_args()

    token, client_id, client_secret, note = choose_credentials(os.environ)
    if note:
        log(f"note      : {note}")
    api = ShopifyAdmin(
        shop=os.environ.get("SHOPIFY_SHOP", ""),
        token=token, client_id=client_id, client_secret=client_secret,
        api_version=os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION,
        log=log,
    )
    try:
        shop = api.check_credential()
    except CredentialError as exc:
        raise SystemExit(f"Shopify credential problem: {exc}\nNothing was written.")
    log(f"shopify   : {shop['name']} ({shop['myshopifyDomain']})")
    log(f"mode      : {'EXECUTE - removing tags' if args.execute else 'DRY RUN'}")
    log(f"retiring  : {', '.join(sorted(tags.RETIRED_TAGS))}")

    log()
    log("exporting every customer in the store ...")
    actual = api.export_customer_tags(args.poll_timeout)
    log(f"  {len(actual)} customers in the export")

    found = find_retired(actual)
    by_tag = {}
    for doomed in found.values():
        for tag in doomed:
            by_tag[tag] = by_tag.get(tag, 0) + 1

    log()
    log(f"customers carrying a retired tag : {len(found)}")
    for tag, count in sorted(by_tag.items(), key=lambda kv: -kv[1]):
        log(f"  {tag:<24} {count:>6}")

    if not found:
        log()
        log("Nothing to do - no retired tags left in the store.")
        return

    # Prove the blast radius before doing anything: every other tag these
    # customers carry is listed here and none of it will be touched.
    survivors = {}
    for customer_id, doomed in found.items():
        for tag in actual[customer_id]:
            if tag not in doomed:
                survivors[tag] = survivors.get(tag, 0) + 1
    if survivors:
        log()
        log("other tags on those same customers, all left untouched:")
        for tag, count in sorted(survivors.items(), key=lambda kv: -kv[1]):
            log(f"  {tag:<24} {count:>6}")

    if not args.execute:
        log()
        log("DRY RUN - nothing was removed. Re-run with --execute.")
        return

    running = api.running_bulk("MUTATION")
    if running:
        raise SystemExit(
            f"A bulk mutation is already running on this shop "
            f"({running['id']}, {running['status']}). Nothing was removed.")

    lines = [
        json.dumps({"id": tags.gid(customer_id), "tags": doomed})
        for customer_id, doomed in sorted(found.items())
    ]
    content = ("\n".join(lines) + "\n").encode()
    filename = "bhc_retire_tags.jsonl"

    log()
    log(f"removing from {len(lines)} customers, {len(content)} bytes of JSONL")
    log(f"  example line: {lines[0]}")

    target = api.staged_upload_target(filename)
    staged_path = api.upload_jsonl(target, content, filename)
    operation = api.run_bulk_mutation(TAGS_REMOVE_MUTATION, staged_path)
    log(f"  started {operation['id']}")
    operation = api.poll_bulk(operation["id"], timeout=args.poll_timeout)

    results_url = operation.get("url")
    if operation.get("status") != "COMPLETED":
        log(f"  bulk operation {operation.get('status')} "
            f"(errorCode={operation.get('errorCode')})")
        results_url = operation.get("partialDataUrl")
    if not results_url:
        raise SystemExit("The operation produced no results file; nothing confirmed.")

    confirmed, failures, unrecognized = parse_bulk_results(
        api.download(results_url), "tagsRemove")
    log(f"  confirmed {len(confirmed)}, failed {len(failures)}, "
        f"unrecognized lines {unrecognized}")
    for customer_id, message in list(failures.items())[:20]:
        log(f"    {customer_id}: {message}")

    # Re-export rather than trust the confirmation: this is the only run that
    # will ever touch these customers, so prove the store actually changed.
    log()
    log("re-exporting to verify ...")
    remaining = find_retired(api.export_customer_tags(args.poll_timeout))
    log()
    log("summary")
    log(f"  carried a retired tag : {len(found)}")
    log(f"  confirmed removed     : {len(confirmed)}")
    log(f"  failed                : {len(failures)}")
    log(f"  still carrying one    : {len(remaining)}")
    if remaining:
        log()
        log("Still present - re-run to retry:")
        for customer_id, doomed in list(remaining.items())[:20]:
            log(f"  {customer_id}: {', '.join(doomed)}")
    else:
        log()
        log("No retired tags remain anywhere in the store.")


if __name__ == "__main__":
    main()
