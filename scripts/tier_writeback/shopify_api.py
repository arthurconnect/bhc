"""Shopify Admin API client: single-customer tagging and the bulk import path.

Every GraphQL operation here was validated against the live 2026-07 Admin
schema, and the staged-upload parameter names were read off a real
stagedUploadsCreate response rather than taken from the build spec. See the
README's "Verified against the live API" section.

Two ways to authenticate:

  * client_id + client_secret - the client credentials grant, used by apps
    created in the Shopify Dev Dashboard. This is the current path: Shopify
    retired admin-created custom apps, and a Dev Dashboard app never shows a
    token in the admin at all. The client exchanges the credentials for a
    24-hour token and refreshes it as needed.
  * token - a long-lived shpat_ token from a legacy admin-created custom app.
    Still accepted so existing installs keep working.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_API_VERSION = "2026-07"

# Terminal states for a bulk operation.
_FINISHED = {"COMPLETED", "FAILED", "CANCELED", "EXPIRED"}

TAGS_ADD_MUTATION = """
mutation addTags($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { node { id } userErrors { field message } }
}
""".strip()

TAGS_REMOVE_MUTATION = """
mutation removeTags($id: ID!, $tags: [String!]!) {
  tagsRemove(id: $id, tags: $tags) { node { id } userErrors { field message } }
}
""".strip()

_SHOP_QUERY = "query shopCheck { shop { name myshopifyDomain } }"

_CUSTOMER_TAGS_QUERY = "query customerTags($id: ID!) { customer(id: $id) { id tags } }"

_STAGED_UPLOAD_MUTATION = """
mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
""".strip()

_RUN_BULK_MUTATION = """
mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) {
  bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) {
    bulkOperation { id status type url partialDataUrl objectCount errorCode createdAt }
    userErrors { field message code }
  }
}
""".strip()

_RUN_BULK_QUERY = """
mutation bulkOperationRunQuery($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status type url partialDataUrl objectCount errorCode createdAt }
    userErrors { field message }
  }
}
""".strip()

# The export the bulk write path diffs against. Reading every customer's real
# tags is what lets a bulk run remove a stale or retired tag it never wrote -
# customer_tier_state only knows what this job put there.
CUSTOMER_TAGS_EXPORT = """
{
  customers {
    edges { node { id tags } }
  }
}
""".strip()

_POLL_QUERY = """
query bulkOperationById($id: ID!) {
  bulkOperation(id: $id) {
    id status type errorCode objectCount rootObjectCount
    url partialDataUrl createdAt completedAt
  }
}
""".strip()

# bulkOperations takes no `type` argument - the type is filtered client-side.
_RUNNING_QUERY = """
query runningBulkOps {
  bulkOperations(first: 20, query: "status:created OR status:running",
                 sortKey: CREATED_AT, reverse: true) {
    nodes { id status type createdAt }
  }
}
""".strip()


class ShopifyError(Exception):
    """Any failure talking to Shopify, or any userErrors coming back."""


class CredentialError(ShopifyError):
    """Missing or rejected credential. Always fatal, never retried."""


def normalize_shop(shop):
    """'https://x.myshopify.com/' -> 'x.myshopify.com'."""
    shop = (shop or "").strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if shop.startswith(prefix):
            shop = shop[len(prefix):]
    return shop


class ShopifyAdmin:
    def __init__(self, shop, token=None, client_id=None, client_secret=None,
                 api_version=DEFAULT_API_VERSION, timeout=120,
                 max_retries=5, log=print):
        self.shop = normalize_shop(shop)
        self.api_version = api_version
        self.timeout = timeout
        self.max_retries = max_retries
        self.log = log
        self.endpoint = f"https://{self.shop}/admin/api/{api_version}/graphql.json"
        self.token_endpoint = f"https://{self.shop}/admin/oauth/access_token"

        self.client_id = client_id
        self.client_secret = client_secret
        self._static_token = token or None
        self._token = token or None
        self._token_expires_at = None       # time.monotonic() deadline
        self._token_margin = 0              # refresh this many seconds early
        self.granted_scopes = None          # read back from the token response

    # ---------------------------------------------------------------- token

    def _fetch_token(self):
        """Exchange client credentials for a 24-hour Admin API access token.

        Only works when the app and the store are in the same Shopify
        organization; otherwise Shopify answers shop_not_permitted, which is
        surfaced verbatim because the fix is an org membership change, not
        anything this script can retry its way out of.
        """
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()
        request = urllib.request.Request(self.token_endpoint, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise CredentialError(
                f"Could not get an access token from {self.token_endpoint} "
                f"(HTTP {exc.code}): {detail}"
            ) from None
        except urllib.error.URLError as exc:
            raise CredentialError(
                f"Could not reach {self.token_endpoint}: {exc.reason}"
            ) from None

        token = payload.get("access_token")
        if not token:
            raise CredentialError(f"Token endpoint returned no access_token: {payload}")
        # expires_in is 86399 (24h). The margin makes a long bulk run refresh
        # early rather than have a request land on a token that expired
        # mid-flight; it never exceeds half the token's own lifetime, so an
        # unexpectedly short-lived token is refetched instead of trusted.
        raw_expiry = payload.get("expires_in")
        expires_in = max(int(raw_expiry), 0) if raw_expiry is not None else 86399
        self._token = token
        self._token_expires_at = time.monotonic() + expires_in
        self._token_margin = min(300, expires_in // 2)
        self.granted_scopes = payload.get("scope")
        return token

    def _access_token(self):
        if self._static_token:
            return self._static_token
        if not (self.client_id and self.client_secret):
            raise CredentialError(
                "No Shopify credential: set SHOPIFY_CLIENT_ID and "
                "SHOPIFY_CLIENT_SECRET (Dev Dashboard app), or SHOPIFY_ADMIN_TOKEN "
                "(legacy custom app)."
            )
        if self._token and self._token_expires_at is not None and \
                time.monotonic() < self._token_expires_at - self._token_margin:
            return self._token
        return self._fetch_token()

    def _invalidate_token(self):
        """Drop the cached token so the next call fetches a fresh one."""
        if not self._static_token:
            self._token = None
            self._token_expires_at = None
            self._token_margin = 0

    # ------------------------------------------------------------- transport

    def graphql(self, query, variables=None):
        """POST a GraphQL document. Retries throttling and transient 5xx."""
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        delay = 1.0
        last = None
        refreshed = False
        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(self.endpoint, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Shopify-Access-Token", self._access_token())
            request.add_header("Accept", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 401 and not self._static_token and not refreshed:
                    # A 24-hour token can expire mid-run. Get a new one once,
                    # then treat a second 401 as a real credential failure.
                    self._invalidate_token()
                    refreshed = True
                    last = "401, refreshing the access token"
                elif exc.code in (401, 403):
                    # Name the credential actually used: a 401 while a stale
                    # static token shadows the client id and secret otherwise
                    # reads as "my client credentials are wrong".
                    using = ("the SHOPIFY_ADMIN_TOKEN" if self._static_token
                             else "the client credentials grant")
                    raise CredentialError(
                        f"Shopify rejected the Admin API credential ({exc.code}) "
                        f"using {using}: {detail}"
                    ) from None
                elif exc.code == 404:
                    raise CredentialError(
                        f"No Admin API at {self.endpoint} (404). Check SHOPIFY_SHOP "
                        f"and SHOPIFY_API_VERSION."
                    ) from None
                elif exc.code == 429 or exc.code >= 500:
                    last = f"HTTP {exc.code}: {detail}"
                else:
                    raise ShopifyError(f"HTTP {exc.code}: {detail}") from None
            except urllib.error.URLError as exc:
                last = f"network error: {exc.reason}"
            else:
                errors = payload.get("errors")
                if errors:
                    throttled = any(
                        (error.get("extensions") or {}).get("code") == "THROTTLED"
                        for error in errors
                    )
                    if not throttled:
                        raise ShopifyError(json.dumps(errors)[:1000])
                    last = "THROTTLED"
                else:
                    return payload.get("data") or {}

            if attempt < self.max_retries:
                self.log(f"  retrying after {last} (attempt {attempt}/{self.max_retries})")
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise ShopifyError(f"gave up after {self.max_retries} attempts: {last}")

    # ------------------------------------------------------------ credential

    def check_credential(self):
        """Prove the credential works before anything else happens.

        Fatal on failure, and fatal on a credential that authenticates but
        lacks write_customers - a token that cannot do the job is not a
        working credential, and finding that out now beats finding out after
        the operator has approved a run.
        """
        if not self.shop:
            raise CredentialError("SHOPIFY_SHOP is not set")
        if not self._static_token and not (self.client_id and self.client_secret):
            raise CredentialError(
                "No Shopify credential. Set SHOPIFY_CLIENT_ID and "
                "SHOPIFY_CLIENT_SECRET for a Dev Dashboard app, or "
                "SHOPIFY_ADMIN_TOKEN for a legacy custom app."
            )
        self._access_token()          # fetches and validates, or raises
        data = self.graphql(_SHOP_QUERY)
        shop = (data or {}).get("shop")
        if not shop or not shop.get("myshopifyDomain"):
            raise CredentialError(f"unexpected response to shop query: {data}")

        # The token response echoes back the scopes on the app's released
        # version, so a missing one can be named exactly rather than surfacing
        # later as an opaque per-customer permission error.
        if self.granted_scopes is not None:
            granted = {s.strip() for s in self.granted_scopes.split(",") if s.strip()}
            # Shopify folds read into write - "any permission to write a
            # resource includes permission to read it" - and collapses the
            # pair in the readback, so an app granted both comes back as just
            # write_customers. Demanding read_customers literally would reject
            # a credential that can do everything this job needs.
            can_write = "write_customers" in granted
            can_read = can_write or "read_customers" in granted
            if not (can_write and can_read):
                missing = ([] if can_read else ["read_customers"]) + \
                          ([] if can_write else ["write_customers"])
                raise CredentialError(
                    f"The credential works, but is missing {', '.join(missing)}. "
                    f"Granted: {self.granted_scopes or '(none)'}.\n"
                    f"Add the scope to the app's version in the Dev Dashboard, "
                    f"Release it, then re-approve the app on the store."
                )
        return shop

    # ------------------------------------------------------ single-customer

    def customer_tags(self, customer_gid):
        """Current tags, or None if Shopify has no such customer."""
        data = self.graphql(_CUSTOMER_TAGS_QUERY, {"id": customer_gid})
        customer = (data or {}).get("customer")
        if customer is None:
            return None
        return list(customer.get("tags") or [])

    def _tag_call(self, mutation, field, customer_gid, tag_list):
        data = self.graphql(mutation, {"id": customer_gid, "tags": list(tag_list)})
        payload = (data or {}).get(field) or {}
        errors = payload.get("userErrors") or []
        if errors:
            raise ShopifyError(
                f"{field}: " + "; ".join(
                    f"{'.'.join(e.get('field') or [])}: {e.get('message')}".strip(": ")
                    for e in errors
                )
            )
        node_id = (payload.get("node") or {}).get("id")
        if node_id != customer_gid:
            # No confirmed node back means no confirmed write. Never record it.
            raise ShopifyError(
                f"{field}: Shopify returned no confirmation for {customer_gid} "
                f"(node={node_id!r})"
            )
        return node_id

    def tags_add(self, customer_gid, tag_list):
        return self._tag_call(TAGS_ADD_MUTATION, "tagsAdd", customer_gid, tag_list)

    def tags_remove(self, customer_gid, tag_list):
        return self._tag_call(TAGS_REMOVE_MUTATION, "tagsRemove", customer_gid, tag_list)

    # ---------------------------------------------------------------- bulk

    def running_bulk(self, kind="MUTATION"):
        """A bulk operation of `kind` already in flight, if any.

        Shopify runs one bulk query and one bulk mutation at a time per shop,
        so a second of either kind is simply rejected. Checking first turns
        that into a clear message instead of a mid-flight failure.
        """
        data = self.graphql(_RUNNING_QUERY)
        nodes = ((data or {}).get("bulkOperations") or {}).get("nodes") or []
        for node in nodes:
            if node.get("type") == kind:
                return node
        return None

    def run_bulk_query(self, query):
        data = self.graphql(_RUN_BULK_QUERY, {"query": query})
        result = (data or {}).get("bulkOperationRunQuery") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise ShopifyError(f"bulkOperationRunQuery: {errors}")
        operation = result.get("bulkOperation")
        if not operation or not operation.get("id"):
            raise ShopifyError("bulkOperationRunQuery returned no bulk operation")
        return operation

    def export_customer_tags(self, poll_timeout=3600):
        """Every customer's current tags, as {customer_id: [tag, ...]}.

        One bulk export instead of ~8,700 single reads. A customer absent from
        the result simply is not in Shopify, which the caller records as a
        per-customer error rather than silently skipping.
        """
        from tags import customer_id_from_gid

        running = self.running_bulk("QUERY")
        if running:
            raise ShopifyError(
                f"a bulk query is already running on this shop "
                f"({running['id']}, {running['status']}); wait for it to finish"
            )
        operation = self.run_bulk_query(CUSTOMER_TAGS_EXPORT)
        self.log(f"  started export {operation['id']}")
        operation = self.poll_bulk(operation["id"], timeout=poll_timeout)
        if operation.get("status") != "COMPLETED":
            raise ShopifyError(
                f"customer tag export ended {operation.get('status')} "
                f"(errorCode={operation.get('errorCode')}); nothing was written"
            )
        url = operation.get("url")
        if not url:
            # A completed export with no file means it matched zero customers.
            return {}

        tags_by_customer = {}
        for line in self.download(url).decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            customer_id = customer_id_from_gid(node.get("id"))
            if customer_id is not None:
                tags_by_customer[customer_id] = list(node.get("tags") or [])
        return tags_by_customer

    def staged_upload_target(self, filename):
        """A staged upload slot for bulk mutation variables.

        resource must be BULK_MUTATION_VARIABLES and the file is JSONL; the
        returned `parameters` are the exact signed form fields the storage
        backend expects.
        """
        data = self.graphql(_STAGED_UPLOAD_MUTATION, {
            "input": [{
                "filename": filename,
                "mimeType": "text/jsonl",
                "resource": "BULK_MUTATION_VARIABLES",
                "httpMethod": "POST",
            }]
        })
        result = (data or {}).get("stagedUploadsCreate") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise ShopifyError(f"stagedUploadsCreate: {errors}")
        targets = result.get("stagedTargets") or []
        if not targets:
            raise ShopifyError("stagedUploadsCreate returned no staged target")
        target = targets[0]
        if not target.get("url"):
            raise ShopifyError("staged target has no upload url")
        return target

    @staticmethod
    def staged_upload_path(target):
        """The value bulkOperationRunMutation wants as stagedUploadPath.

        It is the `key` form parameter - the object path inside the bucket -
        not the target url and not resourceUrl.
        """
        for parameter in target.get("parameters") or []:
            if parameter.get("name") == "key":
                return parameter.get("value")
        raise ShopifyError("staged target has no `key` parameter")

    def upload_jsonl(self, target, content, filename):
        """POST the JSONL to the staged target as multipart/form-data.

        Every signed parameter is sent verbatim and in the order Shopify
        returned it, and the file part goes LAST - the storage backend ignores
        anything after the file part, so a parameter placed after it is simply
        dropped and the signature check fails.
        """
        fields = [(p["name"], p["value"]) for p in target.get("parameters") or []]
        boundary = f"----bhc{uuid.uuid4().hex}"
        crlf = b"\r\n"
        parts = []
        for name, value in fields:
            parts.append(f"--{boundary}".encode() + crlf)
            parts.append(
                f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
            )
            parts.append(str(value).encode() + crlf)
        parts.append(f"--{boundary}".encode() + crlf)
        parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"'
            .encode() + crlf
        )
        parts.append(b"Content-Type: text/jsonl" + crlf + crlf)
        parts.append(content + crlf)
        parts.append(f"--{boundary}--".encode() + crlf)
        body = b"".join(parts)

        request = urllib.request.Request(target["url"], data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                detail = response.read().decode(errors="replace")[:400]
        except urllib.error.HTTPError as exc:
            raise ShopifyError(
                f"staged upload failed: HTTP {exc.code}: "
                f"{exc.read().decode(errors='replace')[:400]}"
            ) from None
        # success_action_status is 201 in the signed policy.
        if status not in (200, 201, 204):
            raise ShopifyError(f"staged upload returned HTTP {status}: {detail}")
        return self.staged_upload_path(target)

    def run_bulk_mutation(self, mutation, staged_path):
        data = self.graphql(_RUN_BULK_MUTATION, {
            "mutation": mutation, "stagedUploadPath": staged_path,
        })
        result = (data or {}).get("bulkOperationRunMutation") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise ShopifyError(f"bulkOperationRunMutation: {errors}")
        operation = result.get("bulkOperation")
        if not operation or not operation.get("id"):
            raise ShopifyError("bulkOperationRunMutation returned no bulk operation")
        return operation

    def poll_bulk(self, operation_id, timeout=3600, interval=5, max_interval=30):
        """Poll one operation by id until it reaches a terminal state.

        By id, deliberately: currentBulkOperation is deprecated and defaults to
        type QUERY, so polling it would watch the wrong operation and report
        COMPLETED off a stale export.
        """
        waited = 0.0
        last_count = None
        while True:
            data = self.graphql(_POLL_QUERY, {"id": operation_id})
            operation = (data or {}).get("bulkOperation")
            if operation is None:
                raise ShopifyError(f"bulk operation {operation_id} disappeared")
            status = operation.get("status")
            count = operation.get("objectCount")
            if count != last_count:
                self.log(f"  bulk {status.lower()}, {count} objects processed")
                last_count = count
            if status in _FINISHED:
                return operation
            if waited >= timeout:
                raise ShopifyError(
                    f"bulk operation {operation_id} still {status} after {timeout}s"
                )
            time.sleep(interval)
            waited += interval
            interval = min(interval * 1.5, max_interval)

    def download(self, url):
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()


def parse_bulk_results(raw, field):
    """Read a bulk mutation results JSONL into per-customer outcomes.

    Returns (confirmed, failures, unrecognized) where `confirmed` is the set of
    customer ids Shopify returned a node for with no userErrors, `failures` maps
    customer id (or "line N" when the line carries no id) to a message, and
    `unrecognized` counts lines that held no recognisable payload at all.

    That last number is the canary for the failure mode the spec warns about: a
    bulk operation that reports COMPLETED while having tagged nothing. Since a
    customer is only ever recorded from a confirmed node id, a results file in
    an unexpected shape records nobody and reports loudly instead of silently
    booking 8,700 successes.
    """
    from tags import customer_id_from_gid

    confirmed, failures, unrecognized = set(), {}, 0
    for lineno, line in enumerate(raw.decode(errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            unrecognized += 1
            continue
        if not isinstance(record, dict):
            unrecognized += 1
            continue

        payload = None
        for container in (record.get("data"), record):
            if isinstance(container, dict) and isinstance(container.get(field), dict):
                payload = container[field]
                break

        label = record.get("__lineNumber", lineno)
        if payload is None:
            message = record.get("message") or record.get("errors")
            if message:
                failures[f"line {label}"] = str(message)[:500]
            else:
                unrecognized += 1
            continue

        customer_id = customer_id_from_gid((payload.get("node") or {}).get("id"))
        errors = payload.get("userErrors") or []
        if errors:
            key = customer_id if customer_id is not None else f"line {label}"
            failures[key] = "; ".join(
                str(error.get("message")) for error in errors
            )[:500]
        elif customer_id is not None:
            confirmed.add(customer_id)
        else:
            unrecognized += 1
    return confirmed, failures, unrecognized
