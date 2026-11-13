from unittest.mock import patch

from psycopg2.errors import UniqueViolation

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.payment_paymos import uninstall_hook


@tagged("post_install", "-at_install")
class TestPaymosInstall(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider_a = cls.env.ref("payment_paymos.payment_provider_paymos")
        cls.method_a = cls.env.ref("payment_paymos.payment_method_paymos")
        cls.company_a = cls.provider_a.company_id
        cls.company_b = cls.env["res.company"].create({
            "name": "Paymos Install Company B",
            "parent_id": cls.company_a.id,
        })
        cls.provider_b = cls.provider_a.copy({"company_id": cls.company_b.id})
        cls.admin = new_test_user(
            cls.env,
            login="paymos-install-admin",
            groups="base.group_user,base.group_system",
            company_id=cls.company_a.id,
            company_ids=[Command.set((cls.company_a | cls.company_b).ids)],
        )
        cls.employee = new_test_user(
            cls.env,
            login="paymos-install-employee",
            company_id=cls.company_a.id,
            company_ids=[Command.set(cls.company_a.ids)],
        )
        cls.partner = cls.env["res.partner"].create({"name": "Paymos Install Customer"})

    def _connection_values(self, provider, generation=1, environment="sandbox"):
        return {
            "company_id": provider.company_id.id,
            "provider_id": provider.id,
            "generation": generation,
            "project_id": f"test-project-{provider.id}",
            "environment": environment,
            "source_url": "https://odoo.example.test",
            "credentials_envelope": "test-ciphertext",
        }

    def _connection(self, provider, generation=1, environment="sandbox"):
        return self.env["paymos.connection"].create(
            self._connection_values(provider, generation, environment)
        )

    def _order(self, provider):
        return self.env["sale.order"].with_company(provider.company_id).create({
            "partner_id": self.partner.id,
            "company_id": provider.company_id.id,
        })

    def _transaction(self, provider, order, suffix):
        return self.env["payment.transaction"].create({
            "provider_id": provider.id,
            "payment_method_id": provider.payment_method_ids.id,
            "reference": f"Paymos-Install-{suffix}",
            "partner_id": order.partner_id.id,
            "currency_id": order.currency_id.id,
            "amount": 12.34,
            "operation": "online_redirect",
            "sale_order_ids": [Command.set(order.ids)],
        })

    def _attempt_values(self, connection, order, tx, generation=1):
        return {
            "company_id": connection.company_id.id,
            "provider_id": connection.provider_id.id,
            "connection_id": connection.id,
            "sale_order_id": order.id,
            "transaction_id": tx.id,
            "project_id": connection.project_id,
            "environment": connection.environment,
            "generation": generation,
            "external_order_id": f"test-order-{tx.id}",
            "amount": "12.34",
            "currency": tx.currency_id.name,
        }

    def test_initial_state_and_company_scope(self):
        self.assertEqual(self.method_a.provider_id, self.provider_a)
        self.assertTrue(self.method_a.image)
        attempts = {}
        connections = {}
        for provider in self.provider_a | self.provider_b:
            self.assertEqual(provider.code, "paymos")
            self.assertEqual(provider._get_default_payment_method_codes(), {"paymos"})
            self.assertFalse(provider.active)
            self.assertFalse(provider.is_live)
            self.assertFalse(provider.is_published)
            self.assertFalse(provider.paymos_active_connection_id)
            self.assertFalse(provider.paymos_pending_envelope)
            self.assertEqual(provider.payment_method_ids.code, "paymos")
            self.assertFalse(provider.payment_method_ids.active)

            connection = connections[provider.id] = self._connection(provider)
            order = self._order(provider)
            tx = self._transaction(provider, order, provider.id)
            attempt = attempts[provider.id] = self.env["paymos.attempt"].create(
                self._attempt_values(connection, order, tx)
            )
            self.assertEqual(attempt.disposition, "prepared")
            self.assertEqual(order.state, "draft")
            self.assertEqual(tx.state, "draft")

        for own_provider, foreign_provider in (
            (self.provider_a, self.provider_b), (self.provider_b, self.provider_a),
        ):
            company = own_provider.company_id
            own_attempt = attempts[own_provider.id]
            foreign_attempt = attempts[foreign_provider.id]
            scoped_attempts = self.env["paymos.attempt"].with_user(self.admin).with_context(
                allowed_company_ids=company.ids,
            )
            scoped_provider = own_provider.with_user(self.admin).with_context(
                allowed_company_ids=company.ids,
            )
            self.assertEqual(scoped_attempts.search([]).ids, own_attempt.ids)
            with self.assertRaises(AccessError):
                scoped_attempts.browse(foreign_attempt.id).read(["amount"])
            with self.assertRaises(AccessError):
                scoped_attempts.browse(own_attempt.id).write({"disposition": "paid"})
            with self.assertRaises(AccessError):
                scoped_attempts.create(self._attempt_values(
                    connections[own_provider.id], own_attempt.sale_order_id, own_attempt.transaction_id,
                ))
            self.assertNotIn("paymos_pending_envelope", scoped_provider.fields_get())
            with self.assertRaises(AccessError):
                scoped_provider.read(["paymos_pending_envelope"])
            with self.assertRaises(AccessError):
                scoped_provider.write({"paymos_pending_envelope": "untrusted-ciphertext"})
            with self.assertRaises(AccessError):
                scoped_provider.write({"paymos_active_connection_id": connections[own_provider.id].id})
            with self.assertRaises(AccessError):
                connections[own_provider.id].with_user(self.admin).read(["credentials_envelope"])

        with self.assertRaises(AccessError):
            self.env["paymos.attempt"].with_user(self.employee).search([])
        with self.assertRaises(UserError):
            self.env["paymos.connection"].create(dict(
                self._connection_values(self.provider_a, generation=2),
                company_id=self.company_b.id,
            ))
        with self.assertRaises(UserError):
            self.provider_a.paymos_active_connection_id = connections[self.provider_b.id]

    def test_connection_generations_and_order_uniqueness(self):
        sandbox = self._connection(self.provider_a)
        live = self._connection(self.provider_a, environment="live")
        self.assertEqual(sandbox.generation, live.generation)
        with self.assertRaises(UniqueViolation):
            self._connection(self.provider_a)
        with self.assertRaises(UserError):
            sandbox.write({"credentials_envelope": "different-ciphertext"})
        with self.assertRaises(ValidationError):
            self.provider_a.paymos_active_connection_id = live
        self.provider_a.paymos_active_connection_id = sandbox

        order = self._order(self.provider_a)
        first_tx = self._transaction(self.provider_a, order, "first")
        self.provider_a.write({"paymos_active_connection_id": live.id, "is_live": True})
        replacement_tx = self._transaction(self.provider_a, order, "replacement")
        attempt_model = self.env["paymos.attempt"]
        first = attempt_model.create(self._attempt_values(sandbox, order, first_tx))
        first.invoice_id = "test-invoice"
        with self.assertRaises(UserError):
            first.invoice_id = "other-invoice"
        with self.assertRaises(UserError):
            first.amount = "12.33"
        replacement_values = self._attempt_values(live, order, replacement_tx, generation=2)
        with self.assertRaises(UniqueViolation):
            attempt_model.create(replacement_values)
        first.disposition = "expired"
        replacement = attempt_model.create(replacement_values)
        first.review_reason = "paid_late"
        self.assertEqual(first.disposition, "expired")
        self.assertEqual(first.review_reason, "paid_late")
        self.assertEqual(first.connection_id, sandbox)
        self.assertEqual(replacement.connection_id, live)
        self.assertEqual(replacement.generation, 2)
        self.assertEqual(attempt_model.search([
            ("sale_order_id", "=", order.id),
            ("disposition", "not in", ["cancelled", "expired"]),
        ]), replacement)
        with self.assertRaises(UserError):
            first.disposition = "review"

    def test_uninstall_hook_preserves_native_history(self):
        connection = self._connection(self.provider_a)
        self.provider_a.write({
            "paymos_active_connection_id": connection.id,
            "paymos_pending_envelope": "test-pending-ciphertext",
        })
        order = self._order(self.provider_a)
        tx = self._transaction(self.provider_a, order, "uninstall")
        with patch.object(self.env.registry, "uninstalling_modules", {"payment_paymos"}):
            uninstall_hook(self.env)

        self.assertTrue(order.exists())
        self.assertEqual(order.state, "draft")
        self.assertTrue(tx.exists())
        self.assertEqual(tx.sale_order_ids, order)
        self.assertEqual(tx.provider_id, self.provider_a)
        self.assertEqual(tx.payment_method_id, self.method_a)
        self.assertFalse(self.provider_a.active)
        self.assertEqual(self.provider_a.code, "none")
        self.assertFalse(self.provider_a.is_published)
        self.assertFalse(self.provider_a.paymos_pending_envelope)
        self.assertFalse(self.provider_a.paymos_active_connection_id)
        self.assertFalse(self.method_a.active)
        self.assertFalse(self.env.ref("payment_paymos.payment_provider_paymos", raise_if_not_found=False))
        self.assertFalse(self.env.ref("payment_paymos.payment_method_paymos", raise_if_not_found=False))
