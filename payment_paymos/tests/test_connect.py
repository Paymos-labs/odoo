import json
import os
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.payment_paymos.models import payment_provider
from odoo.addons.payment_paymos.services import connect, sdk


_SOURCE = "https://store-b.example"
_DEVICE_SECRET = "mock-device-secret"
_OAUTH_TOKEN = "mock-short-lived-bearer"
_KEY_ENV = "PAYMOS_ODOO_ENVELOPE_KEY"


def _response(status, payload):
    return sdk.HttpResponse(status, json.dumps(payload).encode("utf-8"))


def _device():
    return {
        "device_code": _DEVICE_SECRET,
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://app.paymos.io/connect/cms/verify",
        "verification_uri_complete": "https://app.paymos.io/connect/cms/verify?user_code=ABCD-EFGH",
        "expires_in": 600,
        "interval": 5,
    }


def _approved():
    def environment(stage, enabled):
        return {
            "base_url": "https://api.paymos.io",
            "project_id": "prj_mock",
            "api_key": f"pk_{stage}_mock",
            "api_secret": f"sk_{stage}_mock",
            "webhook_secret": f"whsec_{stage}_mock",
            "capabilities": ["invoice_refunds_v1"],
            "refund_contract_version": 1,
            "refunds_enabled": enabled,
        }

    return {
        "access_token": _OAUTH_TOKEN,
        "token_type": "Bearer",
        "expires_in": 120,
        "paymos_config": {
            "schema": 1,
            "plugin": "odoo",
            "source_url": _SOURCE,
            "environments": {
                "sandbox": environment("test", True),
                "live": environment("live", False),
            },
        },
    }


@tagged("post_install", "-at_install")
class TestPaymosConnect(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider_a = cls.env.ref("payment_paymos.payment_provider_paymos")
        cls.company_b = cls.env["res.company"].create({
            "name": "Paymos Connect Company B", "parent_id": cls.provider_a.company_id.id,
        })
        cls.provider_b = cls.provider_a.copy({"company_id": cls.company_b.id})
        admin_values = {
            "groups": "base.group_user,base.group_system",
            "company_id": cls.company_b.id,
            "company_ids": [Command.set((cls.provider_a.company_id | cls.company_b).ids)],
        }
        cls.admin_b = new_test_user(cls.env, login="paymos-connect-admin-b", **admin_values)
        cls.other_admin_b = new_test_user(cls.env, login="paymos-connect-admin-other", **admin_values)
        cls.employee_b = new_test_user(
            cls.env, login="paymos-connect-employee", company_id=cls.company_b.id,
            company_ids=[Command.set(cls.company_b.ids)],
        )

    def setUp(self):
        super().setUp()
        key = patch.dict(os.environ, {_KEY_ENV: Fernet.generate_key().decode("ascii")})
        key.start()
        self.addCleanup(key.stop)
        self.now = 1000
        self.binding = connect.DeviceBinding(
            self.provider_b.id, self.company_b.id, self.env.uid, _SOURCE, "mock-session-b",
        )

    def _client(self, *responses):
        transport = Mock(side_effect=responses)
        return connect.DeviceConnect(transport=transport, clock=lambda: self.now), transport

    def test_bound_device_and_credential_contract(self):
        client, transport = self._client(_response(200, _device()), _response(200, _approved()))
        challenge = client.start(self.binding)
        self.assertEqual(challenge.expires_at, 1600)
        self.assertEqual(challenge.retry_after, 5)
        self.assertNotIn(_DEVICE_SECRET, challenge.envelope)
        self.assertNotIn(challenge.user_code, repr(challenge))
        self.assertNotIn(challenge.envelope, repr(challenge))
        approval = urlsplit(challenge.verification_url)
        self.assertEqual((approval.scheme, approval.netloc, approval.path), (
            "https", "app.paymos.io", "/connect/cms/verify",
        ))
        self.assertEqual(parse_qs(approval.query), {
            "cms": ["odoo"], "source_url": [_SOURCE], "user_code": ["ABCD-EFGH"],
        })
        method, url, headers, body, timeout = transport.call_args.args
        self.assertEqual((method, url), ("POST", "https://app.paymos.io/connect/cms/device"))
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(parse_qs(body.decode("ascii")), {
            "client_id": ["paymos-cms"], "scope": ["cms:bootstrap"],
        })
        self.assertEqual(timeout, sdk.HTTP_TIMEOUT)
        self.assertEqual(transport.call_args.kwargs["max_bytes"], 65536)

        self.now += 5
        invalid_bindings = (
            replace(self.binding, company_id=self.provider_a.company_id.id, provider_id=self.provider_a.id),
            replace(self.binding, provider_id=self.provider_a.id),
            replace(self.binding, user_id=self.binding.user_id + 1),
            replace(self.binding, session_id="mock-foreign-session"),
            replace(self.binding, source_url="https://store-a.example"),
        )
        for foreign in invalid_bindings:
            with self.assertRaisesRegex(connect.ConnectError, "^binding_mismatch$"):
                client.poll(foreign, challenge.envelope, challenge.flow_id)
        with self.assertRaisesRegex(connect.ConnectError, "^binding_mismatch$"):
            client.poll(self.binding, challenge.envelope, "ab" * 16)
        self.assertEqual(transport.call_count, 1)

        result = client.poll(self.binding, challenge.envelope, challenge.flow_id)
        self.assertEqual(result.status, "connected")
        self.assertEqual(transport.call_args.args[:2], ("POST", "https://app.paymos.io/connect/token"))
        self.assertEqual(parse_qs(transport.call_args.args[3].decode("ascii")), {
            "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
            "device_code": [_DEVICE_SECRET],
            "client_id": ["paymos-cms"],
        })
        self.assertEqual(set(result.credentials), {"sandbox", "live"})
        sandbox = result.credentials["sandbox"]
        live = result.credentials["live"]
        self.assertEqual(sandbox.project_id, live.project_id)
        self.assertTrue(sandbox.refunds_enabled)
        self.assertFalse(live.refunds_enabled)
        self.assertEqual(sandbox.refund_contract_version, 1)
        self.assertNotIn(_OAUTH_TOKEN, repr(result))
        self.assertNotIn(sandbox.api_secret, repr(sandbox))
        self.assertNotIn(sandbox.webhook_secret, repr(sandbox))
        self.assertFalse(self.provider_a.paymos_active_connection_id)
        self.assertFalse(self.provider_b.paymos_active_connection_id)

        binding = {
            "provider_id": self.provider_b.id, "company_id": self.company_b.id,
            "generation": 2, "source_url": _SOURCE,
        }
        sealed = connect.seal_credentials(sandbox, **binding)
        self.assertNotIn(sandbox.api_secret, sealed)
        self.assertNotIn(_OAUTH_TOKEN, sealed)
        binding.update(environment="sandbox", project_id="prj_mock")
        self.assertEqual(connect.open_credentials(sealed, **binding), sandbox)
        for change in (
            {"company_id": self.provider_a.company_id.id},
            {"provider_id": self.provider_a.id},
            {"generation": 3},
            {"environment": "live"},
            {"project_id": "prj_other"},
            {"source_url": "https://store-a.example"},
        ):
            with self.assertRaisesRegex(connect.ConnectError, "^binding_mismatch$"):
                connect.open_credentials(sealed, **(binding | change))
        with self.assertRaisesRegex(connect.ConnectError, "^invalid_envelope$"):
            connect.open_credentials(sealed[:-8], **binding)

    def test_pending_slow_down_and_timeout_keep_absolute_expiry(self):
        client, transport = self._client(
            _response(200, _device()),
            _response(400, {"error": "authorization_pending"}),
            _response(400, {"error": "slow_down"}),
            sdk.PaymosTransportError("mock transport error with a secret"),
        )
        challenge = client.start(self.binding)
        self.now += 4
        waiting = client.poll(self.binding, challenge.envelope, challenge.flow_id)
        self.assertEqual((waiting.status, waiting.retry_after), ("waiting", 1))
        self.assertEqual(waiting.envelope, challenge.envelope)
        self.assertEqual(transport.call_count, 1)

        self.now += 1
        waiting = client.poll(self.binding, waiting.envelope, challenge.flow_id)
        self.assertEqual((waiting.status, waiting.retry_after), ("waiting", 5))
        self.now += 5
        waiting = client.poll(self.binding, waiting.envelope, challenge.flow_id)
        self.assertEqual((waiting.status, waiting.retry_after), ("waiting", 10))
        self.now += 10
        waiting = client.poll(self.binding, waiting.envelope, challenge.flow_id)
        self.assertEqual((waiting.status, waiting.retry_after), ("waiting", 20))
        self.assertEqual(transport.call_count, 4)
        self.now = challenge.expires_at
        self.assertEqual(client.poll(self.binding, waiting.envelope, challenge.flow_id).status, "expired")
        self.assertEqual(transport.call_count, 4)

    def test_expired_or_denied_cannot_return_credentials(self):
        for error, expected in (
            ("expired_token", "expired"), ("invalid_grant", "expired"), ("access_denied", "denied"),
        ):
            with self.subTest(error=error):
                client, _transport = self._client(_response(200, _device()), _response(400, {"error": error}))
                challenge = client.start(self.binding)
                self.now += challenge.retry_after
                result = client.poll(self.binding, challenge.envelope, challenge.flow_id)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.envelope)
                self.assertIsNone(result.credentials)

    def test_rejects_untrusted_device_response_and_missing_encryption_key(self):
        for bad_url in (
            "https://app.paymos.io.evil.test/connect/cms/verify?user_code=ABCD-EFGH",
            "https://app.paymos.io@evil.test/connect/cms/verify?user_code=ABCD-EFGH",
            "https://app.paymos.io/evil?user_code=ABCD-EFGH",
            "https://app.paymos.io/connect/cms/verify?user_code=ABCD-EFGH&user_code=OTHER",
            "https://app.paymos.io/connect/cms/verify?user_code=OTHER",
        ):
            with self.subTest(url=bad_url):
                response = _response(200, _device() | {"verification_uri_complete": bad_url})
                client, _transport = self._client(response)
                with self.assertRaisesRegex(connect.ConnectError, "^invalid_approval_url$"):
                    client.start(self.binding)

        client, transport = self._client(_response(200, _device()))
        with patch.dict(os.environ, {_KEY_ENV: ""}):
            with self.assertRaisesRegex(connect.ConnectError, "^encryption_key_unavailable$"):
                client.start(self.binding)
        transport.assert_not_called()
        with self.assertRaisesRegex(connect.ConnectError, "^invalid_source$"):
            client.start(replace(self.binding, source_url="http://store-b.example"))
        transport.assert_not_called()
        self.assertEqual(connect.canonical_source("https://STORE-B.EXAMPLE:443/"), _SOURCE)

    def test_rejects_invalid_approval_and_keeps_remote_errors_redacted(self):
        for path, value, expected in (
            (("access_token",), False, "invalid_token"),
            (("token_type",), "Basic", "invalid_token"),
            (("refresh_token",), "mock-unexpected-refresh-token", "invalid_token"),
            (("expires_in",), False, "invalid_token"),
            (("paymos_config", "schema"), True, "invalid_response"),
            (("paymos_config", "source_url"), "https://store-a.example", "binding_mismatch"),
            (("paymos_config", "plugin"), "woocommerce", "binding_mismatch"),
            (("paymos_config", "environments", "sandbox", "api_key"), "pk_live_mock", "invalid_credentials"),
            (("paymos_config", "environments", "live", "webhook_secret"), "whsec_test_mock", "invalid_credentials"),
            (("paymos_config", "environments", "sandbox", "base_url"), "https://evil.test", "invalid_credentials"),
            (("paymos_config", "environments", "sandbox", "project_id"), "prj_other", "binding_mismatch"),
        ):
            with self.subTest(path=path):
                approved = _approved()
                section = approved
                for key in path[:-1]:
                    section = section[key]
                section[path[-1]] = value
                client, _transport = self._client(_response(200, _device()), _response(200, approved))
                challenge = client.start(self.binding)
                self.now += challenge.retry_after
                with self.assertRaisesRegex(connect.ConnectError, f"^{expected}$"):
                    client.poll(self.binding, challenge.envelope, challenge.flow_id)

        for response, expected in (
            (_response(400, {"error": "invalid_request", "error_description": _DEVICE_SECRET}),
             "connection_unavailable"),
            (sdk.HttpResponse(200, b'{"token_type":"Bearer","token_type":"Basic"}'), "invalid_response"),
            (sdk.HttpResponse(302, _DEVICE_SECRET.encode("utf-8")), "connection_unavailable"),
        ):
            client, _transport = self._client(_response(200, _device()), response)
            challenge = client.start(self.binding)
            self.now += challenge.retry_after
            with self.assertRaisesRegex(connect.ConnectError, f"^{expected}$"):
                client.poll(self.binding, challenge.envelope, challenge.flow_id)

    def test_sdk_transport_and_checkout_url_are_restricted(self):
        credentials = connect.Credentials(
            "prj_mock", "sandbox", "pk_test_mock", "sk_test_mock", "whsec_test_mock",
            (), None, False,
        )
        vendor = SimpleNamespace(VERSION="1.6.0", Paymos=Mock(), Response=Mock(), WebhookVerifier=Mock())
        with patch.object(sdk, "import_module", return_value=vendor) as load:
            sdk.make_client(credentials)
            load.assert_called_once_with(".._vendor.paymos", package=sdk.__package__)
            self.assertEqual(vendor.Paymos.call_args.kwargs["base_url"], "https://api.paymos.io")
            self.assertEqual(vendor.Paymos.call_args.kwargs["max_retries"], 0)
            sdk.webhook_verifier(credentials)
            vendor.WebhookVerifier.assert_called_once_with(credentials.webhook_secret)

        transport = vendor.Paymos.call_args.kwargs["transport"]
        remote = _response(409, {"code": "idempotency_conflict", "detail": "sk_test_mock"})
        with patch.object(sdk, "request_https", return_value=remote):
            transport("POST", "https://api.paymos.io/v1/invoices", {}, b"{}", sdk.HTTP_TIMEOUT)
        self.assertEqual(vendor.Response.call_args.args[0], 409)
        self.assertEqual(json.loads(vendor.Response.call_args.args[1])["code"], "idempotency_conflict")
        self.assertNotIn(credentials.api_secret, vendor.Response.call_args.args[1])

        good_checkout = "https://checkout.paymos.io/invoice/test"
        self.assertEqual(sdk.validate_checkout_url(good_checkout), good_checkout)
        for url in (
            "https://checkout.paymos.io.evil.test/invoice/test",
            "https://checkout.paymos.io@evil.test/invoice/test",
            "http://checkout.paymos.io/invoice/test",
            "https://checkout.paymos.io//evil.test/invoice",
        ):
            with self.assertRaises(ValueError):
                sdk.validate_checkout_url(url)

    def test_https_transport_refuses_redirect_and_limits_response(self):
        url = "https://app.paymos.io/connect/cms/device"
        origin = "https://app.paymos.io"
        redirect = HTTPError(url, 302, "Found", {"Location": "https://evil.test/"}, BytesIO(b"secret"))
        with patch.object(sdk, "build_opener") as opener:
            opener.return_value.open.side_effect = redirect
            result = sdk.request_https("POST", url, {}, b"{}", 8, origin=origin, max_bytes=32)
        opener.assert_called_once_with(sdk._NoRedirect)
        self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertEqual((result.status, result.body), (302, b""))

        response = BytesIO(b"body longer than maximum")
        response.status = 200
        with patch.object(sdk, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(sdk.PaymosTransportError, "^Paymos HTTPS response exceeded its limits"):
                sdk.request_https("POST", url, {}, b"{}", 8, origin=origin, max_bytes=4)

    def _request_session(self, user, session_id="mock-provider-session"):
        return patch.object(payment_provider, "request", SimpleNamespace(
            session=SimpleNamespace(uid=user.id, sid=session_id),
            httprequest=SimpleNamespace(url_root="https://spoofed-host.example"),
        ))

    def _provider_connection(self, provider, approved):
        client, transport = self._client(_response(200, _device()), _response(200, approved))
        with self._request_session(provider.env.user), patch.object(connect, "DeviceConnect", return_value=client):
            action = provider.action_paymos_connect()
            params = action["params"]
            self.assertEqual(action["tag"], "payment_paymos.connect")
            self.assertEqual(set(params), {
                "provider_id", "flow_id", "user_code", "verification_url", "approval_origin", "retry_after",
            })
            self.assertNotIn(_DEVICE_SECRET, json.dumps(action))
            self.assertEqual(parse_qs(urlsplit(params["verification_url"]).query)["source_url"], [_SOURCE])
            self.now += params["retry_after"]
            result = provider.action_paymos_poll(params["flow_id"])
            self.assertEqual(provider.action_paymos_poll(params["flow_id"]), {"status": "expired"})
        self.assertEqual(transport.call_count, 2)
        return result

    def _company_admin_provider(self):
        return self.provider_b.with_user(self.admin_b).with_context(allowed_company_ids=self.company_b.ids)

    def test_provider_saves_exact_binding(self):
        self.env["ir.config_parameter"].sudo().set_str("web.base.url", _SOURCE)
        provider = self._company_admin_provider()
        self.assertEqual(self._provider_connection(provider, _approved()), {"status": "connected"})
        self.assertEqual(provider.paymos_connection_status, "connected")
        self.assertEqual(provider.paymos_project_alias, "…j_mock")
        self.assertFalse(provider.active)
        self.assertFalse(provider.is_published)
        self.assertFalse(provider.is_live)
        first = provider.sudo().paymos_active_connection_id
        self.assertEqual((first.project_id, first.generation, first.environment), ("prj_mock", 1, "sandbox"))
        self.assertEqual(first.company_id, self.company_b)
        self.assertEqual(first.provider_id, self.provider_b)
        self.assertEqual(first.source_url, _SOURCE)
        self.assertFalse(provider.sudo().paymos_pending_envelope)
        self.assertFalse(provider._paymos_checkout_connection())

        with self.assertRaises(AccessError):
            provider.write({"is_live": True})
        with self.assertRaises(AccessError):
            provider.write({"paymos_active_connection_id": first.id})
        with self._request_session(self.admin_b):
            provider.action_paymos_use_live()
        first_live = provider.sudo().paymos_active_connection_id
        self.assertEqual((first_live.project_id, first_live.generation, first_live.environment),
                         ("prj_mock", 1, "live"))
        self.assertTrue(provider.is_live)
        self.assertFalse(provider.active)
        self.assertFalse(provider.is_published)
        provider.write({"active": True, "is_published": True})
        self.assertEqual(provider._paymos_checkout_connection(), first_live)

        approved = _approved()
        approved["paymos_config"]["environments"]["sandbox"]["webhook_secret"] = "whsec_test_reconnected"
        self.assertEqual(self._provider_connection(provider, approved), {"status": "connected"})
        current = provider.sudo().paymos_active_connection_id
        self.assertEqual((current.generation, current.environment), (2, "sandbox"))
        self.assertFalse(provider.is_live)
        self.assertFalse(provider.active)
        self.assertFalse(provider.is_published)
        original = connect.open_credentials(
            first.credentials_envelope, provider_id=provider.id, company_id=self.company_b.id,
            generation=1, source_url=_SOURCE, environment="sandbox", project_id="prj_mock",
        )
        self.assertEqual(original.webhook_secret, "whsec_test_mock")
        self.assertTrue(first_live.exists())

        foreign_project = _approved()
        for credential in foreign_project["paymos_config"]["environments"].values():
            credential["project_id"] = "prj_different"
        self.assertEqual(self._provider_connection(provider, foreign_project), {"status": "conflict"})
        self.assertEqual(provider.sudo().paymos_active_connection_id, current)
        with self._request_session(self.admin_b):
            provider.action_paymos_disconnect()
        self.assertEqual(provider.paymos_connection_status, "disconnected")
        self.assertFalse(provider.sudo().paymos_active_connection_id)
        self.assertFalse(provider._paymos_checkout_connection())
        self.assertEqual(self._provider_connection(provider, foreign_project), {"status": "conflict"})
        self.assertFalse(provider.sudo().paymos_active_connection_id)
        rows = self.env["paymos.connection"].search([("provider_id", "=", provider.id)])
        self.assertEqual(set(rows.mapped("generation")), {1, 2})
        self.assertEqual(len(rows), 4)

    def test_provider_requires_exact_company_user_session_and_source(self):
        parameters = self.env["ir.config_parameter"].sudo()
        parameters.set_str("web.base.url", _SOURCE)
        provider = self._company_admin_provider()
        client, transport = self._client(_response(200, _device()), _response(200, _approved()))
        with patch.object(connect, "DeviceConnect", return_value=client):
            foreign_provider = self.provider_a.with_user(self.admin_b).with_context(
                allowed_company_ids=self.company_b.ids,
            )
            # Odoo's native provider rule permits the parent company, so the exact check matters.
            foreign_provider.check_access("write")
            with self._request_session(self.admin_b):
                with self.assertRaises(AccessError):
                    foreign_provider.action_paymos_connect()
            employee = provider.with_user(self.employee_b)
            with self._request_session(self.employee_b):
                with self.assertRaises(AccessError):
                    employee.action_paymos_connect()
            transport.assert_not_called()

            with self._request_session(self.admin_b):
                params = provider.action_paymos_connect()["params"]
            self.now += params["retry_after"]
            with self._request_session(self.other_admin_b):
                with self.assertRaises(UserError):
                    provider.with_user(self.other_admin_b).action_paymos_poll(params["flow_id"])
            with self._request_session(self.admin_b, "changed-session"):
                with self.assertRaises(UserError):
                    provider.action_paymos_poll(params["flow_id"])
            with self._request_session(self.admin_b):
                with self.assertRaises(UserError):
                    provider.action_paymos_poll("0" * 32)
                parameters.set_str("web.base.url", "https://changed-domain.example")
                with self.assertRaises(UserError):
                    provider.action_paymos_poll(params["flow_id"])
                self.assertEqual(transport.call_count, 1)
                parameters.set_str("web.base.url", _SOURCE)
                self.assertEqual(provider.action_paymos_poll(params["flow_id"]), {"status": "connected"})
            self.assertEqual(transport.call_count, 2)

    def test_native_provider_identity_stays_immutable_before_connect(self):
        provider = self._company_admin_provider()
        self.assertFalse(provider.sudo().paymos_active_connection_id)
        transaction = self.env["payment.transaction"].create({
            "provider_id": provider.id,
            "payment_method_id": self.provider_b.payment_method_ids.id,
            "reference": "Paymos Connect Identity Draft",
            "partner_id": self.company_b.partner_id.id,
            "currency_id": self.company_b.currency_id.id,
            "amount": 1,
            "operation": "online_redirect",
        })
        with self.assertRaises(UserError):
            provider.write({"code": "none"})
        with self.assertRaises(UserError):
            provider.write({"company_id": self.provider_a.company_id.id})
        self.assertEqual((transaction.state, transaction.provider_code), ("draft", "paymos"))
        self.assertEqual(transaction.company_id, self.company_b)

        ordinary = provider.env["payment.provider"].create({
            "name": "Ordinary Provider Identity", "code": "none", "company_id": self.company_b.id,
        })
        with self.assertRaises(UserError):
            ordinary.write({"code": "paymos"})
        ordinary.write({"name": "Ordinary Provider Renamed"})
        self.assertEqual(ordinary.name, "Ordinary Provider Renamed")

        created = provider.env["payment.provider"].with_context(
            default_code="paymos", default_active=True, default_is_live=True, default_is_published=True,
        ).create({"name": "New Paymos Provider Identity", "company_id": self.company_b.id})
        self.assertEqual(created.code, "paymos")
        self.assertFalse(created.active)
        self.assertFalse(created.is_published)
        self.assertFalse(created.is_live)

    def test_native_publication_requires_exact_company_and_current_binding(self):
        parameters = self.env["ir.config_parameter"].sudo()
        parameters.set_str("web.base.url", _SOURCE)
        provider = self._company_admin_provider()
        parent = self.provider_a.with_user(self.admin_b).with_context(
            allowed_company_ids=self.provider_a.company_id.ids,
        )
        excluded_parent = parent.with_context(allowed_company_ids=self.company_b.ids)
        excluded_parent.check_access("write")
        self.assertEqual(self._provider_connection(parent, _approved()), {"status": "connected"})

        for field in ("active", "is_published"):
            with self.assertRaises(AccessError):
                excluded_parent.write({field: True})
            with self.assertRaises(UserError):
                provider.write({field: True})
        self.assertFalse(provider.active)
        self.assertFalse(provider.is_published)

        self.assertEqual(self._provider_connection(provider, _approved()), {"status": "connected"})
        parent.write({"active": True, "is_published": True})
        provider.write({"active": True, "is_published": True})
        provider_model = provider.env["payment.provider"].sudo()
        available = provider_model._find_available_providers(
            self.company_b.id, self.company_b.partner_id.id, 1.0,
            currency_id=self.company_b.currency_id.id,
        )
        self.assertIn(provider.id, available.ids)
        self.assertNotIn(parent.id, available.ids)

        parameters.set_str("web.base.url", "https://stale-origin.example")
        available = provider_model._find_available_providers(
            self.company_b.id, self.company_b.partner_id.id, 1.0,
            currency_id=self.company_b.currency_id.id,
        )
        self.assertNotIn(provider.id, available.ids)
        provider.write({"active": False, "is_published": False})
        for field in ("active", "is_published"):
            with self.assertRaises(UserError):
                provider.write({field: True})
        self.assertFalse(provider.active)
        self.assertFalse(provider.is_published)

    def test_native_paymos_redirect_view_without_validation(self):
        provider = self._company_admin_provider()
        template = self.env.ref("payment_paymos.redirect_form")
        self.assertEqual(provider._get_redirect_form_view(), template)
        self.assertFalse(provider._get_redirect_form_view(is_validation=True))
