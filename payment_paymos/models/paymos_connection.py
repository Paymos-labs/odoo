from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.orm.models import NO_ACCESS


class PaymosConnection(models.Model):
    _name = "paymos.connection"
    _description = "Paymos connection generation"
    _rec_name = "generation"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    provider_id = fields.Many2one(
        "payment.provider", required=True, readonly=True, ondelete="cascade", check_company=True,
    )
    generation = fields.Integer(required=True, readonly=True)
    project_id = fields.Char(required=True, readonly=True)
    environment = fields.Selection(
        [("sandbox", "Sandbox"), ("live", "Live")], required=True, readonly=True,
    )
    source_url = fields.Char(required=True, readonly=True)
    credentials_envelope = fields.Text(required=True, readonly=True, copy=False, groups=NO_ACCESS)

    _environment_generation = models.Constraint(
        "UNIQUE(provider_id, generation, environment)",
        "This Paymos connection generation and environment already exist.",
    )
    _positive_generation = models.Constraint(
        "CHECK(generation > 0)", "A Paymos connection generation must be positive.",
    )

    @api.constrains("company_id", "provider_id")
    def _check_provider_binding(self):
        for connection in self:
            if (
                connection.provider_id.code != "paymos"
                or connection.provider_id.company_id != connection.company_id
            ):
                raise ValidationError(self.env._("The Paymos connection does not match the provider company."))

    def write(self, vals):
        self.check_access("write")
        if self and vals:
            raise UserError(self.env._("A Paymos connection generation cannot be changed."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_uninstall(self):
        if self:
            raise UserError(self.env._("A Paymos connection generation must be retained."))
