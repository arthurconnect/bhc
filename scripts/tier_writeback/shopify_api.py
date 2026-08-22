"""Shopify Admin API client: single-customer tagging and the bulk import path.

Every GraphQL operation here was validated against the live 2026-07 Admin
schema, and the staged-upload parameter names were read off a real
stagedUploadsCreate response rather than taken from the build spec. See the
README's "Verified against the live API" section.
"""

import json
import time
import urllib.error
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
    def __init__(self, shop, token, api_version=DEFAULT_API_VERSION, timeout=120,
                 max_retries=5, log=print):
        self.shop = normalize_shop(shop)
        self.token = token
        self.api_version = api_version
        self.timeout = timeout
        self.max_retries = max_retries
        self.log = log
        self.endpoint = f"https://{self.shop}/admin/api/{api_version}/graphql.json"

    # ------------------------------------------------------------- transport

    def graphql(self, query, variables=None):
        """POST a GraphQL document. Retries throttling and transient 5xx."""
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        delay = 1.0
        last = None
        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(self.endpoint, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Shopify-Access-Token", self.token)
            request.add_header("Accept", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code in (401, 403):
                    raise CredentialError(
                        f"Shopify rejected the Admin API token ({exc.code}): {detail}"
                    ) from None
                if exc.code == 404:
                    raise CredentialError(
                        f"No Admin API at {self.endpoint} (404). Check SHOPIFY_SHOP "
                        f"and SHOPIFY_API_VERSION."
                    ) from None
                if exc.code == 429 or exc.code >= 500:
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
        """Prove the token works before anything else happens. Fatal on failure."""
        if not self.shop:
            raise CredentialError("SHOPIFY_SHOP is not set")
        if not self.token:
            raise CredentialError("SHOPIFY_ADMIN_TOKEN is not set")
        data = self.graphql(_SHOP_QUERY)
        shop = (data or {}).get("shop")
        if not shop or not shop.get("myshopifyDomain"):
            raise CredentialError(f"unexpected response to shop query: {data}")
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

    def running_bulk_mutation(self):
        """A MUTATION bulk operation already in flight, if any.

        Shopify runs one bulk mutation at a time per shop, so starting a second
        would simply be rejected. Checking first turns that into a clear message.
        """
        data = self.graphql(_RUNNING_QUERY)
        nodes = ((data or {}).get("bulkOperations") or {}).get("nodes") or []
        for node in nodes:
            if node.get("type") == "MUTATION":
                return node
        return None

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
