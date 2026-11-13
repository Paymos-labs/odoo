from odoo import api, fields, models
from odoo.addons.payment import utils as payment_utils
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request
from odoo.orm.models import NO_ACCESS

from ..services import connect


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(selection_add=[("paymos", "Paymos")], ondelete={"paymos": "set default"})
    paymos_pending_envelope = fields.Text(copy=False, groups=NO_ACCESS)
    paymos_active_connection_id = fields.Many2one(
        "paymos.connection", copy=False, groups=NO_ACCESS, ondelete="set null", check_company=True,
    )
    paymos_connection_status = fields.Selection(
        [("disconnected", "Disconnected"), ("connected", "Connected")],
        string="Paymos connection",
        compute="_compute_paymos_connection_summary",
        groups="base.group_system",
    )
    paymos_project_alias = fields.Char(
        string="Paymos project",
        compute="_compute_paymos_connection_summary",
        groups="base.group_system",
    )

    @api.depends("code")
    def _compute_feature_support_fields(self):
        super()._compute_feature_support_fields()
        self.filtered(lambda provider: provider.code == "paymos").support_refund = "full_only"

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code == "paymos":
            return {"paymos"}
        return super()._get_default_payment_method_codes()

    @api.depends("paymos_active_connection_id", "company_id", "code")
    @api.depends_context("uid", "allowed_company_ids")
    def _compute_paymos_connection_summary(self):
        allowed_companies = self.env.companies
        for provider in self:
            connection = (
                provider.sudo().paymos_active_connection_id
                if provider.code == "paymos" and provider.company_id in allowed_companies
                else False
            )
            provider.paymos_connection_status = "connected" if connection else "disconnected"
            provider.paymos_project_alias = f"…{connection.project_id[-6:]}" if connection else False

    @api.model_create_multi
    def create(self, vals_list):
        default_code = (
            self.default_get(["code"]).get("code")
            if any("code" not in values for values in vals_list)
            else None
        )
        return super().create([
            dict(values, active=False, is_live=False, is_published=False)
            if values.get("code", default_code) == "paymos" else values
            for values in vals_list
        ])

    def copy_data(self, default=None):
        values_list = super().copy_data(default)
        for provider, values in zip(self, values_list):
            if provider.code == "paymos" and values is not None:
                values.update(active=False, is_live=False, is_published=False)
                values.pop("paymos_pending_envelope", None)
                values.pop("paymos_active_connection_id", None)
        return values_list

    def copy(self, default=None):
        copies = super().copy(default)
        paymos_copies = copies.filtered(lambda provider: provider.code == "paymos")
        if paymos_copies:
            paymos_copies.payment_method_ids.active = False
        return copies

    @api.constrains("paymos_active_connection_id", "company_id", "code", "is_live")
    def _check_paymos_active_connection(self):
        for provider in self.sudo():
            connection = provider.paymos_active_connection_id
            if connection and (
                provider.code != "paymos"
                or connection.provider_id != provider
                or connection.company_id != provider.company_id
                or connection.environment != ("live" if provider.is_live else "sandbox")
            ):
                raise ValidationError(self.env._("The selected Paymos connection does not match this provider."))

    def write(self, vals):
        native_flags = {"active", "is_published"} & vals.keys()
        identity = {"company_id", "code", "is_live"} & vals.keys()
        if (native_flags or identity) and "payment_paymos" not in self.env.registry.uninstalling_modules:
            self.check_access("write")
            if identity:
                self._paymos_check_identity_write(vals)
            if native_flags:
                self._paymos_check_publication_write(vals, native_flags)
        return super().write(vals)

    def _paymos_check_identity_write(self, vals):
        for provider in self:
            if "code" in vals and provider.code != vals["code"] and (
                provider.code == "paymos" or vals["code"] == "paymos"
            ):
                raise UserError(self.env._("An existing payment provider cannot change to or from Paymos."))
            if provider.code != "paymos":
                continue
            if "company_id" in vals and provider.company_id.id != vals["company_id"]:
                raise UserError(self.env._("Duplicate Paymos to configure a different company."))
            if not self.env.su and "is_live" in vals and provider.is_live != vals["is_live"]:
                raise AccessError(self.env._("Select the Paymos environment in its connection settings."))

    def _paymos_check_publication_write(self, vals, native_flags):
        changed = self.filtered(lambda provider: provider.code == "paymos" and any(
            bool(vals[field]) != provider[field] for field in native_flags
        ))
        if not changed:
            return
        if not self.env.su:
            changed._paymos_check_admin_access()
            if "paymos_active_connection_id" in vals:
                changed.check_field_access(changed._fields["paymos_active_connection_id"], "write")
        enabling = changed.filtered(lambda provider: any(
            vals[field] and not provider[field] for field in native_flags
        ))
        for provider in enabling.sudo():
            if not provider._paymos_current_connection(vals):
                raise UserError(self.env._("Connect Paymos to this website before enabling or publishing it."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_paymos_connection(self):
        if self.env["paymos.connection"].sudo().search_count(
            [("provider_id", "in", self.ids)], limit=1,
        ):
            raise UserError(self.env._("A configured Paymos provider must be retained."))

    def _paymos_source_url(self, website=None):
        self.ensure_one()
        website = self.website_id if website is None else website
        if website and website.company_id != self.company_id:
            raise connect.ConnectError("invalid_source")
        configured_url = website.domain or self.env["ir.config_parameter"].sudo().get_str("web.base.url")
        return connect.canonical_source(configured_url)

    def _paymos_current_connection(self, vals=None):
        self.ensure_one()
        vals = vals or {}
        connections = self.env["paymos.connection"]
        connection = self.paymos_active_connection_id
        if "paymos_active_connection_id" in vals:
            connection = connections.browse(vals["paymos_active_connection_id"]).exists()
        is_live = vals.get("is_live", self.is_live)
        if (
            not connection
            or connection.provider_id != self
            or connection.company_id != self.company_id
            or connection.environment != ("live" if is_live else "sandbox")
        ):
            return connections
        website = self.env["website"].browse(vals["website_id"]) if "website_id" in vals else None
        try:
            if connection.source_url == self._paymos_source_url(website):
                return connection
        except connect.ConnectError:
            pass
        return connections

    def _paymos_checkout_connection(self):
        self.ensure_one()
        provider = self.sudo()
        if provider.code == "paymos" and provider.active and provider.is_published:
            return provider._paymos_current_connection()
        return provider.env["paymos.connection"]

    @api.model
    def _find_available_providers(self, company_id, *args, is_validation=False, report=None, **kwargs):
        providers = super()._find_available_providers(
            company_id, *args, is_validation=is_validation, report=report, **kwargs,
        )
        unavailable = providers.filtered(lambda provider: provider.code == "paymos" and (
            is_validation
            or provider.company_id.id != company_id
            or not provider._paymos_checkout_connection()
        ))
        payment_utils.add_to_report(
            report, unavailable, available=False, reason=self.env._("Paymos is unavailable for this checkout."),
        )
        return providers - unavailable

    def _get_redirect_form_view(self, is_validation=False):
        self.ensure_one()
        if self.code != "paymos":
            return super()._get_redirect_form_view(is_validation=is_validation)
        if is_validation:
            return self.env["ir.ui.view"]
        return self.env.ref("payment_paymos.redirect_form")

    def _paymos_check_admin_access(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only a system administrator can manage Paymos."))
        self.check_access("write")
        companies = self.env.companies
        if any(provider.code != "paymos" or provider.company_id not in companies for provider in self):
            raise AccessError(self.env._("This Paymos provider is outside your active companies."))

    def _paymos_lock_for_admin(self):
        self.ensure_one()
        self._paymos_check_admin_access()
        if not request or request.session.uid != self.env.uid or not request.session.sid:
            raise AccessError(self.env._("Sign in to Odoo to manage the Paymos connection."))

        session_id = request.session.sid
        provider = self.sudo().try_lock_for_update(allow_referencing=True)
        if not provider:
            raise UserError(self.env._("Paymos settings are being updated. Try again."))
        return provider, session_id

    def _paymos_device_binding(self, user_id, session_id):
        return connect.DeviceBinding(self.id, self.company_id.id, user_id, self._paymos_source_url(), session_id)

    def _paymos_connect_error(self, error):
        if error.code == "invalid_source":
            return UserError(self.env._("Configure a canonical HTTPS domain for this Odoo website."))
        if error.code == "encryption_key_unavailable":
            return UserError(self.env._("The Paymos installation encryption key is unavailable."))
        if error.code in ("binding_mismatch", "invalid_envelope", "device_expired"):
            return UserError(self.env._("This Paymos approval no longer matches the current connection. Start again."))
        return UserError(self.env._("Paymos could not verify this connection. Start again."))

    def action_paymos_connect(self):
        provider, session_id = self._paymos_lock_for_admin()
        try:
            binding = provider._paymos_device_binding(self.env.uid, session_id)
            challenge = connect.DeviceConnect().start(binding)
        except connect.ConnectError as error:
            raise self._paymos_connect_error(error) from None

        provider.paymos_pending_envelope = challenge.envelope
        return {
            "type": "ir.actions.client",
            "tag": "payment_paymos.connect",
            "params": {
                "provider_id": self.id,
                "flow_id": challenge.flow_id,
                "user_code": challenge.user_code,
                "verification_url": challenge.verification_url,
                "approval_origin": connect.CONNECT_ORIGIN,
                "retry_after": challenge.retry_after,
            },
        }

    def action_paymos_poll(self, flow_id):
        provider, session_id = self._paymos_lock_for_admin()
        token = provider.paymos_pending_envelope
        if not token:
            return {"status": "expired"}
        try:
            binding = provider._paymos_device_binding(self.env.uid, session_id)
            result = connect.DeviceConnect().poll(binding, token, flow_id)
            if result.status == "connected":
                status = "connected" if provider._paymos_save_connection(
                    result.credentials, binding.source_url,
                ) else "conflict"
                return {"status": status}
        except connect.ConnectError as error:
            raise self._paymos_connect_error(error) from None

        if result.status == "waiting":
            if result.envelope != token:
                provider.paymos_pending_envelope = result.envelope
            return {"status": "waiting", "retry_after": result.retry_after}
        provider.paymos_pending_envelope = False
        return {"status": result.status}

    def _paymos_save_connection(self, credentials, source_url):
        connection_model = self.env["paymos.connection"]
        own_connections = [("provider_id", "=", self.id), ("company_id", "=", self.company_id.id)]
        project_id = credentials["sandbox"].project_id
        if connection_model.search_count(own_connections + [
            ("source_url", "=", source_url), ("project_id", "!=", project_id),
        ], limit=1):
            self.paymos_pending_envelope = False
            return False

        previous = connection_model.search(own_connections, order="generation desc", limit=1)
        generation = previous.generation + 1 if previous else 1
        binding = {
            "company_id": self.company_id.id,
            "provider_id": self.id,
            "generation": generation,
            "source_url": source_url,
        }
        connections = connection_model.create([
            dict(
                binding,
                project_id=credential.project_id,
                environment=credential.environment,
                credentials_envelope=connect.seal_credentials(credential, **binding),
            )
            for credential in credentials.values()
        ])
        self.write({
            "paymos_pending_envelope": False,
            "paymos_active_connection_id": connections.filtered(lambda row: row.environment == "sandbox").id,
            "is_live": False,
            "active": False,
            "is_published": False,
        })
        return True

    def action_paymos_disconnect(self):
        provider, _session_id = self._paymos_lock_for_admin()
        provider.write({
            "paymos_pending_envelope": False,
            "paymos_active_connection_id": False,
            "is_live": False,
            "active": False,
            "is_published": False,
        })
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def action_paymos_use_sandbox(self):
        return self._paymos_change_environment("sandbox")

    def action_paymos_use_live(self):
        return self._paymos_change_environment("live")

    def _paymos_change_environment(self, environment):
        provider, _session_id = self._paymos_lock_for_admin()
        current = provider.paymos_active_connection_id
        try:
            if not current or current.source_url != provider._paymos_source_url():
                raise connect.ConnectError("binding_mismatch")
        except connect.ConnectError as error:
            raise self._paymos_connect_error(error) from None
        if current.environment != environment:
            sibling = provider.env["paymos.connection"].search([
                ("provider_id", "=", provider.id), ("company_id", "=", provider.company_id.id),
                ("generation", "=", current.generation), ("project_id", "=", current.project_id),
                ("source_url", "=", current.source_url), ("environment", "=", environment),
            ], limit=1)
            if not sibling:
                raise UserError(self.env._("Connect Paymos again to select this environment."))
            provider.write({
                "paymos_active_connection_id": sibling.id,
                "is_live": environment == "live",
                "active": False,
                "is_published": False,
            })
        return {"type": "ir.actions.client", "tag": "soft_reload"}
