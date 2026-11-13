from contextlib import contextmanager
from contextvars import ContextVar

from odoo import api, models
from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.controllers.payment_status import PaymentStatus
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..services import money, payment, refund


_SERVER_SENTINEL = object()
_WRITE_SCOPE = ContextVar("paymos_write_scope", default=None)
_REFUND_CREATE_SCOPE = ContextVar("paymos_refund_create_scope", default=None)
_STATE_FIELDS = frozenset({"state", "state_message", "last_state_change", "is_post_processed"})
_PROJECTION_FIELDS = _STATE_FIELDS | {"provider_reference", "landing_route"}
_POST_PROCESS_FIELDS = frozenset({"is_post_processed", "invoice_ids", "payment_id"})
_BINDING_FIELDS = frozenset({
    "provider_id", "provider_code", "company_id", "payment_method_id", "payment_method_code",
    "amount", "currency_id", "sale_order_ids", "partner_id", "operation", "reference",
    "source_transaction_id", "child_transaction_ids", "refunds_ids", "is_live", "capture_manually",
    "token_id", "tokenize", "landing_route", "payment_data_ids",
})
_PROTECTED_FIELDS = _BINDING_FIELDS | _PROJECTION_FIELDS | _POST_PROCESS_FIELDS
_EMPTY_ON_CREATE = (
    "provider_code", "company_id", "is_live", "is_post_processed", "provider_reference",
    "payment_id", "invoice_ids", "state_message", "last_state_change", "child_transaction_ids",
    "payment_data_ids", "token_id", "tokenize",
)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            provider = self.env["payment.provider"].browse(values.get("provider_id"))
            if provider.code != "paymos":
                prepared.append(values)
                continue

            context = self.env.context
            if (
                values.get("state", "draft") != "draft"
                or context.get("default_state", "draft") != "draft"
                or any(values.get(field) or context.get(f"default_{field}") for field in _EMPTY_ON_CREATE)
                or context.get("default_source_transaction_id")
            ):
                raise AccessError(self.env._("Paymos transactions must be created without a payment result."))

            operation = values.get("operation", context.get("default_operation"))
            if operation == "online_redirect":
                if values.get("source_transaction_id"):
                    raise AccessError(self.env._("Paymos checkout requires an original transaction."))
                currency = self.env["res.currency"].browse(values.get("currency_id"))
                try:
                    money.native_amount(values.get("amount"), currency)
                except money.AmountError:
                    raise ValidationError(self.env._("The Paymos amount does not match the currency precision.")) from None
            elif operation == "refund":
                self._paymos_check_refund_create(values)
            else:
                raise ValidationError(self.env._("Paymos requires a redirected website checkout."))
            prepared.append(dict(values, state="draft", is_post_processed=False))
        return super().create(prepared)

    def _paymos_check_refund_create(self, values):
        parent_id = values.get("source_transaction_id")
        scope = _REFUND_CREATE_SCOPE.get()
        if scope is None or scope[0] is not _SERVER_SENTINEL or scope[1] is not self.env.cr or scope[2] != parent_id:
            raise AccessError(self.env._("Create a Paymos refund from its verified original transaction."))
        parent = self.browse(parent_id)
        amount = values.get("amount")
        if (
            parent.provider_code != "paymos"
            or parent.state != "done"
            or values.get("provider_id") != parent.provider_id.id
            or values.get("payment_method_id") != parent.payment_method_id.id
            or values.get("currency_id") != parent.currency_id.id
            or values.get("partner_id") != parent.partner_id.id
            or values.get("sale_order_ids")
            or self.env.context.get("default_sale_order_ids")
            or type(amount) not in (int, float)
            or amount != -parent.amount
        ):
            raise AccessError(self.env._("The Paymos refund must match the original full payment."))

    @contextmanager
    def _paymos_allow_refund_create(self):
        self.ensure_one()
        token = _REFUND_CREATE_SCOPE.set((_SERVER_SENTINEL, self.env.cr, self.id))
        try:
            yield
        finally:
            _REFUND_CREATE_SCOPE.reset(token)

    @contextmanager
    def _paymos_allow_write(self, field_names):
        token = _WRITE_SCOPE.set((_SERVER_SENTINEL, self.env.cr, frozenset(self.ids), field_names))
        try:
            yield
        finally:
            _WRITE_SCOPE.reset(token)

    def _paymos_write_allowed(self, field_names):
        scope = _WRITE_SCOPE.get()
        return (
            scope is not None
            and scope[0] is _SERVER_SENTINEL
            and scope[1] is self.env.cr
            and set(self.ids) <= scope[2]
            and field_names <= scope[3]
        )

    def write(self, vals):
        protected = _PROTECTED_FIELDS & vals.keys()
        if protected:
            self.check_access("write")
            paymos = self.filtered(lambda tx: tx.provider_code == "paymos")
            if "provider_id" in vals:
                provider = self.env["payment.provider"].sudo().browse(vals["provider_id"])
                if provider.code == "paymos" and self - paymos:
                    raise AccessError(self.env._("An existing transaction cannot change to Paymos."))
            if paymos and not paymos._paymos_write_allowed(protected):
                raise AccessError(self.env._("Verified Paymos processing manages these transaction fields."))
        return super().write(vals)

    @api.model
    def _get_specific_create_values(self, provider_code, values):
        specific = super()._get_specific_create_values(provider_code, values)
        if provider_code == "paymos" and values.get("operation") == "refund":
            parent = self.browse(values["source_transaction_id"])
            specific["is_live"] = parent.is_live
        return specific

    def action_refund(self, amount_to_refund=None):
        paymos = self.filtered(lambda tx: tx.provider_code == "paymos")
        if paymos:
            paymos._paymos_check_admin_access()
        return super().action_refund(amount_to_refund=amount_to_refund)

    def _refund(self, amount_to_refund=None):
        self.ensure_one()
        if self.provider_code != "paymos":
            return super()._refund(amount_to_refund=amount_to_refund)
        try:
            return refund.request_operation(self, "full", amount_to_refund).child_transaction_id
        except (payment.CheckoutError, refund.RefundSnapshotError) as error:
            raise self._paymos_refund_error(error) from None

    def action_paymos_refund_overpayment(self):
        self.ensure_one()
        if self.provider_code != "paymos":
            raise AccessError(self.env._("Select a Paymos payment."))
        self._paymos_check_admin_access()
        try:
            refund.request_operation(self.sudo(), "overpayment")
        except (payment.CheckoutError, refund.RefundSnapshotError) as error:
            raise self._paymos_refund_error(error) from None
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "success", "message": self.env._("The Paymos overpayment refund is recorded.")},
        }

    def _paymos_refund_error(self, error):
        code = str(error)
        if code == "refund_full_only":
            return ValidationError(self.env._("Paymos supports full refunds for the original fiat amount."))
        if code == "refund_no_overpayment":
            return UserError(self.env._("This payment has no verified Paymos overpayment."))
        if code == "refund_needs_review":
            return UserError(self.env._("Review the existing Paymos refund before taking further action."))
        return UserError(self.env._("Paymos could not authorize a refund for this transaction."))

    def _get_specific_rendering_values(self, processing_values):
        values = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "paymos":
            return values
        if not request or not request.session.sid:
            raise ValidationError(self.env._("Refresh the checkout page before paying with Paymos."))
        try:
            attempt = payment.prepare_attempt(self.sudo())
        except payment.CheckoutError as error:
            if error.code == "checkout_busy":
                message = self.env._("Payment for this order is already being prepared. Try again.")
            else:
                message = self.env._("Paymos cannot open payment for this order. Refresh checkout or contact the store.")
            raise ValidationError(message) from None

        transaction = attempt.transaction_id
        transaction._paymos_monitor_checkout()
        token = payment_utils.generate_access_token(
            *payment.start_token_values(transaction.id, attempt.id, request.session.sid), env=self.env,
        )
        return dict(values, start_url=payment.START_URL, tx_id=transaction.id, attempt_id=attempt.id, access_token=token)

    def _paymos_monitor_checkout(self):
        self.ensure_one()
        if request:
            PaymentStatus.monitor_transaction(self)
            request.session["__website_sale_last_tx_id"] = self.id

    def _paymos_mark_checkout_pending(self):
        self.ensure_one()
        if self.state == "draft":
            with self._paymos_allow_write(_STATE_FIELDS):
                self.with_context(payment_safe_write=True)._set_pending()

    def _paymos_attempt(self):
        self.ensure_one()
        return self.env["paymos.attempt"].sudo().search([("transaction_id", "=", self.id)], limit=1)

    def _paymos_check_admin_access(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only a system administrator can reconcile Paymos payments."))
        self.check_access("write")
        if any(tx.company_id not in self.env.companies for tx in self):
            raise AccessError(self.env._("These Paymos transactions are outside your active companies."))

    def action_confirm(self):
        paymos = self.filtered(lambda tx: tx.provider_code == "paymos")
        if not paymos:
            return super().action_confirm()
        paymos._paymos_check_admin_access()
        for transaction in paymos.sudo():
            attempt = transaction._paymos_attempt()
            if not attempt or not attempt.invoice_id:
                raise UserError(self.env._("Paymos has no verified invoice to reconcile for this transaction."))
            try:
                payment.reconcile_attempt(attempt)
            except payment.CheckoutError:
                raise UserError(self.env._("Paymos could not verify this payment yet.")) from None
        other = self - paymos
        if other:
            return super(PaymentTransaction, other).action_confirm()

    def action_post_process(self):
        paymos = self.filtered(lambda tx: tx.provider_code == "paymos")
        if paymos:
            paymos._paymos_check_admin_access()
            if any(tx.state not in ("done", "cancel") for tx in paymos):
                raise UserError(self.env._("Paymos must verify this payment before post-processing."))
        return super().action_post_process()

    def _paymos_marker_matches(self, marker):
        self.ensure_one()
        attempt = marker.attempt_id
        return (
            marker.transaction_id == self
            and marker.company_id == self.company_id == attempt.company_id
            and marker.provider_id == self.provider_id == attempt.provider_id
            and marker.connection_id == attempt.connection_id
            and marker.project_id == attempt.project_id
            and marker.environment == attempt.environment
            and marker.generation == attempt.generation
            and marker.invoice_id == attempt.invoice_id
            and marker.amount == attempt.amount
            and marker.currency == attempt.currency == self.currency_id.name
            and (
                (
                    marker.kind == "invoice" and self == attempt.transaction_id
                    and marker.provider_reference == attempt.invoice_id
                    and marker.external_order_id == attempt.external_order_id
                    and not marker.refund_operation_id
                )
                or (
                    marker.kind == "refund" and self.operation == "refund"
                    and self.source_transaction_id == attempt.transaction_id
                    and marker.refund_operation_id > 0
                )
            )
        )

    def _process(self, payment_data):
        self.ensure_one()
        if self.provider_code != "paymos":
            return super()._process(payment_data)
        if not isinstance(payment_data, dict) or set(payment_data) != {"paymos_event_id"}:
            return
        marker_id = payment_data["paymos_event_id"]
        if type(marker_id) is not int or not 0 < marker_id <= 2147483647:
            return
        marker = self.env["paymos.event"].sudo().browse(marker_id).exists()
        if (
            not marker or marker.consumed or marker.target_state not in ("done", "cancel")
            or not self._paymos_marker_matches(marker)
        ):
            return
        if not self.try_lock_for_update(allow_referencing=True):
            raise payment.CheckoutError("checkout_busy")
        if marker.kind == "invoice":
            if not payment.validate_invoice_marker(marker):
                return
        elif not self._paymos_validate_refund_marker(marker):
            return

        verified = {
            "target_state": marker.target_state,
            "provider_reference": marker.provider_reference,
            "amount": marker.amount,
            "currency": marker.currency,
        }
        transaction = self.with_context(payment_safe_write=True)
        with transaction._paymos_allow_write(_PROJECTION_FIELDS):
            super(PaymentTransaction, transaction)._process(verified)
            if transaction.state != marker.target_state:
                raise payment.CheckoutError("native_projection_failed")

        if marker.kind == "invoice":
            attempt = marker.attempt_id
            disposition = "paid" if marker.target_state == "done" else marker.provider_status
            attempt.write({"disposition": disposition, "review_reason": False})
        else:
            refund.finish_projection(transaction, marker)
        marker.consumed = True

    def _apply_updates(self, payment_data):
        if self.provider_code != "paymos":
            return super()._apply_updates(payment_data)
        if not self._paymos_write_allowed(_PROJECTION_FIELDS):
            raise AccessError(self.env._("Paymos processing requires verified payment data."))
        self.provider_reference = payment_data["provider_reference"]
        if payment_data["target_state"] == "done":
            self._set_done()
        else:
            self._set_canceled()

    def _extract_amount_data(self, payment_data):
        if self.provider_code != "paymos":
            return super()._extract_amount_data(payment_data)
        return {
            "amount": float(money.parse_decimal(payment_data["amount"])),
            "currency_code": payment_data["currency"],
            "precision_digits": self.currency_id.decimal_places,
        }

    def _post_process(self):
        other = self.filtered(lambda tx: tx.provider_code != "paymos")
        if other:
            super(PaymentTransaction, other)._post_process()
        for tx in self - other:
            if not tx.is_post_processed and tx._paymos_can_post_process():
                with tx._paymos_allow_write(_POST_PROCESS_FIELDS):
                    super(PaymentTransaction, tx)._post_process()

    def _paymos_can_post_process(self):
        self.ensure_one()
        if self.operation == "refund":
            marker = self._paymos_refund_postprocess_marker()
            return (
                marker and marker.consumed and marker.target_state == self.state
                and self._paymos_marker_matches(marker)
            )
        attempt = self._paymos_attempt()
        if (
            not attempt or self.state not in ("pending", "done", "cancel")
            or not attempt.sale_order_id.try_lock_for_update(allow_referencing=True)
        ):
            return False
        try:
            payment.assert_native_attempt(attempt, check_order_amount=self.state != "cancel")
            if self.state == "done":
                payment.assert_accounting_ready(self.provider_id, self.currency_id)
        except payment.CheckoutError:
            return False
        if self.state == "pending":
            return True
        if self.state == "done" and (
            attempt.disposition != "paid" or attempt.review_reason == "external_full_refund"
            or self.invoice_ids - attempt.sale_order_id.invoice_ids
        ):
            return False
        if self.state == "cancel" and attempt.disposition not in ("expired", "cancelled"):
            return False
        marker = self.env["paymos.event"].sudo().search([
            ("attempt_id", "=", attempt.id), ("transaction_id", "=", self.id),
            ("kind", "=", "invoice"), ("target_state", "=", self.state), ("consumed", "=", True),
        ], limit=1)
        return bool(marker and self._paymos_marker_matches(marker))

    def _paymos_process_withdrawal(self, event):
        return refund.process_withdrawal(self, event)

    def _paymos_validate_refund_marker(self, marker):
        return refund.validate_marker(self, marker)

    def _paymos_refund_postprocess_marker(self):
        return refund.postprocess_marker(self)
