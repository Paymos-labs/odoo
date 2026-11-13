import re

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class PaymosEvent(models.Model):
    _name = "paymos.event"
    _description = "Verified Paymos event"
    _rec_name = "event_id"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    provider_id = fields.Many2one(
        "payment.provider", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    connection_id = fields.Many2one(
        "paymos.connection", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    attempt_id = fields.Many2one(
        "paymos.attempt", required=True, readonly=True, ondelete="restrict", check_company=True, index=True,
    )
    transaction_id = fields.Many2one(
        "payment.transaction", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    project_id = fields.Char(required=True, readonly=True)
    environment = fields.Selection(
        [("sandbox", "Sandbox"), ("live", "Live")], required=True, readonly=True,
    )
    generation = fields.Integer(required=True, readonly=True)
    event_id = fields.Char(required=True, readonly=True, copy=False)
    raw_sha256 = fields.Char(required=True, readonly=True, copy=False)
    kind = fields.Selection([("invoice", "Invoice"), ("refund", "Refund")], required=True, readonly=True)
    refund_operation_id = fields.Integer(readonly=True)
    invoice_id = fields.Char(required=True, readonly=True)
    external_order_id = fields.Char(required=True, readonly=True)
    provider_reference = fields.Char(required=True, readonly=True)
    provider_status = fields.Char(required=True, readonly=True)
    target_state = fields.Selection(
        [("observe", "Observe"), ("done", "Confirm"), ("cancel", "Cancel")],
        required=True, readonly=True,
    )
    amount = fields.Char(required=True, readonly=True)
    currency = fields.Char(required=True, readonly=True)
    token_currency = fields.Char(readonly=True)
    token_network = fields.Char(readonly=True)
    token_expected = fields.Char(readonly=True)
    token_paid = fields.Char(readonly=True)
    consumed = fields.Boolean(readonly=True, copy=False)

    _delivery_identity = models.Constraint(
        "UNIQUE(environment, project_id, event_id)", "This Paymos delivery already exists.",
    )

    @api.constrains(
        "attempt_id", "transaction_id", "connection_id", "company_id", "provider_id", "kind",
        "refund_operation_id", "project_id", "environment", "generation", "amount", "currency",
        "invoice_id", "external_order_id", "provider_reference", "raw_sha256",
    )
    def _check_binding(self):
        for marker in self:
            attempt = marker.attempt_id
            transaction = marker.transaction_id
            same_attempt = (
                marker.company_id == attempt.company_id
                and marker.provider_id == attempt.provider_id
                and marker.connection_id == attempt.connection_id
                and marker.project_id == attempt.project_id
                and marker.environment == attempt.environment
                and marker.generation == attempt.generation
                and marker.amount == attempt.amount
                and marker.currency == attempt.currency
                and marker.invoice_id == attempt.invoice_id
                and transaction.company_id == attempt.company_id
                and transaction.provider_id == attempt.provider_id
            )
            if marker.kind == "invoice":
                same_operation = (
                    not marker.refund_operation_id
                    and transaction == attempt.transaction_id
                    and marker.external_order_id == attempt.external_order_id
                    and marker.provider_reference == attempt.invoice_id
                )
            else:
                same_operation = (
                    marker.refund_operation_id > 0
                    and transaction.operation == "refund"
                    and transaction.source_transaction_id == attempt.transaction_id
                    and marker.provider_reference.startswith("wdr_")
                )
            if not same_attempt or not same_operation or not re.fullmatch(r"[a-f0-9]{64}", marker.raw_sha256):
                raise ValidationError(self.env._("The verified Paymos event does not match its payment binding."))

    def write(self, vals):
        self.check_access("write")
        if vals and vals != {"consumed": True}:
            raise UserError(self.env._("The verified Paymos event cannot be changed."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_uninstall(self):
        if self:
            raise UserError(self.env._("The verified Paymos event must be retained."))
