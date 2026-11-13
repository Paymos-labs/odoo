import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from lxml import html

from odoo import Command
from odoo.addons.http_routing.tests.common import MockRequest
from odoo.addons.payment.controllers.payment_status import PaymentStatus
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.payment_paymos.services import connect, money, payment, sdk


_SOURCE = "https://odoo-checkout.example"
_PROJECT = "prj_checkout"
_INVOICE_ID = "inv_checkout"
_PAYMENT_URL = f"https://checkout.paymos.io/invoice/{_INVOICE_ID}"


@tagged("post_install", "-at_install")
class TestPaymosCheckout(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_paymos.payment_provider_paymos")
        cls.method = cls.env.ref("payment_paymos.payment_method_paymos")
        cls.website = cls.env.ref("base.default_website")
        cls.company = cls.provider.company_id
        cls.currency = cls.env.ref("base.EUR")
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Paymos Checkout EUR", "currency_id": cls.currency.id, "company_id": cls.company.id,
        })
        cls.partner = cls.env["res.partner"].create({
            "name": "Private Checkout Customer",
            "email": "paymos-checkout-customer@example.test",
        })
        cls.product = cls.env["product.product"].create({
            "name": "Paymos Checkout Service", "type": "service", "list_price": 12.34,
        })

    def setUp(self):
        super().setUp()
        self.authenticate(None, None)
        self.startPatcher(patch.dict(os.environ, {
            "PAYMOS_ODOO_ENVELOPE_KEY": Fernet.generate_key().decode("ascii"),
        }))
        self.env["ir.config_parameter"].sudo().set_str("web.base.url", _SOURCE)
        credentials = connect.Credentials(
            _PROJECT, "sandbox", "pk_test_checkout", "sk_test_checkout", "whsec_test_checkout",
            (), None, False,
        )
        binding = {
            "provider_id": self.provider.id,
            "company_id": self.company.id,
            "generation": 1,
            "source_url": _SOURCE,
        }
        self.connection = self.env["paymos.connection"].create(dict(
            binding, project_id=_PROJECT, environment="sandbox",
            credentials_envelope=connect.seal_credentials(credentials, **binding),
        ))
        self.provider.write({
            "paymos_active_connection_id": self.connection.id, "active": True, "is_published": True,
        })
        self.assertTrue(self.method.active)
        self.tx_sequence = 0

    def _order(self):
        order = self.env["sale.order"].with_company(self.company).create({
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "website_id": self.website.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [Command.create({
                "product_id": self.product.id,
                "product_uom_qty": 1,
                "price_unit": 12.34,
                "tax_ids": [Command.clear()],
            })],
        })
        self.assertEqual(order.currency_id, self.currency)
        self.assertEqual(order.amount_total, 12.34)
        return order

    def _transaction(self, order, amount=12.34):
        self.tx_sequence += 1
        return self.env["payment.transaction"].create({
            "provider_id": self.provider.id,
            "payment_method_id": self.method.id,
            "reference": f"Paymos-Checkout-{self.tx_sequence}",
            "partner_id": order.partner_invoice_id.id,
            "currency_id": order.currency_id.id,
            "amount": amount,
            "operation": "online_redirect",
            "sale_order_ids": [Command.set(order.ids)],
        })

    def _render(self, transaction):
        with MockRequest(self.env, website=self.website) as mock_request:
            mock_request.session.sid = self.session.sid
            mock_request.csrf_token = self.csrf_token
            processing = transaction._get_processing_values()
            monitored_tx = mock_request.session[PaymentStatus.MONITORED_TX_ID_KEY]
            self.assertEqual(mock_request.session["__website_sale_last_tx_id"], monitored_tx)
        form = html.fromstring(processing["redirect_form_html"])
        self.assertEqual((form.attrib["method"], form.attrib["action"]), ("post", payment.START_URL))
        values = {element.attrib["name"]: element.attrib["value"] for element in form.xpath(".//input")}
        self.assertEqual(set(values), {"csrf_token", "tx_id", "attempt_id", "access_token"})
        self.assertEqual(monitored_tx, int(values["tx_id"]))
        self.assertNotIn(self.partner.email, processing["redirect_form_html"])
        return values

    def _start(self, values):
        return self.url_open(payment.START_URL, data=values, allow_redirects=False)

    @staticmethod
    def _invoice(attempt, **overrides):
        return {
            "invoice_id": _INVOICE_ID,
            "project_id": _PROJECT,
            "status": "awaiting_payment",
            "is_final": False,
            "is_test": True,
            "payment_url": _PAYMENT_URL,
            "order": {
                "external_id": attempt.external_order_id,
                "amount": attempt.amount,
                "currency": attempt.currency,
            },
        } | overrides

    def test_create_timeout_reuses_external_id(self):
        order = self._order()
        tx = self._transaction(order)
        invoices = Mock()
        with patch.object(sdk, "make_client", return_value=SimpleNamespace(invoices=invoices)) as factory:
            form = self._render(tx)
            factory.assert_not_called()
            attempt = self.env["paymos.attempt"].browse(int(form["attempt_id"]))
            self.assertEqual((attempt.transaction_id, attempt.sale_order_id), (tx, order))
            self.assertEqual((attempt.amount, attempt.currency, attempt.disposition), ("12.34", "EUR", "prepared"))
            self.assertEqual(tx.state, "draft")
            remote = {}
            accepted_invoice = self._invoice(attempt)

            def accept_then_replay(**values):
                key = values["external_order_id"]
                if key not in remote:
                    remote[key] = accepted_invoice
                    raise TimeoutError("mock server accepted before its reply was lost")
                return remote[key]

            invoices.create.side_effect = accept_then_replay
            response = self._start(form)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "5")
            self.assertEqual(tx.state, "pending")
            self.assertFalse(attempt.invoice_id)
            self.assertEqual(attempt.disposition, "prepared")

            response = self._start(form)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["Location"], _PAYMENT_URL)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(attempt.invoice_id, _INVOICE_ID)
            self.assertEqual(attempt.disposition, "open")
            self.assertEqual(tx.state, "pending")
            self.assertEqual(order.state, "draft")
            self.assertEqual(factory.call_args.args[0].api_key, "pk_test_checkout")
            self.assertEqual(len(remote), 1)
            self.assertEqual(invoices.create.call_count, 2)
            for call in invoices.create.call_args_list:
                self.assertEqual(call.args, ())
                self.assertEqual(call.kwargs, {
                    "project_id": _PROJECT, "amount": "12.34", "currency": "EUR",
                    "external_order_id": attempt.external_order_id,
                })

            invoices.get.return_value = remote[attempt.external_order_id]
            self.assertEqual(self._start(form).status_code, 303)
            self.assertEqual(invoices.create.call_count, 2)
            invoices.get.assert_called_once_with(_INVOICE_ID)
            self.assertEqual(self.url_open("/payment/status", allow_redirects=False).status_code, 200)
            self.assertEqual(tx.state, "pending")
            self.assertEqual(order.state, "draft")

    def test_repeated_native_checkout_reuses_original_and_refuses_changed_amount(self):
        order = self._order()
        first = self._transaction(order)
        repeat = self._transaction(order)
        with patch.object(sdk, "make_client") as factory:
            first_form = self._render(first)
            repeat_form = self._render(repeat)
            self.assertEqual(repeat_form["tx_id"], str(first.id))
            self.assertEqual(repeat_form["attempt_id"], first_form["attempt_id"])
            self.assertEqual(repeat_form["access_token"], first_form["access_token"])
            self.assertEqual(repeat.state, "draft")
            order.order_line.price_unit = 15.00
            changed = self._transaction(order, amount=15.00)
            with self.assertRaises(ValidationError):
                self._render(changed)
            self.assertEqual(self._start(first_form).status_code, 409)
            self.assertEqual(self.env["paymos.attempt"].search_count([("sale_order_id", "=", order.id)]), 1)
            factory.assert_not_called()

    def test_start_requires_native_csrf_exact_token_and_same_session(self):
        order = self._order()
        tx = self._transaction(order)
        form = self._render(tx)
        with patch.object(sdk, "make_client") as factory:
            without_csrf = {name: value for name, value in form.items() if name != "csrf_token"}
            self.assertEqual(self._start(without_csrf).status_code, 400)
            self.assertEqual(self._start(form | {"tx_id": str(tx.id + 1)}).status_code, 403)
            self.assertEqual(self._start(form | {"attempt_id": str(int(form["attempt_id"]) + 1)}).status_code, 403)
            self.assertEqual(self._start(form | {"access_token": "0" * 64}).status_code, 403)
            self.assertEqual(self._start([*form.items(), ("tx_id", form["tx_id"])]).status_code, 400)
            self.authenticate(None, None)
            self.assertEqual(self._start(form | {"csrf_token": self.csrf_token()}).status_code, 403)
            factory.assert_not_called()
        self.assertEqual(tx.state, "draft")

    def test_untrusted_url_or_terminal_invoice_never_confirms_order(self):
        for number, overrides, reason in (
            (1, {"payment_url": "https://checkout.paymos.io.evil.test/invoice/inv_checkout"},
             "invalid_checkout_url"),
            (2, {"payment_url": "https://checkout.paymos.io/invoice/inv_other"}, "invalid_checkout_url"),
            (3, {"status": "paid", "is_final": True}, "invoice_not_open"),
        ):
            with self.subTest(reason=reason, case=number):
                order = self._order()
                tx = self._transaction(order)
                form = self._render(tx)
                attempt = self.env["paymos.attempt"].browse(int(form["attempt_id"]))
                invoice = self._invoice(attempt, invoice_id=f"inv_checkout_{number}", **overrides)
                invoices = Mock()
                invoices.create.return_value = invoice
                with patch.object(sdk, "make_client", return_value=SimpleNamespace(invoices=invoices)):
                    response = self._start(form)
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("Location", response.headers)
                self.assertEqual(attempt.invoice_id, invoice["invoice_id"])
                self.assertEqual((attempt.disposition, attempt.review_reason), ("review", reason))
                self.assertEqual(tx.state, "pending")
                self.assertEqual(order.state, "draft")

    def test_native_amount_checks_before_odoo_rounds(self):
        order = self._order()
        with self.assertRaisesRegex(ValidationError, "currency precision"):
            self._transaction(order, amount=12.345)
        with self.assertRaisesRegex(ValidationError, "currency precision"):
            self._transaction(order, amount="12.345")
        self.assertEqual(money.native_amount(1111.1100000000001, self.currency), "1111.11")
        self.assertEqual(money.native_amount(0, self.currency, allow_zero=True), "0.00")
        with self.assertRaises(money.AmountError):
            money.parse_decimal(12.34)
