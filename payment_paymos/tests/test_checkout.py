import copy
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from lxml import html

from odoo import Command
from odoo.addons.http_routing.tests.common import MockRequest
from odoo.addons.payment.controllers.payment_status import PaymentStatus
from odoo.exceptions import UserError, ValidationError
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
            expected_products = [{"title": "Paymos Checkout Service", "quantity": 1, "unit_amount": "12.34"}]
            self.assertEqual(
                attempt.products_json,
                json.dumps(expected_products, separators=(",", ":"), ensure_ascii=False),
            )
            for call in invoices.create.call_args_list:
                self.assertEqual(call.args, ())
                self.assertEqual(call.kwargs, {
                    "project_id": _PROJECT, "amount": "12.34", "currency": "EUR",
                    "external_order_id": attempt.external_order_id,
                    "products": expected_products,
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

    def _custom_order(self, line_values):
        order = self.env["sale.order"].with_company(self.company).create({
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "website_id": self.website.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [Command.create(values) for values in line_values],
        })
        self.assertEqual(order.currency_id, self.currency)
        return order

    def test_single_and_multi_order_lines_use_client_request(self):
        described = self.env["product.product"].create({
            "name": "Studio Pro",
            "type": "service",
            "list_price": 24.50,
            "description_sale": "<p>Access\r\n&amp; Updates</p><br />Month 1 &nbsp;",
        })
        minimal = self.env["product.product"].create({
            "name": "Priority Support",
            "type": "service",
            "list_price": 12.00,
        })
        single_lines = [
            {
                "product_id": described.id,
                "name": "  <b>Studio</b> &amp; Pro\x00  ",
                "product_uom_qty": 2,
                "price_unit": 24.50,
                "tax_ids": [Command.clear()],
            },
            {
                "display_type": "line_section",
                "name": "Shipping Section",
                "product_uom_qty": 0,
                "price_unit": 0,
            },
            {
                "display_type": "line_note",
                "name": "Gift Note",
                "product_uom_qty": 0,
                "price_unit": 0,
            },
        ]
        if "is_downpayment" in self.env["sale.order.line"]._fields:
            single_lines.append({
                "product_id": minimal.id,
                "name": "Downpayment",
                "product_uom_qty": 1,
                "price_unit": 0.0,
                "is_downpayment": True,
                "tax_ids": [Command.clear()],
            })
        single_order = self._custom_order(single_lines)
        self.assertEqual(single_order.amount_total, 49.00)
        single_tx = self._transaction(single_order, amount=49.00)
        single_requests = []
        single_invoices = Mock()

        def single_request(method, path, payload):
            single_requests.append((method, path, copy.deepcopy(payload)))
            return self._invoice(single_attempt)

        single_client = SimpleNamespace(request=Mock(side_effect=single_request), invoices=single_invoices)
        with patch.object(sdk, "make_client", return_value=single_client):
            single_form = self._render(single_tx)
            single_attempt = self.env["paymos.attempt"].browse(int(single_form["attempt_id"]))
            expected_single = [{
                "title": "Studio & Pro",
                "quantity": 2,
                "unit_amount": "24.50",
                "description": "Access\n& Updates\nMonth 1",
            }]
            self.assertEqual(
                single_attempt.products_json,
                json.dumps(expected_single, separators=(",", ":"), ensure_ascii=False),
            )
            response = self._start(single_form)
        self.assertEqual(response.status_code, 303)
        single_invoices.create.assert_not_called()
        self.assertEqual(single_requests, [(
            "POST",
            "/v1/invoices",
            {
                "project_id": _PROJECT,
                "external_order_id": single_attempt.external_order_id,
                "amount": "49.00",
                "currency": "EUR",
                "products": expected_single,
            },
        )])

        multi_order = self._custom_order([
            {
                "product_id": described.id,
                "name": "Studio Pro",
                "product_uom_qty": 2,
                "price_unit": 24.50,
                "tax_ids": [Command.clear()],
            },
            {
                "product_id": minimal.id,
                "name": "Priority Support",
                "product_uom_qty": 1,
                "price_unit": 12.00,
                "tax_ids": [Command.clear()],
            },
            {
                "display_type": "line_note",
                "name": "Excluded note",
                "product_uom_qty": 0,
                "price_unit": 0,
            },
        ])
        self.assertEqual(multi_order.amount_total, 61.00)
        multi_tx = self._transaction(multi_order, amount=61.00)
        multi_requests = []

        def multi_request(method, path, payload):
            multi_requests.append((method, path, copy.deepcopy(payload)))
            return self._invoice(multi_attempt, invoice_id="inv_multi")

        multi_client = SimpleNamespace(request=Mock(side_effect=multi_request), invoices=Mock())
        with patch.object(sdk, "make_client", return_value=multi_client):
            multi_form = self._render(multi_tx)
            multi_attempt = self.env["paymos.attempt"].browse(int(multi_form["attempt_id"]))
            self.assertEqual(self._start(multi_form).status_code, 303)
        self.assertEqual(multi_requests, [(
            "POST",
            "/v1/invoices",
            {
                "project_id": _PROJECT,
                "external_order_id": multi_attempt.external_order_id,
                "amount": "61.00",
                "currency": "EUR",
                "products": [
                    {
                        "title": "Studio Pro",
                        "quantity": 2,
                        "unit_amount": "24.50",
                        "description": "Access\n& Updates\nMonth 1",
                    },
                    {
                        "title": "Priority Support",
                        "quantity": 1,
                        "unit_amount": "12.00",
                    },
                ],
            },
        )])

    def test_fractional_quantity_falls_back_to_amount_only(self):
        order = self._custom_order([
            {
                "product_id": self.product.id,
                "name": "Whole item",
                "product_uom_qty": 1,
                "price_unit": 10.00,
                "tax_ids": [Command.clear()],
            },
            {
                "product_id": self.product.id,
                "name": "Fractional item",
                "product_uom_qty": 1.5,
                "price_unit": 10.00,
                "tax_ids": [Command.clear()],
            },
        ])
        self.assertEqual(order.amount_total, 25.00)
        tx = self._transaction(order, amount=25.00)
        captured = []

        def capture(method, path, payload):
            captured.append((method, path, copy.deepcopy(payload)))
            return self._invoice(attempt, invoice_id="inv_fraction")

        client = SimpleNamespace(request=Mock(side_effect=capture), invoices=Mock())
        with patch.object(sdk, "make_client", return_value=client):
            form = self._render(tx)
            attempt = self.env["paymos.attempt"].browse(int(form["attempt_id"]))
            self.assertFalse(attempt.products_json)
            self.assertEqual(self._start(form).status_code, 303)
        self.assertEqual(captured, [(
            "POST",
            "/v1/invoices",
            {
                "project_id": _PROJECT,
                "external_order_id": attempt.external_order_id,
                "amount": "25.00",
                "currency": "EUR",
            },
        )])

    def test_retry_immutability_and_replay_after_order_line_edit(self):
        draft_order = self._order()
        draft_tx = self._transaction(draft_order)
        historical_draft = payment._extract_products(draft_order)
        historical_draft[0]["image_url"] = "https://retired.example/item.png"
        draft_raw = json.dumps(historical_draft, ensure_ascii=False, indent=2) + "\n"
        with patch.object(payment, "_order_products_json", return_value=draft_raw):
            draft_form = self._render(draft_tx)
        draft_attempt = self.env["paymos.attempt"].browse(int(draft_form["attempt_id"]))
        self.assertEqual(self._render(draft_tx)["attempt_id"], draft_form["attempt_id"])
        self.assertEqual(draft_attempt.products_json, draft_raw)
        for field, value in (
            ("title", "Changed title"),
            ("description", "Added description"),
            ("quantity", 2),
            ("quantity", True),
            ("quantity", 1.0),
            ("unit_amount", "12.340"),
            ("unit_amount", 12.34),
            ("unknown", "new field"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(historical_draft)
                changed[0][field] = value
                with self.assertRaisesRegex(payment.CheckoutError, "^order_changed$"):
                    payment._assert_attempt_snapshot(
                        draft_attempt, draft_order, self.provider, self.connection,
                        "12.34", "EUR", json.dumps(changed),
                    )
                self.assertEqual(draft_attempt.products_json, draft_raw)
        with self.assertRaises(UserError):
            draft_attempt.write({"products_json": "[]"})
        draft_order.order_line[0].name = "Changed before first POST"
        repeat_draft = self._transaction(draft_order)
        with self.assertRaises(ValidationError):
            self._render(repeat_draft)
        self.assertEqual(self._start(draft_form).status_code, 409)

        quoted = self.env["product.product"].create({
            "name": "Quoted Item",
            "type": "service",
            "list_price": 12.34,
            "description_sale": 'Quote "A" \\ B\nЮникод 🚀',
        })
        order = self._custom_order([
            {
                "product_id": quoted.id,
                "name": 'Canvas "Blue" \\ Edition 🚀',
                "product_uom_qty": 1,
                "price_unit": 12.34,
                "tax_ids": [Command.clear()],
            },
            {
                "product_id": self.product.id,
                "name": "Zero fee add-on",
                "product_uom_qty": 1,
                "price_unit": 0.0,
                "tax_ids": [Command.clear()],
            },
        ])
        tx = self._transaction(order, amount=12.34)
        historical_retry = payment._extract_products(order)
        for index, item in enumerate(historical_retry):
            item["image_url"] = {"retired": index}
        saved_products_json = json.dumps(historical_retry, ensure_ascii=False, indent=2) + "\n"
        expected_products = copy.deepcopy(historical_retry)
        for item in expected_products:
            del item["image_url"]
        wire_bodies = []

        def timeout_then_ok(method, path, payload):
            self.assertEqual(attempt.products_json, saved_products_json)
            self.assertEqual(payload["products"], expected_products)
            self.assertEqual(
                {key: value for key, value in payload.items() if key != "products"},
                {
                    "project_id": _PROJECT,
                    "external_order_id": attempt.external_order_id,
                    "amount": "12.34",
                    "currency": "EUR",
                },
            )
            wire_bodies.append((method, path, json.dumps(payload, separators=(",", ":"), ensure_ascii=False)))
            if len(wire_bodies) == 1:
                raise TimeoutError("mock server accepted before reply was lost")
            return self._invoice(attempt, invoice_id="inv_retry_products")

        client = SimpleNamespace(request=Mock(side_effect=timeout_then_ok), invoices=Mock())
        with patch.object(sdk, "make_client", return_value=client):
            with patch.object(payment, "_order_products_json", return_value=saved_products_json):
                form = self._render(tx)
            attempt = self.env["paymos.attempt"].browse(int(form["attempt_id"]))
            self.assertEqual(attempt.products_json, saved_products_json)
            self.assertEqual(self._render(tx)["attempt_id"], form["attempt_id"])

            first_response = self._start(form)
            self.assertEqual(first_response.status_code, 503)
            self.assertEqual(tx.state, "pending")
            self.assertFalse(attempt.invoice_id)

            order.order_line[0].name = "Changed after timeout"
            quoted.description_sale = "Changed product description"
            repeat = self._transaction(order, amount=12.34)
            repeat_form = self._render(repeat)
            self.assertEqual(repeat_form["tx_id"], str(tx.id))
            self.assertEqual(repeat_form["attempt_id"], str(attempt.id))
            self.assertEqual(attempt.products_json, saved_products_json)

            second_response = self._start(repeat_form)
            self.assertEqual(second_response.status_code, 303)
        self.assertEqual(len(wire_bodies), 2)
        self.assertEqual(wire_bodies[0], wire_bodies[1])
        self.assertEqual(json.loads(wire_bodies[1][2])["products"], expected_products)
        self.assertEqual(attempt.products_json, saved_products_json)
        self.assertEqual(json.loads(attempt.products_json), historical_retry)
        self.assertEqual(attempt.invoice_id, "inv_retry_products")
        self.assertEqual(self.env["paymos.attempt"].search_count([("sale_order_id", "=", order.id)]), 1)
