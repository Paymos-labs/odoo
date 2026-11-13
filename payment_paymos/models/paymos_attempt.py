import re

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


_REPLACEABLE = ("cancelled", "expired")
_BINDING_FIELDS = {
    "company_id", "provider_id", "connection_id", "sale_order_id", "transaction_id",
    "project_id", "environment", "generation", "external_order_id", "amount", "currency",
}


class PaymosAttempt(models.Model):
    _name = "paymos.attempt"
    _description = "Paymos payment attempt"
    _rec_name = "external_order_id"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    provider_id = fields.Many2one(
        "payment.provider", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    connection_id = fields.Many2one(
        "paymos.connection", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    sale_order_id = fields.Many2one(
        "sale.order", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    transaction_id = fields.Many2one(
        "payment.transaction", required=True, readonly=True, ondelete="restrict", check_company=True,
    )
    project_id = fields.Char(required=True, readonly=True)
    environment = fields.Selection(
        [("sandbox", "Sandbox"), ("live", "Live")], required=True, readonly=True,
    )
    generation = fields.Integer(required=True, readonly=True)
    external_order_id = fields.Char(required=True, readonly=True, copy=False)
    invoice_id = fields.Char(readonly=True, copy=False)
    amount = fields.Char(required=True, readonly=True)
    currency = fields.Char(required=True, readonly=True)
    disposition = fields.Selection(
        [
            ("prepared", "Prepared"),
            ("open", "Open"),
            ("review", "Needs review"),
            ("paid", "Paid"),
            ("cancelled", "Cancelled"),
            ("expired", "Expired"),
        ],
        required=True,
        readonly=True,
        default="prepared",
    )
    review_reason = fields.Char(readonly=True, copy=False)

    _order_generation = models.Constraint(
        "UNIQUE(sale_order_id, generation)", "This Paymos order generation already exists.",
    )
    _transaction = models.Constraint(
        "UNIQUE(transaction_id)", "This payment transaction already has a Paymos attempt.",
    )
    _external_order = models.Constraint(
        "UNIQUE(external_order_id)", "This Paymos external order ID already exists.",
    )
    _invoice = models.Constraint(
        "UNIQUE(environment, project_id, invoice_id)", "This Paymos invoice is already linked.",
    )
    _positive_generation = models.Constraint(
        "CHECK(generation > 0)", "A Paymos order generation must be positive.",
    )
    _current_sale_order = models.UniqueIndex(
        "(sale_order_id) WHERE disposition NOT IN ('cancelled', 'expired')",
        "This order already has a current Paymos attempt.",
    )

    @api.constrains(
        "company_id", "provider_id", "connection_id", "sale_order_id", "transaction_id",
        "project_id", "environment", "amount", "currency",
    )
    def _check_binding(self):
        for attempt in self:
            connection = attempt.connection_id
            transaction = attempt.transaction_id
            if (
                attempt.provider_id.code != "paymos"
                or attempt.company_id != attempt.provider_id.company_id
                or attempt.company_id != connection.company_id
                or attempt.company_id != attempt.sale_order_id.company_id
                or attempt.company_id != transaction.company_id
                or attempt.provider_id != connection.provider_id
                or attempt.provider_id != transaction.provider_id
                or attempt.provider_id != transaction.payment_method_id.provider_id
                or attempt.sale_order_id != transaction.sale_order_ids
                or attempt.project_id != connection.project_id
                or attempt.environment != connection.environment
                or attempt.environment != ("live" if transaction.is_live else "sandbox")
                or attempt.currency != transaction.currency_id.name
            ):
                raise ValidationError(self.env._("The Paymos attempt does not match the saved order and connection."))
            if (
                not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", attempt.amount)
                or not re.search(r"[1-9]", attempt.amount)
            ):
                raise ValidationError(self.env._("The Paymos attempt must contain a positive decimal amount."))

    @api.constrains("review_reason")
    def _check_review_reason(self):
        for attempt in self:
            if attempt.review_reason and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", attempt.review_reason):
                raise ValidationError(self.env._("The Paymos review reason must be a diagnostic code."))

    def write(self, vals):
        self.check_access("write")
        if self and _BINDING_FIELDS & vals.keys():
            raise UserError(self.env._("The original Paymos order binding cannot be changed."))
        if "invoice_id" in vals and any(
            attempt.invoice_id and attempt.invoice_id != vals["invoice_id"] for attempt in self
        ):
            raise UserError(self.env._("A Paymos attempt cannot be assigned to a different invoice."))
        if "disposition" in vals and any(
            attempt.disposition in _REPLACEABLE and attempt.disposition != vals["disposition"]
            for attempt in self
        ):
            raise UserError(self.env._("A cancelled or expired Paymos attempt cannot be reopened."))
        result = super().write(vals)
        if vals.get("disposition") in _REPLACEABLE:
            # PostgreSQL must see the terminal state before a replacement INSERT.
            self.flush_recordset(["disposition"])
        return result
