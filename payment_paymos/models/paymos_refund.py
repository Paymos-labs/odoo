import re
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import refund


_IMMUTABLE = {
    "company_id", "parent_transaction_id", "child_transaction_id", "attempt_id", "mode",
    "external_order_id",
}


class PaymosRefund(models.Model):
    _name = "paymos.refund"
    _description = "Paymos refund operation"
    _rec_name = "external_order_id"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    parent_transaction_id = fields.Many2one(
        "payment.transaction", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    child_transaction_id = fields.Many2one(
        "payment.transaction", readonly=True, ondelete="restrict", check_company=True,
    )
    attempt_id = fields.Many2one(
        "paymos.attempt", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    mode = fields.Selection(
        [("full", "Full"), ("overpayment", "Overpayment")], required=True, readonly=True,
    )
    external_order_id = fields.Char(required=True, readonly=True, copy=False)
    withdrawal_id = fields.Char(readonly=True, copy=False)
    state = fields.Selection(
        [
            ("to_send", "Scheduled"), ("unknown", "Awaiting API reconciliation"),
            ("pending", "Pending"), ("queued", "Queued for Odoo"),
            ("completed", "Completed"), ("failed", "Failed"), ("cancelled", "Cancelled"),
            ("review", "Needs review"),
        ],
        required=True, readonly=True, default="to_send", index=True,
    )
    token_amount = fields.Char(readonly=True)
    token_currency = fields.Char(readonly=True)
    token_network = fields.Char(readonly=True)
    review_reason = fields.Char(readonly=True)
    retry_at = fields.Datetime(default=fields.Datetime.now, readonly=True, index=True)
    retry_count = fields.Integer(readonly=True)

    _attempt_mode = models.Constraint("UNIQUE(attempt_id, mode)", "This Paymos refund already exists.")
    _native_child = models.Constraint("UNIQUE(child_transaction_id)", "This refund transaction is already linked.")
    _external_order = models.Constraint("UNIQUE(external_order_id)", "This Paymos refund reference already exists.")
    _withdrawal = models.Constraint("UNIQUE(withdrawal_id)", "This Paymos withdrawal is already linked.")

    @api.constrains("company_id", "parent_transaction_id", "child_transaction_id", "attempt_id", "mode")
    def _check_refund_binding(self):
        for operation in self:
            if not refund.native_operation_matches(operation):
                raise ValidationError(self.env._("The Paymos refund does not match its original payment."))

    def write(self, vals):
        self.check_access("write")
        if _IMMUTABLE & vals.keys():
            raise UserError(self.env._("The original Paymos refund binding cannot be changed."))
        for operation in self:
            if any(operation[field] and operation[field] != vals[field] for field in (
                "withdrawal_id", "token_amount", "token_currency", "token_network",
            ) if field in vals):
                raise UserError(self.env._("The verified Paymos withdrawal cannot be replaced."))
        if vals.get("review_reason") and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", vals["review_reason"]):
            raise ValidationError(self.env._("The Paymos review reason must be a diagnostic code."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_uninstall(self):
        if self:
            raise UserError(self.env._("Paymos refunds must be retained."))

    def _paymos_defer(self, reason, *, state=None):
        self.ensure_one()
        count = self.retry_count + 1
        self.write({
            "retry_count": count,
            "retry_at": fields.Datetime.now() + timedelta(minutes=min(60, 2 ** min(count - 1, 6))),
            "review_reason": reason,
            "state": "review" if count >= refund.MAX_CHECKS else state or self.state,
        })

    @api.model
    def _cron_paymos_operations(self):
        cron = self.env["ir.cron"]
        operations = self.sudo().search([
            ("state", "in", ("to_send", "unknown", "pending", "queued")),
            ("retry_at", "<=", fields.Datetime.now()),
        ], limit=refund.CRON_BATCH, order="retry_at, id")
        attempts = self.env["paymos.attempt"].sudo().search([
            ("disposition", "in", ("prepared", "open", "review")),
            ("paymos_reconcile_after", "<=", fields.Datetime.now()),
            ("paymos_reconcile_count", "<", refund.MAX_CHECKS),
        ], limit=refund.CRON_BATCH, order="paymos_reconcile_after, id")

        cron._commit_progress(remaining=len(operations) + len(attempts))
        for operation in operations:
            with self.env.cr.savepoint():
                refund.run_operation(operation)
            if not cron._commit_progress(processed=1):
                return
        for attempt in attempts:
            with self.env.cr.savepoint():
                refund.recover_invoice(attempt)
            if not cron._commit_progress(processed=1):
                return


class PaymosAttemptRecovery(models.Model):
    _inherit = "paymos.attempt"

    paymos_reconcile_after = fields.Datetime(default=fields.Datetime.now, readonly=True, index=True)
    paymos_reconcile_count = fields.Integer(readonly=True)


class PaymosRefundEvent(models.Model):
    _inherit = "paymos.event"

    @api.constrains(
        "attempt_id", "transaction_id", "connection_id", "company_id", "provider_id", "kind",
        "refund_operation_id", "project_id", "environment", "generation", "amount", "currency",
        "invoice_id", "external_order_id", "provider_reference", "raw_sha256",
    )
    def _check_binding(self):
        excess = self.filtered(
            lambda marker: marker.kind == "refund" and marker.target_state == "observe"
            and marker.transaction_id == marker.attempt_id.transaction_id
        )
        super(PaymosRefundEvent, self - excess)._check_binding()
        for marker in excess:
            operation = self.env["paymos.refund"].sudo().browse(marker.refund_operation_id).exists()
            attempt = marker.attempt_id
            if (
                not operation or operation.mode != "overpayment" or operation.attempt_id != attempt
                or marker.company_id != attempt.company_id or marker.provider_id != attempt.provider_id
                or marker.connection_id != attempt.connection_id or marker.project_id != attempt.project_id
                or marker.environment != attempt.environment or marker.generation != attempt.generation
                or marker.amount != attempt.amount or marker.currency != attempt.currency
                or marker.invoice_id != attempt.invoice_id or marker.external_order_id != operation.external_order_id
                or marker.provider_reference != operation.withdrawal_id
                or not re.fullmatch(r"[a-f0-9]{64}", marker.raw_sha256)
            ):
                raise ValidationError(self.env._("The verified Paymos refund event does not match its operation."))
