import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, new_test_user

from odoo.addons.payment_paymos import uninstall_hook
from odoo.addons.payment_paymos.services import connect, payment, sdk


_SOURCE = "https://odoo-webhook.example"
_PROJECT = "prj_webhook"
_SECRET = "whsec_test_webhook_fixture"
_INVOICE_ID = "inv_webhook"


class _RawVerifier:
    def __init__(self, credentials):
        self.key = credentials.webhook_secret.encode("ascii")

    def verify(self, header, raw):
        try:
            timestamp_field, signature_field = header.split(",")
            timestamp = int(timestamp_field.removeprefix("t="))
            if abs(int(time.time()) - timestamp) > 300:
                return False
            expected = hmac.new(self.key, str(timestamp).encode("ascii") + b"." + raw, hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature_field, f"v1={expected}")
        except ValueError:
            return False


@tagged("post_install", "-at_install")
class TestPaymosWebhook(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_paymos.payment_provider_paymos")
        cls.method = cls.env.ref("payment_paymos.payment_method_paymos")
        cls.website = cls.env.ref("base.default_website")
        cls.company = cls.provider.company_id
        if not cls.company.chart_template:
            cls.env["account.chart.template"].try_loading(False, cls.company, install_demo=False)
        cls.company.sale_automatic_invoice = True
        cls.currency = cls.env.ref("base.EUR")
        cls.currency.active = True
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Paymos Webhook EUR", "currency_id": cls.currency.id, "company_id": cls.company.id,
        })
        cls.partner = cls.env["res.partner"].create({
            "name": "Paymos Webhook Fixture", "email": "webhook-customer@example.test",
        })
        cls.product = cls.env["product.product"].create({
            "name": "Paymos Webhook Service", "type": "service", "list_price": 12.34,
            "invoice_policy": "order",
        })
        cls.admin = new_test_user(
            cls.env, login="paymos-webhook-admin",
            groups="base.group_user,base.group_system,sales_team.group_sale_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )

    def setUp(self):
        super().setUp()
        self.authenticate(None, None)
        self.startPatcher(patch.dict(os.environ, {
            "PAYMOS_ODOO_ENVELOPE_KEY": Fernet.generate_key().decode("ascii"),
        }))
        self.env["ir.config_parameter"].sudo().set_str("web.base.url", _SOURCE)
        credentials = connect.Credentials(
            _PROJECT, "sandbox", "pk_test_webhook_fixture", "sk_test_webhook_fixture", _SECRET,
            ("invoice_refunds_v1",), 1, True,
        )
        binding = {
            "provider_id": self.provider.id, "company_id": self.company.id,
            "generation": 1, "source_url": _SOURCE,
        }
        self.connection = self.env["paymos.connection"].create(dict(
            binding, project_id=_PROJECT, environment="sandbox",
            credentials_envelope=connect.seal_credentials(credentials, **binding),
        ))
        self.provider.write({
            "paymos_active_connection_id": self.connection.id, "active": True, "is_published": True,
        })
        payment.assert_accounting_ready(self.provider, self.currency)
        self.native_account_method = self.provider._get_provider_payment_method("paymos")
        self.assertTrue(self.native_account_method)
        self.assertEqual(self.native_account_method.payment_type, "inbound")
        self.client = SimpleNamespace(invoices=Mock(), refunds=Mock())
        self.client.refunds.list.return_value = {"items": [], "next_cursor": None}
        self.factory = self.startPatcher(patch.object(sdk, "make_client", return_value=self.client))
        self.verifier = self.startPatcher(patch.object(sdk, "webhook_verifier", side_effect=_RawVerifier))
        self.sequence = 0

    def _order(self):
        order = self.env["sale.order"].with_company(self.company).create({
            "partner_id": self.partner.id, "company_id": self.company.id,
            "website_id": self.website.id, "pricelist_id": self.pricelist.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 1,
                "price_unit": 12.34, "tax_ids": [Command.clear()],
            })],
        })
        self.assertEqual((order.amount_total, order.currency_id), (12.34, self.currency))
        return order

    def _transaction_values(self, order):
        self.sequence += 1
        return {
            "provider_id": self.provider.id, "payment_method_id": self.method.id,
            "reference": f"Paymos-Webhook-{self.sequence}", "partner_id": order.partner_invoice_id.id,
            "currency_id": order.currency_id.id, "amount": 12.34,
            "operation": "online_redirect", "sale_order_ids": [Command.set(order.ids)],
        }

    def _attempt(self, order=None):
        order = order if order is not None else self._order()
        tx = self.env["payment.transaction"].create(self._transaction_values(order))
        attempt = payment.prepare_attempt(tx)
        tx._paymos_mark_checkout_pending()
        return order, tx, attempt

    @staticmethod
    def _invoice(attempt, **changes):
        return {
            "invoice_id": _INVOICE_ID,
            "project_id": _PROJECT,
            "status": "paid",
            "is_final": True,
            "is_test": True,
            "order": {"external_id": attempt.external_order_id, "amount": attempt.amount, "currency": attempt.currency},
            "payment": {"currency": "USDT", "network": "TRC20", "expected": "10", "paid": "10", "remaining": "0"},
        } | changes

    @staticmethod
    def _raw(invoice, event_id="evt_paymos_webhook", event_type=None, **encoding):
        return json.dumps({
            "event_id": event_id,
            "event_type": event_type or f"invoice.{invoice['status']}",
            "version": 1,
            "occurred_at": int(time.time()),
            "data": invoice,
        }, **encoding).encode("utf-8")

    @staticmethod
    def _signature(raw, timestamp=None):
        timestamp = int(time.time()) if timestamp is None else timestamp
        digest = hmac.new(_SECRET.encode("ascii"), str(timestamp).encode("ascii") + b"." + raw, hashlib.sha256)
        return f"t={timestamp},v1={digest.hexdigest()}"

    def _post(self, raw, *, signature=None):
        return self.url_open(payment.WEBHOOK_URL, data=raw, method="POST", allow_redirects=False, headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature if signature is not None else self._signature(raw),
        })

    def _queue(self, tx):
        return self.env["payment.data"].sudo().search([("transaction_id", "=", tx.id)])

    def _consume(self, tx, *, post_process=True):
        queue = self._queue(tx)
        self.assertEqual(len(queue), 1)
        payload = queue.payload
        with self.env.cr.savepoint():
            tx.with_context(payment_safe_write=True)._process(payload)
            queue.unlink()
        if post_process:
            tx.with_context(skip_sale_auto_invoice_send=True, mail_notify_force_send=False)._post_process_with_lock()
        return payload

    def test_signed_duplicate_once(self):
        order, tx, attempt = self._attempt()
        invoice = self._invoice(attempt)
        self.client.invoices.get.return_value = invoice
        raw = self._raw(invoice | {"private_note": "тест"}, ensure_ascii=False)
        self.assertEqual(self._post(raw).status_code, 200)
        self.assertEqual(self._post(raw).status_code, 200)
        self.client.invoices.get.assert_called_once_with(_INVOICE_ID)
        self.assertEqual(tx.state, "pending")
        self.assertEqual(order.state, "draft")
        marker = self.env["paymos.event"].search([("attempt_id", "=", attempt.id)])
        self.assertEqual(marker.raw_sha256, hashlib.sha256(raw).hexdigest())
        self.assertFalse(marker.consumed)
        self.assertEqual(self._queue(tx).payload, {"paymos_event_id": marker.id})
        self.assertEqual(self.verifier.call_args.args[0].webhook_secret, _SECRET)

        payload = self._consume(tx)
        self.assertTrue(marker.consumed)
        self.assertEqual((tx.state, attempt.disposition, tx.provider_reference), ("done", "paid", _INVOICE_ID))
        self.assertEqual(order.state, "sale")
        self.assertTrue(tx.is_post_processed)
        self.assertEqual(len(tx.invoice_ids), 1)
        self.assertEqual(tx.invoice_ids, order.invoice_ids)
        self.assertEqual(tx.invoice_ids.state, "posted")
        self.assertEqual(tx.invoice_ids.amount_total, 12.34)
        self.assertTrue(tx.payment_id)
        self.assertEqual(tx.payment_id.amount, 12.34)
        self.assertEqual(tx.payment_id.move_id.state, "posted")
        self.assertEqual(tx.payment_id.payment_method_line_id.payment_method_id, self.native_account_method)
        self.assertEqual(self.client.refunds.list.call_count, 2)
        for call in self.client.refunds.list.call_args_list:
            self.assertEqual(call.kwargs, {"invoice_id": _INVOICE_ID, "limit": 50})

        posted_invoice = tx.invoice_ids
        posted_payment = tx.payment_id
        posted_line = posted_payment.payment_method_line_id
        self.assertEqual(self._post(raw).status_code, 200)
        tx.with_context(payment_safe_write=True)._process(payload)
        tx.with_context(skip_sale_auto_invoice_send=True, mail_notify_force_send=False)._post_process_with_lock()
        self.assertEqual((tx.invoice_ids, tx.payment_id), (posted_invoice, posted_payment))
        self.assertFalse(self._queue(tx))

        with patch.object(self.env.registry, "uninstalling_modules", {"payment_paymos"}):
            uninstall_hook(self.env)
        self.assertTrue(order.exists())
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.invoice_ids, posted_invoice)
        self.assertEqual(tx.payment_id, posted_payment)
        self.assertEqual(posted_payment.move_id.state, "posted")
        self.assertEqual(posted_payment.payment_method_line_id, posted_line)
        self.assertFalse(posted_line.journal_id)
        self.assertTrue(self.native_account_method.exists())

    def test_native_rpc_cannot_settle_or_rebind(self):
        order, tx, attempt = self._attempt()
        foreign = self._order()
        actor = tx.with_user(self.admin).with_context(
            allowed_company_ids=self.company.ids, payment_safe_write=True,
            paymos_write_scope=[tx.id, "state"],
        )
        other_provider = self.env["payment.provider"].create({
            "name": "Ordinary Provider Webhook Test", "code": "none", "company_id": self.company.id,
        })
        for values in (
            {"state": "done"}, {"amount": 1}, {"currency_id": self.company.currency_id.id},
            {"sale_order_ids": [Command.set(foreign.ids)]}, {"provider_id": other_provider.id},
            {"reference": "changed"}, {"is_post_processed": True},
            {"landing_route": "https://untrusted.example/"},
            {"invoice_ids": [Command.clear()]}, {"payment_id": False},
        ):
            with self.assertRaises(AccessError):
                actor.write(values)
        with self.assertRaises(AccessError):
            actor.with_context(default_state="done").create(self._transaction_values(order))
        with self.assertRaises(AccessError):
            actor.create(self._transaction_values(order) | {"state": "done"})
        with self.assertRaises(AccessError):
            actor.with_context(default_is_post_processed=True).create(self._transaction_values(order))
        with self.assertRaises(UserError):
            actor.action_confirm()
        with self.assertRaises(UserError):
            actor.action_post_process()
        with self.assertRaises(AccessError):
            actor.env["paymos.event"].create({"event_id": "evt_forged"})

        ordinary_method = self.env["payment.method"].create({
            "name": "Ordinary Method Webhook Test", "code": "other", "provider_id": other_provider.id,
            "image": self.method.image,
        })
        other = actor.create(self._transaction_values(order) | {
            "provider_id": other_provider.id, "payment_method_id": ordinary_method.id,
        })
        with self.assertRaises(AccessError):
            other.write({"provider_id": self.provider.id})
        other.write({"state_message": "Native other provider"})
        self.assertEqual(other.state_message, "Native other provider")

        for payload in ({"state": "done"}, {"paymos_event_id": 2147483647}):
            forged = actor.env["payment.data"].create({"transaction_id": tx.id, "payload": payload})
            tx.with_context(payment_safe_write=True)._process(forged.payload)
            forged.unlink()
        self.assertEqual(tx.state, "pending")
        self.assertEqual(order.state, "draft")
        self.assertFalse(tx.is_post_processed)
        self.assertFalse(attempt.invoice_id)
        self.factory.assert_not_called()

    def test_signature_body_and_signed_binding_fail_before_reverse(self):
        _order, tx, attempt = self._attempt()
        invoice = self._invoice(attempt)
        raw = self._raw(invoice)
        self.assertEqual(self._post(raw, signature="t=1,v1=bad").status_code, 401)
        stale = int(time.time()) - 301
        self.assertEqual(self._post(raw, signature=self._signature(raw, stale)).status_code, 401)
        tampered = raw + b" "
        self.assertEqual(self._post(tampered, signature=self._signature(raw)).status_code, 401)
        duplicate = b'{"event_id":"evt_shadow",' + raw[1:]
        self.assertEqual(self._post(duplicate).status_code, 400)
        self.assertEqual(self._post(b"x" * (payment.MAX_WEBHOOK_BYTES + 1)).status_code, 413)
        self.assertEqual(self._post(iter((raw,)), signature=self._signature(raw)).status_code, 411)
        mismatch = invoice | {"order": invoice["order"] | {"amount": "12.33"}}
        self.assertEqual(self._post(self._raw(mismatch)).status_code, 400)
        self.assertEqual(self._post(self._raw(invoice | {"project_id": "prj_elsewhere"})).status_code, 401)
        self.assertEqual(self._post(self._raw(invoice | {"is_test": False})).status_code, 401)
        self.factory.assert_not_called()
        self.assertFalse(self._queue(tx))
        self.assertEqual(tx.state, "pending")

    def test_same_event_id_changed_bytes_stops_projection(self):
        _order, tx, attempt = self._attempt()
        invoice = self._invoice(attempt)
        self.client.invoices.get.return_value = invoice
        original = self._raw(invoice)
        self.assertEqual(self._post(original).status_code, 200)
        self.assertEqual(self._post(original + b" ").status_code, 409)
        self.assertEqual(attempt.review_reason, "event_payload_conflict")
        self._consume(tx, post_process=False)
        self.assertEqual(tx.state, "pending")
        self.assertFalse(self.env["paymos.event"].search([("attempt_id", "=", attempt.id)]).consumed)

    def test_native_enqueue_rollback_and_inverse_order_guard(self):
        order, tx, attempt = self._attempt()
        foreign_order = self._order()
        invoice = self._invoice(attempt)
        self.client.invoices.get.return_value = invoice
        raw = self._raw(invoice)
        original_record = type(tx)._record

        def broken_record(record, payload):
            original_record(record, payload)
            raise RuntimeError("mock native enqueue failure after insert")

        with patch.object(type(tx), "_record", broken_record):
            self.assertEqual(self._post(raw).status_code, 503)
        self.assertFalse(self._queue(tx))
        self.assertFalse(attempt.invoice_id)
        self.assertFalse(self.env["paymos.event"].search([("attempt_id", "=", attempt.id)]))

        self.assertEqual(self._post(raw).status_code, 200)
        queue = self._queue(tx)
        foreign_order.transaction_ids = [Command.link(tx.id)]
        with self.assertRaises(payment.CheckoutError):
            tx.with_context(payment_safe_write=True)._process(queue.payload)
        self.assertEqual(tx.state, "pending")
        self.assertEqual(order.state, "draft")
        self.assertEqual(foreign_order.state, "draft")

        foreign_order.transaction_ids = [Command.unlink(tx.id)]
        self._consume(tx, post_process=False)
        self.assertEqual(tx.state, "done")
        foreign_order.transaction_ids = [Command.link(tx.id)]
        tx.with_context(skip_sale_auto_invoice_send=True, mail_notify_force_send=False)._post_process_with_lock()
        self.assertFalse(tx.is_post_processed)
        self.assertEqual((order.state, foreign_order.state), ("draft", "draft"))

    def test_underpaid_and_late_remain_unsettled(self):
        order, tx, attempt = self._attempt()
        exact = self._invoice(attempt)
        underpaid = exact | {"payment": exact["payment"] | {"paid": "9.9", "remaining": "0.1"}}
        self.client.invoices.get.return_value = underpaid
        self.assertEqual(self._post(self._raw(underpaid, "evt_paymos_underpaid")).status_code, 200)
        self.assertEqual((attempt.disposition, attempt.review_reason), ("review", "insufficient_payment"))
        self.assertFalse(self._queue(tx))

        closed = exact | {"status": "expired", "payment": None}
        self.client.invoices.get.return_value = closed
        self.assertEqual(self._post(self._raw(closed, "evt_paymos_expired")).status_code, 200)
        self._consume(tx, post_process=False)
        self.assertEqual((attempt.disposition, tx.state), ("expired", "cancel"))
        _same_order, replacement_tx, replacement = self._attempt(order)
        late = closed | {"paid_late_at": int(time.time()), "payment": exact["payment"]}
        self.client.invoices.get.return_value = late
        raw = self._raw(late, "evt_paymos_late", "invoice.paid_late")
        self.assertEqual(self._post(raw).status_code, 200)
        self.assertEqual((attempt.disposition, attempt.review_reason), ("expired", "paid_late"))
        self.assertEqual((replacement.disposition, replacement.review_reason), ("review", "previous_attempt_paid"))
        self.assertEqual(replacement_tx.state, "pending")
        self.assertEqual(order.state, "draft")

    def test_external_refund_snapshot_blocks_enqueue_and_apply(self):
        order, tx, attempt = self._attempt()
        invoice = self._invoice(attempt)
        self.client.invoices.get.return_value = invoice
        full = {
            "withdrawal_id": "wdr_refundfixture", "external_order_id": "dashboard_refund",
            "invoice_id": _INVOICE_ID, "project_id": _PROJECT, "is_test": True,
            "type": "refund", "mode": "full", "status": "pending_review", "is_final": False,
        }
        self.client.refunds.list.return_value = {"items": [full], "next_cursor": None}
        self.assertEqual(self._post(self._raw(invoice, "evt_full_pending")).status_code, 200)
        self.assertEqual((attempt.disposition, attempt.review_reason), ("review", "external_full_refund"))
        self.assertFalse(self._queue(tx))

        self.client.refunds.list.return_value = {"items": [], "next_cursor": None}
        self.assertEqual(self._post(self._raw(invoice, "evt_queue_then_refund")).status_code, 200)
        self.assertTrue(self._queue(tx))
        full = full | {"status": "completed", "is_final": True}
        self.client.refunds.list.return_value = {"items": [full], "next_cursor": None}
        self._consume(tx, post_process=False)
        self.assertEqual(tx.state, "pending")
        self.assertEqual(order.state, "draft")
        self.assertEqual(attempt.review_reason, "external_full_refund")

    def test_refund_snapshot_unavailable_leaves_recoverable_attempt(self):
        _order, tx, attempt = self._attempt()
        invoice = self._invoice(attempt)
        self.client.invoices.get.return_value = invoice
        raw = self._raw(invoice)
        self.client.refunds.list.side_effect = TimeoutError("mock refund history timeout")
        self.assertEqual(self._post(raw).status_code, 503)
        self.assertEqual(attempt.review_reason, "refund_snapshot_unavailable")
        self.assertFalse(self._queue(tx))
        self.assertFalse(self.env["paymos.event"].search([("attempt_id", "=", attempt.id)]))

        self.client.refunds.list.side_effect = None
        self.assertEqual(payment.reconcile_attempt(attempt), "queued")
        self.assertTrue(self._queue(tx))
        self._consume(tx, post_process=False)
        self.assertEqual(tx.state, "done")

    def test_accounting_not_ready_refuses_checkout_and_paid_projection(self):
        _order, tx, attempt = self._attempt()
        self.currency.active = False
        with self.assertRaisesRegex(payment.CheckoutError, "^accounting_unavailable$"):
            payment.start_attempt(attempt)
        self.factory.assert_not_called()
        invoice = self._invoice(attempt)
        self.client.invoices.get.return_value = invoice
        self.assertEqual(self._post(self._raw(invoice)).status_code, 503)
        self.assertFalse(self._queue(tx))
        self.assertEqual((attempt.disposition, attempt.review_reason), ("review", "accounting_unavailable"))

        self.currency.active = True
        _other_order, other_tx, other_attempt = self._attempt()
        line = self.provider.journal_id.inbound_payment_method_line_ids.filtered(
            lambda item: item.payment_provider_id == self.provider
        )
        line.payment_account_id = False
        self.factory.reset_mock()
        with self.assertRaisesRegex(payment.CheckoutError, "^accounting_unavailable$"):
            payment.start_attempt(other_attempt)
        self.factory.assert_not_called()
        other_invoice = self._invoice(other_attempt, invoice_id="inv_webhook_missing_journal")
        self.client.invoices.get.return_value = other_invoice
        self.assertEqual(self._post(self._raw(other_invoice, "evt_missing_journal")).status_code, 503)
        self.assertEqual(other_attempt.review_reason, "accounting_unavailable")
        self.assertFalse(self._queue(other_tx))
        self.assertEqual((tx.state, other_tx.state), ("pending", "pending"))

    def test_withdrawal_is_not_an_invoice_projection(self):
        _order, tx, attempt = self._attempt()
        attempt.invoice_id = _INVOICE_ID
        raw = self._raw({
            "invoice_id": _INVOICE_ID, "project_id": _PROJECT, "is_test": True,
            "withdrawal_id": "wdr_refundfixture", "status": "completed",
        }, "evt_withdrawal", "withdrawal.completed")
        self.assertEqual(self._post(raw).status_code, 503)
        self.factory.assert_not_called()
        self.assertFalse(self._queue(tx))
