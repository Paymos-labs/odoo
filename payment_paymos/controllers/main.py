import re

from odoo import http
from odoo.addons.payment import utils as payment_utils
from odoo.http import request

from ..services import payment


class PaymosController(http.Controller):
    @http.route(payment.START_URL, type="http", auth="public", methods=["POST"], website=True, csrf=True, sitemap=False)
    def paymos_start(self, **_post):
        form = request.httprequest.form
        names = {"tx_id", "attempt_id", "access_token", "csrf_token"}
        if request.httprequest.args or set(form) != names or any(len(form.getlist(name)) != 1 for name in names):
            return self._paymos_response(400, request.env._("Invalid Paymos checkout request."))
        tx_id = form["tx_id"]
        attempt_id = form["attempt_id"]
        token = form["access_token"]
        if not all(re.fullmatch(r"[1-9][0-9]{0,9}", value) for value in (tx_id, attempt_id)):
            return self._paymos_response(400, request.env._("Invalid Paymos checkout request."))

        tx_id, attempt_id = int(tx_id), int(attempt_id)
        if (
            not request.session.sid
            or not re.fullmatch(r"[a-f0-9]{64}", token)
            or not payment_utils.check_access_token(
                token, *payment.start_token_values(tx_id, attempt_id, request.session.sid),
            )
        ):
            return self._paymos_response(403, request.env._("Invalid Paymos checkout authorization."))

        attempt = request.env["paymos.attempt"].sudo().browse(attempt_id).exists()
        if (
            not attempt
            or attempt.transaction_id.id != tx_id
            or attempt.sale_order_id.website_id != request.env.website
        ):
            return self._paymos_response(403, request.env._("Invalid Paymos checkout authorization."))

        try:
            url = payment.start_attempt(attempt)
        except payment.CheckoutError as error:
            if error.code in ("checkout_busy", "connection_unavailable", "accounting_unavailable"):
                return self._paymos_response(
                    503, request.env._("Paymos payment is pending. Try opening the checkout again."), retry=True,
                )
            return self._paymos_response(
                409, request.env._("Paymos cannot open payment for this order. Contact the store if it was charged."),
            )

        response = request.redirect(url, code=303, local=False)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @http.route(
        payment.WEBHOOK_URL, type="http", auth="public", methods=["POST"], csrf=False,
        sitemap=False, max_content_length=payment.MAX_WEBHOOK_BYTES,
    )
    def paymos_webhook(self):
        incoming = request.httprequest
        if incoming.mimetype != "application/json":
            return self._paymos_response(415, "")
        length = incoming.content_length
        if length is None:
            return self._paymos_response(411, "")
        if length > payment.MAX_WEBHOOK_BYTES:
            return self._paymos_response(413, "")
        raw = incoming.get_data(cache=False, as_text=False, parse_form_data=False)
        if len(raw) != length:
            return self._paymos_response(400, "")
        signature = incoming.headers.get("X-Webhook-Signature", "")
        try:
            payment.handle_webhook(request.env, raw, signature)
        except payment.WebhookError as error:
            return self._paymos_response(error.status, "")
        return self._paymos_response(200, "")

    @staticmethod
    def _paymos_response(status, message, *, retry=False):
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
        ]
        if retry:
            headers.append(("Retry-After", "5"))
        return request.make_response(message, headers=headers, status=status)
