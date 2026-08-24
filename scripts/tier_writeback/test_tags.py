#!/usr/bin/env python3
"""Unit tests for the parts that are easy to get quietly wrong.

No credentials and no network: the tag scheme, the add/remove diff, the bulk
results parser, and the multipart body the staged upload depends on.

    python3 test_tags.py
"""

import http.server
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tags                                    # noqa: E402
import writeback                               # noqa: E402
from shopify_api import (  # noqa: E402
    CredentialError,
    ShopifyAdmin,
    parse_bulk_results,
)


class TagScheme(unittest.TestCase):
    def test_every_tag_in_the_spec_table_is_mapped(self):
        self.assertEqual(
            set(tags.TIER_TAGS.values()),
            {"bhc-svip-caroline", "bhc-vip-caroline", "bhc-caroline",
             "bhc-vip-betty", "bhc-betty", "bhc-not-tracked"},
        )
        self.assertEqual(
            set(tags.ENGAGEMENT_TAGS.values()),
            {"bhc-active", "bhc-at-risk", "bhc-winback", "bhc-lapsed"},
        )
        self.assertEqual(len(tags.MANAGED_TAGS), 11)

    def test_at_risk_underscore_becomes_hyphen(self):
        # customer_tiers stores "at_risk"; the tag is "bhc-at-risk". A naive
        # "bhc-" + engagement_state would write "bhc-at_risk" instead.
        self.assertEqual(tags.ENGAGEMENT_TAGS["at_risk"], "bhc-at-risk")
        self.assertIn("bhc-at-risk", tags.desired_tags("Backyard Betty", False, "at_risk"))

    def test_desired_tags_is_one_tier_one_engagement_plus_optional_star(self):
        self.assertEqual(
            tags.desired_tags("VIP Backyard Betty", True, "active"),
            frozenset({"bhc-vip-betty", "bhc-active", "bhc-star"}),
        )
        self.assertEqual(
            tags.desired_tags("Not yet tracked", False, "winback"),
            frozenset({"bhc-not-tracked", "bhc-winback"}),
        )

    def test_unknown_values_raise_rather_than_guessing(self):
        with self.assertRaises(tags.UnmappedValue):
            tags.desired_tags("Platinum Caroline", False, "active")
        with self.assertRaises(tags.UnmappedValue):
            tags.desired_tags("Backyard Betty", False, "dormant")


class Diff(unittest.TestCase):
    def test_first_pass_adds_everything_and_removes_nothing(self):
        desired = tags.desired_tags("Backyard Betty", False, "active")
        add, remove = tags.diff([], desired)
        self.assertEqual(add, ["bhc-active", "bhc-betty"])
        self.assertEqual(remove, [])

    def test_climbing_a_tier_removes_the_stale_tier_tag(self):
        # The failure this whole scheme exists to prevent: Betty -> Caroline
        # must not leave the customer wearing both.
        current = ["bhc-betty", "bhc-active"]
        desired = tags.desired_tags("Country Club Caroline", False, "active")
        add, remove = tags.diff(current, desired)
        self.assertEqual(add, ["bhc-caroline"])
        self.assertEqual(remove, ["bhc-betty"])
        after = (set(current) | set(add)) - set(remove)
        self.assertEqual(
            len(after & set(tags.TIER_TAGS.values())), 1,
            "a customer must carry exactly one tier tag",
        )

    def test_engagement_moves_are_also_one_in_one_out(self):
        add, remove = tags.diff(
            ["bhc-caroline", "bhc-active"],
            tags.desired_tags("Country Club Caroline", False, "at_risk"),
        )
        self.assertEqual(add, ["bhc-at-risk"])
        self.assertEqual(remove, ["bhc-active"])

    def test_star_is_independent_of_tier_and_only_drops_when_repeat_goes_false(self):
        add, remove = tags.diff(
            ["bhc-betty", "bhc-active"],
            tags.desired_tags("Backyard Betty", True, "active"),
        )
        self.assertEqual((add, remove), (["bhc-star"], []))

        add, remove = tags.diff(
            ["bhc-betty", "bhc-active", "bhc-star"],
            tags.desired_tags("Backyard Betty", False, "active"),
        )
        self.assertEqual((add, remove), ([], ["bhc-star"]))

    def test_unmanaged_tags_are_never_touched(self):
        current = ["Wholesale", "bhc-betty", "bhc-active", "bhc-handpicked", "VIP"]
        desired = tags.desired_tags("Country Club Caroline", False, "active")
        add, remove = tags.diff(current, desired)
        self.assertEqual(remove, ["bhc-betty"])
        for survivor in ("Wholesale", "bhc-handpicked", "VIP"):
            self.assertNotIn(survivor, remove)

    def test_tag_comparison_is_case_insensitive_but_removal_is_exact(self):
        # Shopify dedupes tags case-insensitively, so "BHC-Betty" already
        # present must not cause a duplicate add, and removing it has to use
        # the casing Shopify actually holds.
        add, remove = tags.diff(
            ["BHC-Betty", "BHC-Active"],
            tags.desired_tags("Backyard Betty", False, "active"),
        )
        self.assertEqual(add, [])
        self.assertEqual(remove, [])

        add, remove = tags.diff(
            ["BHC-Betty"], tags.desired_tags("Country Club Caroline", False, "active")
        )
        self.assertEqual(remove, ["BHC-Betty"])

    def test_already_correct_customer_is_a_no_op(self):
        desired = tags.desired_tags("SVIP Country Club Caroline", True, "winback")
        add, remove = tags.diff(sorted(desired), desired)
        self.assertEqual((add, remove), ([], []))


class StateMirror(unittest.TestCase):
    def test_empty_state_means_nothing_known(self):
        self.assertEqual(tags.tags_from_state(None, None, None), frozenset())

    def test_state_round_trips_through_the_tag_scheme(self):
        self.assertEqual(
            tags.tags_from_state("VIP Country Club Caroline", True, "at_risk"),
            tags.desired_tags("VIP Country Club Caroline", True, "at_risk"),
        )

    def test_gid_round_trip(self):
        self.assertEqual(tags.gid(1865798851), "gid://shopify/Customer/1865798851")
        self.assertEqual(
            tags.customer_id_from_gid("gid://shopify/Customer/1865798851"), 1865798851)
        self.assertIsNone(tags.customer_id_from_gid(None))
        self.assertIsNone(tags.customer_id_from_gid("nonsense"))


class BulkResults(unittest.TestCase):
    def test_confirmed_lines_are_recorded(self):
        raw = b"\n".join([
            json.dumps({"data": {"tagsAdd": {
                "node": {"id": "gid://shopify/Customer/1"}, "userErrors": []}},
                "__lineNumber": 0}).encode(),
            json.dumps({"data": {"tagsAdd": {
                "node": {"id": "gid://shopify/Customer/2"}, "userErrors": []}},
                "__lineNumber": 1}).encode(),
        ])
        confirmed, failures, unrecognized = parse_bulk_results(raw, "tagsAdd")
        self.assertEqual(confirmed, {1, 2})
        self.assertEqual(failures, {})
        self.assertEqual(unrecognized, 0)

    def test_payload_without_a_data_wrapper_is_also_read(self):
        raw = json.dumps({"tagsAdd": {
            "node": {"id": "gid://shopify/Customer/7"}, "userErrors": []}}).encode()
        confirmed, _, unrecognized = parse_bulk_results(raw, "tagsAdd")
        self.assertEqual(confirmed, {7})
        self.assertEqual(unrecognized, 0)

    def test_user_errors_are_failures_not_successes(self):
        raw = json.dumps({"data": {"tagsAdd": {
            "node": {"id": "gid://shopify/Customer/3"},
            "userErrors": [{"field": ["id"], "message": "Customer not found"}]}},
            "__lineNumber": 0}).encode()
        confirmed, failures, _ = parse_bulk_results(raw, "tagsAdd")
        self.assertEqual(confirmed, set())
        self.assertEqual(failures, {3: "Customer not found"})

    def test_an_unexpected_result_shape_confirms_nobody(self):
        # The failure mode the spec warns about: an operation that reports
        # COMPLETED while having tagged nothing. Whatever the file holds, if it
        # carries no confirmed node ids then no customer is recorded as written.
        raw = b"\n".join([
            json.dumps({"somethingElse": {"ok": True}}).encode(),
            b"not json at all",
            json.dumps({"data": {"productUpdate": {"userErrors": []}}}).encode(),
        ])
        confirmed, failures, unrecognized = parse_bulk_results(raw, "tagsAdd")
        self.assertEqual(confirmed, set())
        self.assertEqual(unrecognized, 3)


class _AuthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal stand-in for the token endpoint and the GraphQL endpoint."""

    token_requests = []
    scope = "read_customers,write_customers"
    expires_in = 86399
    fail_next_graphql_with_401 = False
    graphql_tokens = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        if self.path.endswith("/admin/oauth/access_token"):
            _AuthHandler.token_requests.append(
                dict(urllib.parse.parse_qsl(body))
            )
            payload = {
                "access_token": f"shpat_issued_{len(_AuthHandler.token_requests)}",
                "scope": _AuthHandler.scope,
                "expires_in": _AuthHandler.expires_in,
            }
            return self._json(200, payload)

        _AuthHandler.graphql_tokens.append(
            self.headers.get("X-Shopify-Access-Token")
        )
        if _AuthHandler.fail_next_graphql_with_401:
            _AuthHandler.fail_next_graphql_with_401 = False
            return self._json(401, {"errors": "[API] Invalid API key or access token"})
        return self._json(200, {"data": {"shop": {
            "name": "Test Shop", "myshopifyDomain": "test.myshopify.com"}}})

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class ClientCredentialsGrant(unittest.TestCase):
    """Dev Dashboard apps never expose a shpat_ token in the admin.

    Shopify retired admin-created custom apps, so the current path is the
    client credentials grant: exchange client id and secret for a 24-hour
    token, and refresh it when it runs out.
    """

    def setUp(self):
        _AuthHandler.token_requests = []
        _AuthHandler.graphql_tokens = []
        _AuthHandler.scope = "read_customers,write_customers"
        _AuthHandler.expires_in = 86399
        _AuthHandler.fail_next_graphql_with_401 = False
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _AuthHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _client(self, **kwargs):
        api = ShopifyAdmin(shop="test.myshopify.com", log=lambda *_: None, **kwargs)
        api.endpoint = f"{self.base}/admin/api/2026-07/graphql.json"
        api.token_endpoint = f"{self.base}/admin/oauth/access_token"
        return api

    def test_credentials_are_exchanged_for_a_token(self):
        api = self._client(client_id="cid", client_secret="secret")
        shop = api.check_credential()

        self.assertEqual(shop["myshopifyDomain"], "test.myshopify.com")
        self.assertEqual(len(_AuthHandler.token_requests), 1)
        self.assertEqual(_AuthHandler.token_requests[0], {
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "secret",
        })
        self.assertEqual(_AuthHandler.graphql_tokens, ["shpat_issued_1"])

    def test_token_is_cached_across_calls(self):
        api = self._client(client_id="cid", client_secret="secret")
        api.check_credential()
        api.graphql("query shopCheck { shop { name myshopifyDomain } }")
        self.assertEqual(len(_AuthHandler.token_requests), 1)
        self.assertEqual(_AuthHandler.graphql_tokens,
                         ["shpat_issued_1", "shpat_issued_1"])

    def test_an_expired_token_is_refreshed_once_on_401(self):
        # A 24-hour token can lapse in the middle of a long bulk run.
        api = self._client(client_id="cid", client_secret="secret")
        api.check_credential()
        _AuthHandler.fail_next_graphql_with_401 = True
        api.graphql("query shopCheck { shop { name myshopifyDomain } }")

        self.assertEqual(len(_AuthHandler.token_requests), 2)
        self.assertEqual(_AuthHandler.graphql_tokens[-1], "shpat_issued_2")

    def test_an_expired_token_is_refetched_before_the_next_call(self):
        api = self._client(client_id="cid", client_secret="secret")
        api.check_credential()
        self.assertEqual(len(_AuthHandler.token_requests), 1)

        # Wind the clock past the token's life rather than sleeping for it.
        api._token_expires_at = time.monotonic() - 1
        api.graphql("query shopCheck { shop { name myshopifyDomain } }")

        self.assertEqual(len(_AuthHandler.token_requests), 2)
        self.assertEqual(_AuthHandler.graphql_tokens[-1], "shpat_issued_2")

    def test_missing_write_scope_is_fatal_and_named(self):
        _AuthHandler.scope = "read_customers"
        api = self._client(client_id="cid", client_secret="secret")
        with self.assertRaises(CredentialError) as caught:
            api.check_credential()
        self.assertIn("write_customers", str(caught.exception))

    def test_write_customers_alone_is_accepted(self):
        # Shopify collapses the pair: an app granted read+write reads back as
        # write_customers only, because write already implies read. Requiring
        # read_customers literally would reject a working credential.
        _AuthHandler.scope = "write_customers"
        api = self._client(client_id="cid", client_secret="secret")
        self.assertEqual(
            api.check_credential()["myshopifyDomain"], "test.myshopify.com")

    def test_no_customer_scopes_at_all_names_both(self):
        _AuthHandler.scope = "read_products"
        api = self._client(client_id="cid", client_secret="secret")
        with self.assertRaises(CredentialError) as caught:
            api.check_credential()
        self.assertIn("read_customers", str(caught.exception))
        self.assertIn("write_customers", str(caught.exception))

    def test_no_credential_at_all_is_fatal(self):
        api = self._client()
        with self.assertRaises(CredentialError):
            api.check_credential()
        self.assertEqual(_AuthHandler.token_requests, [])

    def test_a_legacy_static_token_still_works_and_is_never_exchanged(self):
        api = self._client(token="shpat_legacy")
        api.check_credential()
        self.assertEqual(_AuthHandler.token_requests, [])
        self.assertEqual(_AuthHandler.graphql_tokens, ["shpat_legacy"])


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    captured = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _CaptureHandler.captured = {
            "content_type": self.headers.get("Content-Type"),
            "body": self.rfile.read(length),
        }
        self.send_response(201)
        self.end_headers()
        self.wfile.write(b"")

    def log_message(self, *_args):
        pass


class StagedUploadBody(unittest.TestCase):
    """The staged upload signature depends on exact form-field composition.

    The storage backend ignores everything after the file part, so a signed
    parameter emitted after it is dropped and the upload is rejected. These
    parameter names are the ones a real stagedUploadsCreate call returned for
    resource BULK_MUTATION_VARIABLES.
    """

    PARAMETERS = [
        {"name": "Content-Type", "value": "text/jsonl"},
        {"name": "success_action_status", "value": "201"},
        {"name": "acl", "value": "private"},
        {"name": "key", "value": "tmp/6421903/bulk/abc/bhc_tier_tags_add.jsonl"},
        {"name": "x-goog-date", "value": "20260822T041940Z"},
        {"name": "x-goog-credential", "value": "merchant-assets@example/goog4_request"},
        {"name": "x-goog-algorithm", "value": "GOOG4-RSA-SHA256"},
        {"name": "x-goog-signature", "value": "deadbeef"},
        {"name": "policy", "value": "eyJjb25kaXRpb25zIjpbXX0="},
    ]

    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_staged_upload_path_is_the_key_parameter(self):
        target = {"url": self.url, "parameters": self.PARAMETERS}
        self.assertEqual(
            ShopifyAdmin.staged_upload_path(target),
            "tmp/6421903/bulk/abc/bhc_tier_tags_add.jsonl",
        )

    def test_every_parameter_is_sent_in_order_with_the_file_last(self):
        api = ShopifyAdmin("example.myshopify.com", "token", log=lambda *_: None)
        target = {"url": self.url, "parameters": self.PARAMETERS}
        content = b'{"id":"gid://shopify/Customer/1","tags":["bhc-betty"]}\n'

        path = api.upload_jsonl(target, content, "bhc_tier_tags_add.jsonl")
        self.assertEqual(path, "tmp/6421903/bulk/abc/bhc_tier_tags_add.jsonl")

        body = _CaptureHandler.captured["body"].decode()
        self.assertIn("multipart/form-data; boundary=",
                      _CaptureHandler.captured["content_type"])

        positions = []
        for parameter in self.PARAMETERS:
            marker = 'name="%s"' % parameter["name"]
            self.assertIn(marker, body, f"{parameter['name']} missing from the form")
            self.assertIn(parameter["value"], body)
            positions.append(body.index(marker))

        self.assertEqual(positions, sorted(positions),
                         "signed parameters must keep the order Shopify returned")
        self.assertGreater(body.index('name="file"'), max(positions),
                           "the file part must come last")
        self.assertIn(content.decode(), body)


class CredentialPrecedence(unittest.TestCase):
    """A stale SHOPIFY_ADMIN_TOKEN must not shadow the client credentials."""

    def test_client_credentials_win_when_both_are_set(self):
        token, cid, secret, note = writeback.choose_credentials({
            "SHOPIFY_ADMIN_TOKEN": "shpat_stale",
            "SHOPIFY_CLIENT_ID": "cid",
            "SHOPIFY_CLIENT_SECRET": "secret",
        })
        self.assertIsNone(token)
        self.assertEqual((cid, secret), ("cid", "secret"))
        self.assertIn("unset SHOPIFY_ADMIN_TOKEN", note)

    def test_a_lone_static_token_is_still_used(self):
        token, cid, secret, note = writeback.choose_credentials(
            {"SHOPIFY_ADMIN_TOKEN": "shpat_only"})
        self.assertEqual(token, "shpat_only")
        self.assertEqual((cid, secret, note), (None, None, None))

    def test_a_half_set_client_credential_does_not_displace_the_token(self):
        token, _cid, _secret, note = writeback.choose_credentials({
            "SHOPIFY_ADMIN_TOKEN": "shpat_only",
            "SHOPIFY_CLIENT_ID": "cid",          # secret missing
        })
        self.assertEqual(token, "shpat_only")
        self.assertIsNone(note)

    def test_empty_strings_count_as_unset(self):
        token, cid, secret, _note = writeback.choose_credentials({
            "SHOPIFY_ADMIN_TOKEN": "", "SHOPIFY_CLIENT_ID": "",
            "SHOPIFY_CLIENT_SECRET": "",
        })
        self.assertEqual((token, cid, secret), (None, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
