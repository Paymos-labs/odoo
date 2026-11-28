import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, new_test_user

from odoo.addons.payment_paymos.services import connect, payment, refund, sdk

from .test_webhook import _RawVerifier


_SOURCE = "https://odoo-refund.example"
_PROJECT = "prj_refund"
_SECRET = "whsec_test_refund_fixture"
_INVOICE_ID = "inv_refundfixture"
_WITHDRAWAL_ID = "wdr_refundfixture"


@tagged("post_install", "-at_install")
class TestPaymosRefund(HttpCase):
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
            "name": "Paymos Refund EUR", "currency_id": cls.currency.id, "company_id": cls.company.id,
        })
        cls.partner = cls.env["res.partner"].create({"name": "Paymos Refund Fixture"})
        cls.product = cls.env["product.product"].create({
            "name": "Paymos Refund Service", "type": "service", "list_price": 12.34, "invoice_policy": "order",
        })
        cls.admin = new_test_user(
            cls.env, login="paymos-refund-admin",
            groups="base.group_user,base.group_system,sales_team.group_sale_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.employee = new_test_user(
            cls.env, login="paymos-refund-employee", company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )

    def setUp(self):
        super().setUp()
        self.authenticate(None, None)
        self.startPatcher(patch.dict(os.environ, {
            "PAYMOS_ODOO_ENVELOPE_KEY": Fernet.generate_key().decode("ascii"),
        }))
        self.env["ir.config_parameter"].sudo().set_str("web.base.url", _SOURCE)
        self.connection = self._connection("sandbox")
        self.provider.write({
            "paymos_active_connection_id": self.connection.id, "active": True, "is_published": True,
        })
        payment.assert_accounting_ready(self.provider, self.currency)
        self.client = SimpleNamespace(invoices=Mock(), refunds=Mock())
        self.client.refunds.list.return_value = {"items": [], "next_cursor": None}
        self.factory = self.startPatcher(patch.object(sdk, "make_client", return_value=self.client))
        self.startPatcher(patch.object(sdk, "webhook_verifier", side_effect=_RawVerifier))

    def _connection(self, environment, *, refunds_enabled=True, generation=1):
        stage = "test" if environment == "sandbox" else "live"
        credentials = connect.Credentials(
            _PROJECT, environment, f"pk_{stage}_refund_fixture", f"sk_{stage}_refund_fixture",
            f"whsec_{stage}_refund_fixture", ("invoice_refunds_v1",), 1, refunds_enabled,
        )
        binding = {
            "provider_id": self.provider.id, "company_id": self.company.id,
            "generation": generation, "source_url": _SOURCE,
        }
        return self.env["paymos.connection"].create(dict(
            binding, project_id=_PROJECT, environment=environment,
            credentials_envelope=connect.seal_credentials(credentials, **binding),
        ))

    def _prepared(self, status="paid"):
        order = self.env["sale.order"].with_company(self.company).create({
            "partner_id": self.partner.id, "company_id": self.company.id,
            "website_id": self.website.id, "pricelist_id": self.pricelist.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 1,
                "price_unit": 12.34, "tax_ids": [Command.clear()],
            })],
        })
        self.assertEqual((order.amount_total, order.currency_id), (12.34, self.currency))
        tx = self.env["payment.transaction"].create({
            "provider_id": self.provider.id, "payment_method_id": self.method.id,
            "reference": "Paymos-Refund-Original", "partner_id": order.partner_invoice_id.id,
            "currency_id": self.currency.id, "amount": 12.34, "operation": "online_redirect",
            "sale_order_ids": [Command.set(order.ids)],
        })
        attempt = payment.prepare_attempt(tx)
        invoice = {
            "invoice_id": _INVOICE_ID,
            "project_id": _PROJECT,
            "status": status,
            "is_final": True,
            "is_test": True,
            "order": {"external_id": attempt.external_order_id, "amount": "12.34", "currency": "EUR"},
            "payment": {
                "currency": "USDC", "network": "ERC20", "expected": "12.34",
                "paid": "15.00" if status == "paid_over" else "12.34", "remaining": "0",
            },
        }
        self.client.invoices.get.return_value = invoice
        return order, tx, attempt, invoice

    @staticmethod
    def _raw(event_id, event_type, data):
        return json.dumps({
            "event_id": event_id, "event_type": event_type, "version": 1,
            "occurred_at": int(time.time()), "data": data,
        }).encode("utf-8")

    def _webhook(self, raw):
        timestamp = int(time.time())
        digest = hmac.new(_SECRET.encode("ascii"), str(timestamp).encode("ascii") + b"." + raw, hashlib.sha256)
        return self.url_open(payment.WEBHOOK_URL, method="POST", data=raw, allow_redirects=False, headers={
            "Content-Type": "application/json", "X-Webhook-Signature": f"t={timestamp},v1={digest.hexdigest()}",
        })

    def _queue(self, tx):
        return self.env["payment.data"].sudo().search([("transaction_id", "=", tx.id)])

    def _native_apply(self, tx):
        queue = self._queue(tx)
        self.assertEqual(len(queue), 1)
        with self.env.cr.savepoint():
            tx.with_context(payment_safe_write=True)._process(queue.payload)
            queue.unlink()
        tx.with_context(skip_sale_auto_invoice_send=True, mail_notify_force_send=False)._post_process_with_lock()

    def _settled(self, status="paid"):
        order, tx, attempt, invoice = self._prepared(status)
        self.assertEqual(self._webhook(self._raw("evt_refund_original", f"invoice.{status}", invoice)).status_code, 200)
        self._native_apply(tx)
        self.assertEqual((tx.state, attempt.disposition, order.state), ("done", "paid", "sale"))
        self.assertEqual(tx.payment_id.move_id.state, "posted")
        return order, tx, attempt, invoice

    def _actor(self, tx):
        return tx.with_user(self.admin).with_context(allowed_company_ids=self.company.ids)

    def _operation(self, attempt, mode="full"):
        return self.env["paymos.refund"].sudo().search([
            ("attempt_id", "=", attempt.id), ("mode", "=", mode),
        ])

    @staticmethod
    def _snapshot(operation, *, status="completed", amount="12.34", **changes):
        return {
            "type": "refund", "withdrawal_id": _WITHDRAWAL_ID, "invoice_id": _INVOICE_ID,
            "project_id": _PROJECT, "is_test": True, "mode": operation.mode,
            "external_order_id": operation.external_order_id, "status": status,
            "is_final": status in ("completed", "failed", "cancelled"), "amount": amount,
            "currency": "USDC", "network": "ERC20",
            "destination_address": "0xfixture", "first_sender": "0xfixture",
        } | changes

    def test_timeout_completion_single_child(self):
        order, parent, attempt, _invoice = self._settled()
        self.assertEqual(self.provider.support_refund, "full_only")
        self.assertEqual(self.method.support_refund, "full_only")
        original_payment = parent.payment_id

        live = self._connection("live")
        self.provider.write({
            "paymos_active_connection_id": live.id, "is_live": True,
            "active": False, "is_published": False,
        })
        actor = self._actor(parent)
        actor.action_refund(amount_to_refund=12.34)
        operation = self._operation(attempt)
        child = operation.child_transaction_id
        self.assertEqual((operation.state, child.operation, child.amount), ("to_send", "refund", -12.34))
        self.assertEqual(child.source_transaction_id, parent)
        self.assertFalse(child.is_live)
        self.assertEqual(child.currency_id, self.currency)
        self.assertEqual(parent._refund(), child)
        self.client.refunds.create.assert_not_called()

        created = self._snapshot(operation, status="created")
        accepted = {}

        def lost_first_reply(**values):
            key = values["external_order_id"]
            if key not in accepted:
                accepted[key] = created
                raise TimeoutError("mock Paymos accepted the refund before reply was lost")
            return accepted[key]

        self.client.refunds.create.side_effect = lost_first_reply
        refund.run_operation(operation)
        self.assertEqual((operation.state, child.state), ("unknown", "pending"))
        self.assertFalse(operation.withdrawal_id)
        refund.run_operation(operation)
        self.assertEqual((operation.state, operation.withdrawal_id), ("pending", _WITHDRAWAL_ID))
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.client.refunds.create.call_count, 2)
        for call in self.client.refunds.create.call_args_list:
            self.assertEqual(call.kwargs, {
                "invoice_id": _INVOICE_ID, "mode": "full", "external_order_id": operation.external_order_id,
            })
        self.assertTrue(self.factory.call_args.args[0].api_key.startswith("pk_test_"))

        completed = self._snapshot(operation)
        self.client.refunds.get.return_value = completed
        self.client.refunds.list.return_value = {"items": [completed], "next_cursor": None}
        raw = self._raw("evt_refund_completed", "withdrawal.completed", completed)
        self.assertEqual(self._webhook(raw).status_code, 200)
        self.assertEqual(self._webhook(raw).status_code, 200)
        self.assertEqual(len(self._queue(child)), 1)
        self.assertEqual(child.state, "pending")
        self._native_apply(child)
        self.assertEqual((child.state, operation.state), ("done", "completed"))
        self.assertEqual(child.provider_reference, _WITHDRAWAL_ID)
        self.assertEqual((child.amount, child.currency_id.name), (-12.34, "EUR"))
        self.assertEqual((operation.token_amount, operation.token_currency), ("12.34", "USDC"))
        self.assertTrue(child.is_post_processed)
        self.assertEqual(child.payment_id.move_id.state, "posted")
        self.assertEqual(child.payment_id.amount, 12.34)
        self.assertEqual(child.payment_id.currency_id, self.currency)
        self.assertEqual(child.payment_id.payment_type, "outbound")
        self.assertEqual(child.payment_id.source_payment_id, original_payment)

        actor.action_refund()
        actor.action_refund()
        self.assertEqual(parent._refund(), child)
        self.assertEqual(parent.child_transaction_ids, child)
        self.assertEqual((parent.state, order.state, parent.payment_id), ("done", "sale", original_payment))
        self.assertEqual(self.client.refunds.create.call_count, 2)
        self.assertFalse(self._queue(child))

    def test_partial_and_direct_rpc_are_refused(self):
        _order, parent, attempt, _invoice = self._settled()
        actor = self._actor(parent)
        for amount in (0, -12.34, 1, 12.345, 12.35):
            with self.assertRaises(ValidationError):
                actor.action_refund(amount_to_refund=amount)
        with self.assertRaises(AccessError):
            parent.with_user(self.employee).action_refund()
        with self.assertRaises(AccessError):
            actor.with_context(paymos_refund_create=True).create({
                "provider_id": self.provider.id, "payment_method_id": self.method.id,
                "reference": "Paymos-Refund-Forged", "partner_id": parent.partner_id.id,
                "currency_id": self.currency.id, "amount": -12.34, "operation": "refund",
                "source_transaction_id": parent.id,
            })
        self.assertFalse(self._operation(attempt))
        self.assertFalse(parent.child_transaction_ids)
        self.client.refunds.create.assert_not_called()

    def test_failed_refund_stays_review_and_cannot_repeat_payout(self):
        _order, parent, attempt, _invoice = self._settled()
        child = parent._refund()
        operation = self._operation(attempt)
        failed = self._snapshot(operation, status="failed")
        self.client.refunds.create.return_value = failed
        self.client.refunds.get.return_value = failed
        refund.run_operation(operation)
        self.assertEqual(operation.state, "queued")
        self.client.refunds.list.return_value = {"items": [failed], "next_cursor": None}
        self._native_apply(child)
        self.assertEqual((operation.state, operation.review_reason, child.state),
                         ("failed", "refund_failed", "cancel"))
        self.assertFalse(child.payment_id)
        with self.assertRaisesRegex(UserError, "Review the existing Paymos refund"):
            self._actor(parent).action_refund()
        self.assertEqual(parent.child_transaction_ids, child)
        self.client.refunds.create.assert_called_once()

    def test_tolerance_payment_full_refund_is_refused_before_a_child_exists(self):
        # CMSP-26: the server refunds no invoice accepted below its expected amount
        # (RefundCore: gross < expected), so no refund transaction may wait for it.
        order, parent, attempt, invoice = self._prepared()
        invoice["payment"]["paid"] = "12.30"
        self.assertEqual(self._webhook(self._raw("evt_refund_tolerance", "invoice.paid", invoice)).status_code, 200)
        self._native_apply(parent)
        self.assertEqual((parent.state, attempt.disposition, order.state), ("done", "paid", "sale"))
        with self.assertRaisesRegex(UserError, "Paymos could not authorize a refund"):
            self._actor(parent).action_refund(amount_to_refund=12.34)
        self.assertFalse(self._operation(attempt))
        self.assertFalse(parent.child_transaction_ids)
        self.client.refunds.create.assert_not_called()

    def test_definitive_refund_refusal_goes_to_review_at_once(self):
        # CMSP-26: 400, 404, 409, 410 and 422 refuse the POST for good; it is not an unknown result.
        _order, parent, attempt, _invoice = self._settled()
        parent._refund()
        operation = self._operation(attempt)
        self.client.refunds.create.side_effect = sdk._vendor().ConflictError(409, json.dumps({
            "type": "about:blank", "title": "Paymos API request failed.", "status": 409, "code": "", "detail": "",
        }))
        refund.run_operation(operation)
        self.assertEqual((operation.state, operation.review_reason), ("review", "refund_request_rejected"))
        refund.run_operation(operation)
        self.client.refunds.create.assert_called_once()

    def test_overpayment_does_not_change_principal(self):
        order, parent, attempt, _invoice = self._settled("paid_over")
        original_payment = parent.payment_id
        original_invoice = parent.invoice_ids
        actor = self._actor(parent)
        actor.action_paymos_refund_overpayment()
        actor.action_paymos_refund_overpayment()
        operation = self._operation(attempt, "overpayment")
        self.assertFalse(operation.child_transaction_id)
        self.assertFalse(parent.child_transaction_ids)
        self.client.refunds.create.assert_not_called()

        excess = self._snapshot(operation, amount="2.66")
        self.client.refunds.create.return_value = excess
        self.client.refunds.get.return_value = excess
        refund.run_operation(operation)
        self.assertEqual(operation.state, "completed")
        self.client.refunds.list.return_value = {"items": [excess], "next_cursor": None}
        raw = self._raw("evt_overpayment_refunded", "withdrawal.completed", excess)
        self.assertEqual(self._webhook(raw).status_code, 200)
        self.assertEqual(self._webhook(raw).status_code, 200)
        self.assertEqual(len(order.message_ids.filtered(lambda message: "Paymos returned the overpayment." in message.body)), 1)
        self.assertEqual((order.state, parent.state, parent.payment_id, parent.invoice_ids),
                         ("sale", "done", original_payment, original_invoice))
        self.assertFalse(parent.child_transaction_ids)
        self.client.refunds.create.assert_called_once_with(
            invoice_id=_INVOICE_ID, mode="overpayment", external_order_id=operation.external_order_id,
        )

    def test_mismatched_asset_amount_and_destination_do_not_project(self):
        _order, parent, attempt, _invoice = self._settled()
        child = parent._refund()
        operation = self._operation(attempt)
        snapshot = self._snapshot(operation)
        for invalid in (
            snapshot | {"currency": "USDT"},
            snapshot | {"amount": "12.33"},
            snapshot | {"destination_address": "0xother"},
        ):
            self.client.refunds.get.return_value = invalid
            self.client.refunds.list.return_value = {"items": [invalid], "next_cursor": None}
            raw = self._raw("evt_refund_mismatch", "withdrawal.completed", invalid)
            self.assertEqual(self._webhook(raw).status_code, 503)
            self.assertEqual(child.state, "pending")
            self.assertFalse(self._queue(child))
        self.assertFalse(operation.withdrawal_id)
        self.assertFalse(child.payment_id)
        self.client.refunds.create.assert_not_called()

    def test_invalid_refund_binding(self):
        order, parent, attempt, _invoice = self._settled()
        original_payment = parent.payment_id
        child = parent._refund()
        operation = self._operation(attempt)
        snapshot = self._snapshot(operation)
        foreign = (
            snapshot | {"project_id": "prj_foreignrefund"},
            snapshot | {"invoice_id": "inv_foreignrefund"},
        )
        for index, wrong_scope in enumerate(foreign):
            raw = self._raw(f"evt_foreign_refund_{index}", "withdrawal.completed", wrong_scope)
            self.assertEqual(self._webhook(raw).status_code, 401)
        self.client.refunds.get.assert_not_called()

        self.client.refunds.list.return_value = {"items": [snapshot], "next_cursor": None}
        for index, wrong_scope in enumerate(foreign):
            self.client.refunds.get.return_value = wrong_scope
            raw = self._raw(f"evt_foreign_reverse_{index}", "withdrawal.completed", snapshot)
            self.assertEqual(self._webhook(raw).status_code, 503)
            self.assertFalse(self._queue(child))

        for _ in range(refund.MAX_CHECKS):
            refund.run_operation(operation)
        self.assertEqual((operation.state, operation.review_reason), ("review", "refund_snapshot_invalid"))
        self.assertFalse(operation.withdrawal_id)
        self.assertEqual((child.state, parent.state, order.state), ("pending", "done", "sale"))
        self.assertEqual(parent.payment_id, original_payment)
        self.assertFalse(child.payment_id)
        self.assertFalse(self._queue(child))
        self.client.refunds.create.assert_not_called()

    def test_lost_invoice_recovery_only_reads_api(self):
        order, tx, attempt, invoice = self._prepared()
        self.assertFalse(attempt.invoice_id)
        self.client.invoices.list.return_value = {"items": [{"invoice_id": _INVOICE_ID}], "next_cursor": None}
        refund.recover_invoice(attempt)
        self.assertEqual(attempt.invoice_id, _INVOICE_ID)
        self.client.invoices.list.assert_called_once_with(external_order_id=attempt.external_order_id, limit=2)
        self.client.invoices.create.assert_not_called()
        self._native_apply(tx)
        self.assertEqual((tx.state, order.state, attempt.disposition), ("done", "sale", "paid"))

    def test_stale_refunds_enabled_false_allows_refund_with_valid_contract(self):
        disabled = self._connection("sandbox", refunds_enabled=False, generation=2)
        self.provider.write({"paymos_active_connection_id": disabled.id})
        _order, parent, attempt, _invoice = self._settled()

        actor = self._actor(parent)
        actor.action_refund(amount_to_refund=12.34)
        operation = self._operation(attempt)
        child = operation.child_transaction_id
        self.assertEqual((operation.state, child.operation, child.amount), ("to_send", "refund", -12.34))

        completed = self._snapshot(operation)
        self.client.refunds.create.return_value = completed
        self.client.refunds.get.return_value = completed
        refund.run_operation(operation)
        self.assertEqual(operation.state, "queued")
        self.assertFalse(operation.review_reason)
        self.client.refunds.create.assert_called_once_with(
            invoice_id=_INVOICE_ID, mode="full", external_order_id=operation.external_order_id,
        )

        self.client.refunds.list.return_value = {"items": [completed], "next_cursor": None}
        self._native_apply(child)
        self.assertEqual((child.state, operation.state), ("done", "completed"))

        invalid = connect.Credentials(
            _PROJECT, "sandbox", "pk_test_refund_fixture", "sk_test_refund_fixture",
            "whsec_test_refund_fixture", ("invoice_refunds_v1",), None, False,
        )
        with self.assertRaisesRegex(refund.RefundSnapshotError, "refund_contract_unavailable"):
            refund._requires_contract(invalid)

    def test_refund_history_accepts_200_utf16_units_and_rejects_over_limit(self):
        _order, parent, attempt, _invoice = self._settled()
        valid_refs = ("a" * 128, "b" * 129, "c" * 200, "я" * 150, "я" * 200, "😀" * 100)
        for index, ref in enumerate(valid_refs):
            item = {
                "type": "refund", "withdrawal_id": f"wdr_valid{index}", "invoice_id": _INVOICE_ID,
                "project_id": _PROJECT, "is_test": True, "mode": "full",
                "external_order_id": ref, "status": "failed", "is_final": True,
            }
            self.assertEqual(refund._refund_item(item, attempt, _INVOICE_ID)["external_order_id"], ref)

        for index, too_long in enumerate(("a" * 201, "я" * 201, "😀" * 100 + "a", "😀" * 101)):
            item = {
                "type": "refund", "withdrawal_id": f"wdr_over{index}", "invoice_id": _INVOICE_ID,
                "project_id": _PROJECT, "is_test": True, "mode": "full",
                "external_order_id": too_long, "status": "failed", "is_final": True,
            }
            with self.assertRaisesRegex(refund.RefundSnapshotError, "refund_snapshot_invalid"):
                refund._refund_item(item, attempt, _INVOICE_ID)
