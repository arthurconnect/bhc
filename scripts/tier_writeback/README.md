# BHC ICP tier write-back → Shopify

Writes the ICP tier, star and engagement tags from the Supabase `customer_tiers`
view onto Shopify customer records, and records what was written in
`customer_tier_state`.

**This job writes to live customer records in a production store.** Everything
else in this project has been read-only. Read the safety section before running it.

Flow: **read truth → diff against believed state → add/remove tags → record the
confirmed result**.

```
customer_tiers (truth)  ─┐
                         ├─► diff ─► tagsAdd / tagsRemove ─► customer_tier_state
customer_tier_state ─────┘
```

## Run it

```bash
export SUPABASE_DB_URL='host=<host> port=5432 dbname=postgres user=postgres.wgnrfautxbhkfyltqkuf password=<password>'
export SHOPIFY_SHOP='the-birdhouse-chick-2.myshopify.com'
export SHOPIFY_CLIENT_ID='...'
export SHOPIFY_CLIENT_SECRET='...'
pip install psycopg2-binary

python3 scripts/tier_writeback/writeback.py                      # dry run (default)
python3 scripts/tier_writeback/writeback.py --limit 10           # dry run, first ten
python3 scripts/tier_writeback/writeback.py --limit 10 --execute # the test batch
python3 scripts/tier_writeback/writeback.py --execute            # the full pass
```

`SUPABASE_DB_URL` accepts either the libpq keyword form above or a
`postgresql://` URI. The keyword form is worth preferring: it takes the password
literally, so a `@`, `#` or `%` in it doesn't need percent-encoding. Copy the
host from the Supabase dashboard's **Connect** button (*Direct → Connection
string → Session pooler*); the session pooler is the IPv4-reachable endpoint,
where the direct connection is IPv6-only.

## Shopify credentials

Shopify has **retired admin-created custom apps** — the flow that produced a
long-lived `shpat_` token from *Settings → Apps → Develop apps*. New apps are
created in the [Dev Dashboard](https://shopify.dev/docs/apps/build/dev-dashboard),
and a Dev Dashboard app never shows a token in the admin at all. It authenticates
with the **client credentials grant**: the app exchanges its client id and secret
for an access token that lives 24 hours, and requests another when that runs out.
This script does that exchange itself and refreshes as needed.

Setting the app up:

1. Dev Dashboard → **Apps** → **Create app** → *Start from Dev Dashboard*, name it
2. **Versions** → set the scopes to `read_customers,write_customers`
   (App URL can stay at its default; this app has no UI) → **Release**
3. App **Home** → scroll to **Install app** → pick the store → **Install**
4. **App settings** → copy the **Client ID** and **Secret**

The app and the store must be in the same Shopify organization, or the token
request fails with `shop_not_permitted`. The script surfaces that message
verbatim, because the fix is an org membership change rather than anything it
can retry.

`SHOPIFY_ADMIN_TOKEN` is still accepted for a legacy custom app that already
exists; set either that or the client id/secret pair. If both are set the client
credentials grant wins and the run says so — a stale `SHOPIFY_ADMIN_TOKEN` left
exported in a shell (editing the env file does not unset what an earlier
`source` put there) would otherwise shadow a good client id and secret with a
401 that looks like the credentials themselves are wrong.

| Flag | Effect |
| --- | --- |
| *(none)* | dry run: prints what it would do, writes nothing |
| `--execute` | actually write |
| `--limit N` | only the first N pending customers, ordered by `customer_id` |
| `--mode single\|bulk\|auto` | force an execution path; `auto` picks bulk above 200 customers |
| `--detail` | list every planned change, not just the totals |
| `--poll-timeout` | seconds to wait for a bulk operation (default 3600) |

## Safety properties

1. **Dry run is the default.** With no flags nothing reaches Shopify and nothing
   reaches `customer_tier_state`. `--execute` is required to write.
2. **The test batch is not skippable.** A full `--execute` pass refuses to start
   while `customer_tier_state` holds no confirmed write, and tells you to run
   `--limit 10 --execute` first. There is deliberately no flag to bypass this.
   After the test batch the script prints admin URLs for the ten customers.
3. **Nothing is recorded as written until Shopify confirms it.** Every
   `customer_tier_state` row is written from an API response that carried that
   customer's own id back. Not before the call, and not from a bare "the bulk
   operation completed" — a completed operation that tagged nothing confirms
   nobody, and records nobody.
4. **A missing or rejected credential is fatal immediately**, in dry run as well
   as in execute, so a bad token surfaces before the real pass rather than
   halfway through it.
5. **Only the eleven managed tags are ever removed.** Any other tag on the
   customer — `Wholesale`, or a hand-added `bhc-` tag — is left alone.
6. **A per-customer failure does not abort the run.** It lands in
   `customer_tier_state.last_error` with the `*_written` columns untouched, so
   the diff query picks that customer up again next run.
7. **An unmapped `tier` or `engagement_state` aborts before any write.** That
   means the view changed shape; tagging most of the base correctly and silently
   skipping the rest is worse than stopping.

## The tag scheme

One tier tag, plus an optional star, plus one engagement tag. All prefixed `bhc-`.

| Supabase value | Shopify tag |
| --- | --- |
| `SVIP Country Club Caroline` | `bhc-svip-caroline` |
| `VIP Country Club Caroline` | `bhc-vip-caroline` |
| `Country Club Caroline` | `bhc-caroline` |
| `VIP Backyard Betty` | `bhc-vip-betty` |
| `Backyard Betty` | `bhc-betty` |
| `Not yet tracked` | `bhc-not-tracked` |
| `is_repeat_customer = true` | `bhc-star` |
| `engagement_state` | `bhc-active` / `bhc-at-risk` / `bhc-winback` / `bhc-lapsed` |

**Exactly one tier tag and one engagement tag per customer at all times.** Tags
are additive in Shopify, so every add of a tier tag is paired with the removal of
the stale one in the same run. The star is independent of the tier.

Two details worth knowing:

* The view stores `at_risk` with an **underscore** while the tag is
  `bhc-at-risk` with a **hyphen**, so the mapping is an explicit table.
  `"bhc-" + engagement_state` would write `bhc-at_risk` onto 3,238 customers.
* Tag comparison is case-insensitive (Shopify dedupes that way) but removal
  sends the exact string Shopify holds, so a `BHC-Betty` left over from
  somewhere is actually removed rather than shadowed by a lowercase add.

Adds are sent before removes. If the removal then fails, the customer briefly
carries the correct tag *and* a stale one and is not recorded as written, so the
next run retries the removal. Removing first and failing the add would instead
drop them out of every segment.

## Two execution paths

**Bulk** (`auto` above 200 customers). `tagsAdd` takes one resource per call, so
the ~8,700-customer first pass goes through `bulkOperationRunMutation`:

1. `stagedUploadsCreate` with `resource: BULK_MUTATION_VARIABLES`,
   `mimeType: "text/jsonl"`, `httpMethod: POST`
2. POST the JSONL to the returned `url` as multipart form data — every returned
   parameter verbatim and in order, then the `file` part **last**
3. `bulkOperationRunMutation(mutation:, stagedUploadPath:)` where
   `stagedUploadPath` is the value of the returned `key` parameter
4. poll until the operation reaches a terminal state
5. download the results JSONL and record only the confirmed customers

A bulk mutation runs exactly one mutation, so adds and removes are two separate
operations run one after the other. On the first pass there is nothing to remove.

**Single** (`auto` at 200 or fewer). Plain `tagsRemove`/`tagsAdd` per customer —
the ongoing monthly runs, and the `--limit 10` test batch. This path first reads
each customer's real tags from Shopify and diffs against *those* rather than
against `customer_tier_state`, so it self-corrects: a tag removed by hand in
admin, or a removal that failed on an earlier run, is noticed and fixed. That is
only affordable at small scale, which is why the bulk path still diffs against
state.

To smoke-test the bulk machinery on a small batch before committing to 8,700:

```bash
python3 writeback.py --limit 10 --mode bulk --execute
```

## Verified against the live API

The build spec described the shape of the bulk path but not the exact field
names, and the failure mode for getting them subtly wrong is a completed
operation that silently tagged nothing. Every operation below was validated
against the live 2026-07 Admin schema, and the staged-upload parameters were
read off a real `stagedUploadsCreate` response for this store rather than copied
from prose:

| Thing | Verified value |
| --- | --- |
| `stagedUploadsCreate` input | `filename`, `mimeType`, `resource`, `httpMethod`, `fileSize` (`StagedUploadInput`) |
| resource enum for bulk variables | `BULK_MUTATION_VARIABLES` |
| staged target response | `stagedTargets { url resourceUrl parameters { name value } }` |
| real parameters returned | `Content-Type`, `success_action_status` (201), `acl`, `key`, `x-goog-date`, `x-goog-credential`, `x-goog-algorithm`, `x-goog-signature`, `policy` |
| `stagedUploadPath` | the value of the **`key`** parameter, not `url` and not `resourceUrl` |
| upload limits | the signed policy carries `content-length-range 1–104857600` and expires after 24h |
| `bulkOperationRunMutation` args | `mutation: String!`, `stagedUploadPath: String!`, optional `clientIdentifier` |
| `tagsAdd` / `tagsRemove` | `(id: ID!, tags: [String!]!)` → `{ node { id } userErrors { field message } }` |
| polling | `bulkOperation(id: ID!)` |
| token endpoint | `POST https://{shop}/admin/oauth/access_token`, form-encoded `grant_type=client_credentials` + `client_id` + `client_secret` |
| token response | `{access_token, scope, expires_in}`; `expires_in` is 86399 (24h), and `scope` reads back what the app's released version actually grants |
| scope readback | `write_customers` alone means read **and** write — Shopify folds read into write and collapses the pair, so an app granted `read_customers,write_customers` reads back as `write_customers` |

Two places where the spec would have gone wrong if followed literally:

* **Polling `currentBulkOperation` watches the wrong operation.** It is
  deprecated, and its `type` argument defaults to `QUERY` — so on this shop it
  would report `COMPLETED` off the last bulk *export* while the tag import was
  still running. This job polls `bulkOperation(id:)` with the id it was handed.
* **"Only one bulk operation at a time per shop" is narrower than that.** A bulk
  query and a bulk mutation can run concurrently; it is two bulk *mutations* that
  cannot. The job checks for a running mutation before starting and refuses with
  a clear message rather than being rejected mid-flight.

The credential shape changed under the spec too: it assumed a `shpat_` token,
which Shopify no longer issues for new apps. Because the token response echoes
the granted scopes, the credential check fails immediately and by name when
`write_customers` is missing, rather than letting that surface later as an
opaque per-customer permission error.

One thing that is **not** verified: the docs mark bulk operations "only
accessible by supported access tokens" without enumerating them, and there was
no way to confirm from the docs that a client-credentials token qualifies. It is
an ordinary offline Admin token, so it should — but that is reasoning, not
evidence, which is the specific reason to run `--limit 10 --mode bulk --execute`
before the full pass.

## Error handling

Per-customer failures go to `customer_tier_state.last_error` and the run
continues. A customer that errored is never recorded as written, so the next run
retries it. The run ends with `attempted / succeeded / failed / unchanged`, the
number still pending, and the drift count.

## Verification after the run

```sql
-- every customer accounted for
select count(*) from customer_tiers;         -- 8,672 as of 2026-08-22
select count(*) from customer_tier_state;

-- distribution written, should match the view
select tier_written, count(*) from customer_tier_state group by 1 order by 2 desc;

-- anything that failed
select customer_id, last_error from customer_tier_state where last_error is not null;

-- drift check: state should now agree with truth
select count(*) from customer_tiers t
join customer_tier_state s using (customer_id)
where s.tier_written is distinct from t.tier;
```

The last query must return zero — the script prints it at the end of every run.
Then spot-check five customers in Shopify admin against the view, including at
least one `bhc-svip-caroline`, since there are only 77 of them and they are the
ones Courtney may contact personally.

Klaviyo needs no separate job — its native Shopify integration syncs customer
tags onto profiles. Verify that in Klaviyo after the first pass rather than
assuming it.

## Expected distribution

Read from the live view on 2026-08-22; matches the 36-month census in the build
spec exactly.

| Tier | Customers | Tag |
| --- | --- | --- |
| SVIP Country Club Caroline | 77 | `bhc-svip-caroline` |
| VIP Country Club Caroline | 39 | `bhc-vip-caroline` |
| Country Club Caroline | 1,039 | `bhc-caroline` |
| VIP Backyard Betty | 187 | `bhc-vip-betty` |
| Backyard Betty | 1,899 | `bhc-betty` |
| Not yet tracked | 5,431 | `bhc-not-tracked` |

Engagement and star, for the same 8,672: `bhc-at-risk` 3,238, `bhc-active` 2,768,
`bhc-winback` 2,579, `bhc-lapsed` 87, `bhc-star` 781.

If the written distribution differs materially from this, stop and investigate
rather than re-running.

## Known limitation carried forward

Order history in Supabase spans exactly 36 months (2023-08-21 onward). `lapsed`
means "no purchase in 36 months", which on a 36-month window is nearly empty by
construction — hence only 87 `bhc-lapsed` tags. That is a property of the data,
not a bug, and it resolves only with a deeper backfill.

## Files

| File | Purpose |
| --- | --- |
| `writeback.py` | CLI, planning, dry-run report, both execution paths |
| `tags.py` | the tag scheme and the add/remove diff — pure functions |
| `shopify_api.py` | Admin API client: tagging, staged upload, bulk operations |
| `db.py` | the source query and `customer_tier_state` reads/writes |
| `test_tags.py` | unit tests for the tag scheme, the diff, the bulk results parser and the staged-upload body |

## Validation

`python3 test_tags.py` — 33 tests, no credentials or live network needed. Covers
the tag table, the `at_risk`/`at-risk` mismatch, the Betty→Caroline climb leaving
exactly one tier tag, case-insensitive matching with exact-case removal,
unmanaged tags surviving, the bulk results parser (including an unexpected result
shape confirming nobody), the multipart body keeping every signed parameter in
order with the file part last, and the client credentials grant — credentials
exchanged for a token, the token cached across calls, an expired token refetched,
a mid-run 401 refreshed exactly once, a missing `write_customers` scope named and
fatal, `write_customers` alone accepted (Shopify folds read into write and the
readback collapses the pair), and a legacy static token never exchanged. Also the credential precedence
rules, so a stale `SHOPIFY_ADMIN_TOKEN` cannot shadow the client credentials.

Beyond that, the whole job was run end to end against a local PostgreSQL mirror
of the live schema, seeded with the real 8,672-customer August 2026 census, and a
stand-in Shopify Admin API, authenticating through the client credentials grant.
54 checks, all passing:

* a dry run leaves both Shopify and `customer_tier_state` untouched
* a full `--execute` before any test batch is refused, having written nothing
* `--limit 10 --execute` writes exactly ten customers and ten state rows
* the full pass then tags all 8,672 through the bulk path: distribution matches
  the census, drift is zero, nothing left pending, every customer carries exactly
  one tier tag and one engagement tag, 3,238 carry `bhc-at-risk` and none carry
  `bhc-at_risk`
* re-running immediately is a no-op
* a tier climb adds the new tag, removes the stale one, and leaves one tier tag
* an injected per-customer failure is recorded in `last_error`, is *not* recorded
  as written, does not stop its neighbours, and is retried on the next run
* a bulk results file in an unexpected shape confirms nobody, warns loudly, and
  leaves every customer pending
* a bulk mutation already in flight, a missing credential (in dry run too), and
  an unmapped tier value each stop the run before any write

The stand-in API is a test harness, not a mock of Shopify's semantics: it proves
the job's own logic and wire format. The first real `--limit 10 --execute`
against the live store, inspected in admin, is still what proves the other half.
