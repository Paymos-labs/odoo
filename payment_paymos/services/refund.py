import hashlib
import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal, localcontext

from odoo import fields

from . import money, payment, sdk


REFUND_SNAPSHOT_LIMIT = 50
CRON_BATCH = 8
MAX_CHECKS = 12
_FINAL = {"completed", "failed", "cancelled"}
_IN_FLIGHT = {"created", "pending_review", "signed", "cancelling"}
_CLOSED_OPERATIONS = ("completed", "failed", "cancelled")
_MAX_EXTERNAL_ORDER_ID_UTF16_UNITS = 200


class RefundSnapshotError(ValueError):
    pass


def _utf16_code_units(value):
    # .NET string limits count UTF-16 code units, including two for astral characters.
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return None


def _requires_contract(credentials):
    if credentials.refund_contract_version != 1 or "invoice_refunds_v1" not in credentials.capabilities:
        raise RefundSnapshotError("refund_contract_unavailable")


def _refund_item(item, attempt, invoice_id):
    if not isinstance(item, dict):
        raise RefundSnapshotError("refund_snapshot_invalid")
    withdrawal_id = item.get("withdrawal_id")
    external_id = item.get("external_order_id")
    external_units = _utf16_code_units(external_id) if isinstance(external_id, str) else None
    status = item.get("status")
    if (
        not isinstance(withdrawal_id, str)
        or not re.fullmatch(r"wdr_[A-Za-z0-9]{1,46}", withdrawal_id)
        or external_units is None
        or not external_id.strip()
        or external_units > _MAX_EXTERNAL_ORDER_ID_UTF16_UNITS
        or item.get("type") != "refund"
        or item.get("invoice_id") != invoice_id
        or item.get("project_id") != attempt.project_id
        or item.get("is_test") is not (attempt.environment == "sandbox")
        or item.get("mode") not in ("full", "overpayment")
        or not isinstance(status, str)
        or status not in _FINAL | _IN_FLIGHT
        or item.get("is_final") is not (status in _FINAL)
    ):
        raise RefundSnapshotError("refund_snapshot_invalid")
    return item


def _refund_history(client, attempt, invoice_id):
    page = client.refunds.list(invoice_id=invoice_id, limit=REFUND_SNAPSHOT_LIMIT)
    if not isinstance(page, dict) or "next_cursor" not in page or page["next_cursor"] is not None:
        raise RefundSnapshotError("refund_snapshot_invalid")
    items = page.get("items")
    if not isinstance(items, list) or len(items) > REFUND_SNAPSHOT_LIMIT:
        raise RefundSnapshotError("refund_snapshot_invalid")
    seen = set()
    for item in items:
        _refund_item(item, attempt, invoice_id)
        if item["withdrawal_id"] in seen:
            raise RefundSnapshotError("refund_snapshot_invalid")
        seen.add(item["withdrawal_id"])
    return items


def blocking_full_refund(client, credentials, attempt, invoice_id):
    """Check one complete, bounded Merchant API refund snapshot for the exact invoice."""
    _requires_contract(credentials)
    return any(
        item["mode"] == "full" and item["status"] not in ("failed", "cancelled")
        for item in _refund_history(client, attempt, invoice_id)
    )


def native_operation_matches(operation):
    attempt = operation.attempt_id
    parent = operation.parent_transaction_id
    child = operation.child_transaction_id
    if (
        not attempt
        or parent != attempt.transaction_id
        or parent.provider_code != "paymos"
        or parent.operation != "online_redirect"
        or parent.company_id != operation.company_id
        or attempt.company_id != operation.company_id
        or parent.state != "done"
        or attempt.disposition != "paid"
    ):
        return False
    if operation.mode == "overpayment":
        return not child
    return bool(
        child and child.operation == "refund" and child.source_transaction_id == parent
        and child.provider_id == parent.provider_id and child.company_id == operation.company_id
        and child.partner_id == parent.partner_id and child.payment_method_id == parent.payment_method_id
        and child.currency_id == parent.currency_id and child.is_live == parent.is_live
        and child.amount == -parent.amount and not child.sale_order_ids
    )


def _lock_refund(attempt, child=None):
    payment._attempt_lock(attempt)
    if not attempt.transaction_id.try_lock_for_update(allow_referencing=True):
        raise payment.CheckoutError("checkout_busy")
    if child and not child.try_lock_for_update(allow_referencing=True):
        raise payment.CheckoutError("checkout_busy")


def _paid_marker(attempt):
    marker = attempt.env["paymos.event"].sudo().search([
        ("attempt_id", "=", attempt.id), ("transaction_id", "=", attempt.transaction_id.id),
        ("kind", "=", "invoice"), ("target_state", "=", "done"), ("consumed", "=", True),
    ], limit=1)
    if not marker or not attempt.transaction_id._paymos_marker_matches(marker):
        raise payment.CheckoutError("refund_original_unverified")
    return marker


def _assert_parent_accounted(parent):
    if not parent.is_post_processed or not parent.payment_id or parent.payment_id.move_id.state != "posted":
        raise payment.CheckoutError("refund_original_unverified")


def _paid_below_expected(marker):
    # A payment accepted within the underpayment tolerance is final "paid" with less than
    # expected; the server refunds no such invoice. Markers without amounts are not judged.
    try:
        return (
            money.parse_decimal(getattr(marker, "token_paid", False))
            < money.parse_decimal(getattr(marker, "token_expected", False))
        )
    except money.AmountError:
        return False


def request_operation(parent, mode, amount=None):
    """Create a durable local intent. This path never sends a financial API request."""
    attempt = parent._paymos_attempt()
    if not attempt or parent.state != "done" or attempt.disposition != "paid" or not attempt.invoice_id:
        raise payment.CheckoutError("refund_original_unverified")
    _lock_refund(attempt)
    payment.assert_native_attempt(attempt)
    marker = _paid_marker(attempt)
    if mode == "full":
        try:
            requested = parent.amount if amount is None else amount
            if money.native_amount(requested, parent.currency_id) != attempt.amount:
                raise payment.CheckoutError("refund_full_only")
        except money.AmountError:
            raise payment.CheckoutError("refund_full_only") from None
    elif mode != "overpayment" or marker.provider_status != "paid_over":
        raise payment.CheckoutError("refund_no_overpayment")

    operations = parent.env["paymos.refund"].sudo()
    operation = operations.search([("attempt_id", "=", attempt.id), ("mode", "=", mode)], limit=1)
    if operation:
        if not native_operation_matches(operation) or operation.state in ("failed", "cancelled", "review"):
            raise payment.CheckoutError("refund_needs_review")
        return operation

    _assert_parent_accounted(parent)
    if attempt.review_reason in (*payment._HARD_REVIEW, "external_full_refund"):
        raise payment.CheckoutError("refund_needs_review")
    credentials = payment.credentials_for_attempt(attempt)
    _requires_contract(credentials)
    if mode == "full":
        if _paid_below_expected(marker):
            raise payment.CheckoutError("refund_not_eligible")
        payment.assert_accounting_ready(parent.provider_id, parent.currency_id)
        with parent._paymos_allow_refund_create():
            child = parent._create_child_transaction(parent.amount, is_refund=True)
        child._paymos_mark_checkout_pending()
    else:
        child = parent.env["payment.transaction"]
    operation = operations.create({
        "company_id": attempt.company_id.id, "parent_transaction_id": parent.id,
        "child_transaction_id": child.id, "attempt_id": attempt.id, "mode": mode,
        "external_order_id": f"odoo_refund_{secrets.token_hex(16)}",
    })
    parent.env.ref("payment_paymos.paymos_operation_cron")._trigger()
    return operation


def _api_snapshot(attempt):
    credentials = payment.credentials_for_attempt(attempt)
    _requires_contract(credentials)
    client = payment._sdk_call(sdk.make_client, credentials)
    invoice = payment._sdk_call(client.invoices.get, attempt.invoice_id)
    payment.check_invoice_binding(invoice, attempt, expected_invoice_id=attempt.invoice_id)
    if (
        invoice.get("status") not in ("paid", "paid_over")
        or invoice.get("is_final") is not True
        or invoice.get("paid_late_at") is not None
    ):
        raise payment.CheckoutError("refund_original_unverified")
    history = payment._sdk_call(_refund_history, client, attempt, attempt.invoice_id)
    return credentials, client, invoice, history


def _token_amount(item, token, network):
    if item.get("currency") != token or item.get("network") != network:
        raise RefundSnapshotError("refund_asset_mismatch")
    try:
        return money.parse_decimal(item.get("amount"))
    except money.AmountError:
        raise RefundSnapshotError("refund_amount_mismatch") from None


def _amount_for_mode(operation, invoice, history, withdrawal_id=None):
    token, network, expected, paid = payment._coverage(invoice)
    with localcontext() as context:
        context.prec = 64
        prior = sum((
            _token_amount(item, token, network) for item in history
            if item["withdrawal_id"] != withdrawal_id and item["status"] == "completed"
        ), Decimal(0))
        amount = paid - prior if operation.mode == "full" else paid - expected - prior
    if amount <= 0:
        raise RefundSnapshotError("refund_amount_mismatch")
    return token, network, amount


def _check_observation(operation, invoice, history, snapshot):
    attempt = operation.attempt_id
    _refund_item(snapshot, attempt, attempt.invoice_id)
    withdrawal_id = snapshot["withdrawal_id"]
    if (
        snapshot["mode"] != operation.mode
        or snapshot["external_order_id"] != operation.external_order_id
        or (operation.withdrawal_id and operation.withdrawal_id != withdrawal_id)
        or any(
            item["withdrawal_id"] != withdrawal_id
            or item["external_order_id"] != operation.external_order_id or item["mode"] != operation.mode
            for item in history
            if item["withdrawal_id"] == withdrawal_id or item["external_order_id"] == operation.external_order_id
        )
    ):
        raise RefundSnapshotError("refund_binding_mismatch")
    destination = snapshot.get("destination_address")
    if (
        not isinstance(destination, str) or not 0 < len(destination) <= 1024
        or not destination.isascii() or not all(33 <= ord(char) <= 126 for char in destination)
        or snapshot.get("first_sender") != destination
    ):
        raise RefundSnapshotError("refund_destination_mismatch")
    token, network, amount = _amount_for_mode(operation, invoice, history, withdrawal_id)
    if _token_amount(snapshot, token, network) != amount:
        raise RefundSnapshotError("refund_amount_mismatch")
    if operation.token_amount and (
        money.parse_decimal(operation.token_amount) != amount
        or operation.token_currency != token or operation.token_network != network
    ):
        raise RefundSnapshotError("refund_amount_mismatch")
    return token, network, amount


def _same_observation(signed, fresh):
    # Destination and first_sender must match inside each representation. The signed
    # snapshot and GET may serialize an address differently on non-EVM networks.
    return all(signed.get(key) == fresh.get(key) for key in (
        "withdrawal_id", "invoice_id", "project_id", "is_test", "mode", "external_order_id",
        "status", "is_final", "currency", "network",
    ))


def _decimal_string(amount):
    value = format(amount, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _remember(operation, snapshot, evidence):
    token, network, amount = evidence
    operation.write({
        "withdrawal_id": snapshot["withdrawal_id"],
        "token_amount": _decimal_string(amount), "token_currency": token, "token_network": network,
    })


def _record_delivery(operation, snapshot, evidence, target, event=None):
    attempt = operation.attempt_id
    status = snapshot["status"]
    token, network, amount = evidence
    if event is None:
        digest = hashlib.sha256(json.dumps([
            operation.id, snapshot["withdrawal_id"], status, token, network, _decimal_string(amount),
        ], separators=(",", ":")).encode("utf-8")).hexdigest()
        event_id = f"refund_reconcile_{digest}"
    else:
        event_id, digest = event["event_id"], event["raw_sha256"]
    return payment.record_projection(
        attempt, event_id=event_id, raw_sha256=digest, kind="refund", refund_operation_id=operation.id,
        transaction=operation.child_transaction_id or operation.parent_transaction_id,
        target_state=target, provider_status=status,
        provider_reference=operation.withdrawal_id, external_order_id=operation.external_order_id,
        coverage=(token, network, amount, amount), retry=event is None,
    )


def _terminal_observation(operation, snapshot, evidence, event=None):
    status = snapshot["status"]
    child = operation.child_transaction_id
    closed = operation.state in _CLOSED_OPERATIONS
    target = ("done" if status == "completed" else "cancel") if child and not closed else "observe"
    with operation.env.cr.savepoint():
        _remember(operation, snapshot, evidence)
        marker = _record_delivery(operation, snapshot, evidence, target, event)
        if closed:
            if status == "completed" and operation.review_reason == "refund_completion_rewound":
                operation.review_reason = False
        elif child:
            if not marker.consumed:
                operation._paymos_defer(False, state="queued")
        else:
            if status == "completed":
                order = operation.attempt_id.sale_order_id
                order.message_post(body=order.env._("Paymos returned the overpayment."))
            operation.write({
                "state": status,
                "review_reason": False if status == "completed" else f"refund_{status}",
            })


def _check_local_operation(operation):
    if not native_operation_matches(operation):
        raise payment.CheckoutError("refund_native_binding_changed")
    attempt = operation.attempt_id
    if attempt.review_reason in payment._HARD_REVIEW:
        raise payment.CheckoutError("refund_needs_review")
    payment.assert_native_attempt(attempt)
    _paid_marker(attempt)
    _assert_parent_accounted(attempt.transaction_id)
    if operation.child_transaction_id:
        payment.assert_accounting_ready(attempt.provider_id, attempt.transaction_id.currency_id)


def _create_refund(client, operation):
    """POST the intent. 400, 404, 409, 410 and 422 refuse this request for good: no refund exists.

    401 and 403 point at a key being rotated or revoked; like 408, 429 and 5xx they stay on the
    unknown-result retries.
    """
    try:
        return client.refunds.create(
            invoice_id=operation.attempt_id.invoice_id, mode=operation.mode,
            external_order_id=operation.external_order_id,
        )
    except sdk._vendor().ApiError as error:
        if error.status in (400, 404, 409, 410, 422):
            raise RefundSnapshotError(error.code or "refund_request_rejected") from None
        raise


def run_operation(operation):
    """Process one committed intent. A retry can only use its original API tuple."""
    if operation.state in (*_CLOSED_OPERATIONS, "review"):
        return
    try:
        _lock_refund(operation.attempt_id, operation.child_transaction_id)
    except payment.CheckoutError:
        return
    try:
        _check_local_operation(operation)
        _credentials, client, invoice, history = _api_snapshot(operation.attempt_id)
        matching = [item for item in history if item["external_order_id"] == operation.external_order_id]
        if len(matching) > 1:
            raise RefundSnapshotError("refund_binding_mismatch")
        created = False
        if operation.withdrawal_id:
            snapshot = payment._sdk_call(client.refunds.get, operation.withdrawal_id)
        elif matching:
            snapshot = payment._sdk_call(client.refunds.get, matching[0]["withdrawal_id"])
        else:
            if any(
                item["status"] in _IN_FLIGHT
                or (item["mode"] == "full" and item["status"] == "completed")
                for item in history
            ):
                raise RefundSnapshotError("refund_external_operation")
            if operation.mode == "overpayment" and invoice["status"] != "paid_over":
                raise RefundSnapshotError("refund_no_overpayment")
            _amount_for_mode(operation, invoice, history)
            try:
                snapshot = payment._sdk_call(_create_refund, client, operation)
                created = True
            except payment.CheckoutError:
                operation._paymos_defer("refund_result_unknown", state="unknown")
                return

        evidence = _check_observation(operation, invoice, history, snapshot)
        if snapshot["status"] in _FINAL:
            if created:
                snapshot = payment._sdk_call(client.refunds.get, snapshot["withdrawal_id"])
                evidence = _check_observation(operation, invoice, history, snapshot)
            if snapshot["status"] in _FINAL:
                _terminal_observation(operation, snapshot, evidence)
                return
        _remember(operation, snapshot, evidence)
        operation._paymos_defer(False, state="pending")
    except RefundSnapshotError as error:
        if str(error) == "refund_snapshot_invalid":
            operation._paymos_defer(str(error))
        else:
            operation.write({"state": "review", "review_reason": str(error)})
    except payment.CheckoutError as error:
        operation._paymos_defer(error.code)


def process_withdrawal(parent, event):
    attempt = parent.env["paymos.attempt"].sudo().browse(event["attempt_id"]).exists()
    if not attempt or attempt.transaction_id != parent:
        return "ignored"
    data = event["data"]
    try:
        _refund_item(data, attempt, attempt.invoice_id)
        operation = parent.env["paymos.refund"].sudo().search([
            ("attempt_id", "=", attempt.id), ("mode", "=", data["mode"]),
            ("external_order_id", "=", data["external_order_id"]),
        ], limit=1)
        with parent.env.cr.savepoint():
            if not operation:
                credentials = payment.credentials_for_attempt(attempt)
                _requires_contract(credentials)
                client = payment._sdk_call(sdk.make_client, credentials)
                current = payment._sdk_call(client.refunds.get, data["withdrawal_id"])
                _refund_item(current, attempt, attempt.invoice_id)
                if not _same_observation(data, current):
                    return "deferred"
                if data["mode"] == "full" and data["status"] not in ("failed", "cancelled"):
                    payment._mark_review(attempt, "external_full_refund")
                return "ignored"

            _lock_refund(attempt, operation.child_transaction_id)
            _check_local_operation(operation)
            _credentials, client, invoice, history = _api_snapshot(attempt)
            current = payment._sdk_call(client.refunds.get, data["withdrawal_id"])
            _refund_item(current, attempt, attempt.invoice_id)
            if not _same_observation(data, current):
                return "deferred"
            evidence = _check_observation(operation, invoice, history, current)
            if _check_observation(operation, invoice, history, data) != evidence:
                return "deferred"
            if event["event_type"] == "withdrawal.completion_rewound" and operation.state == "completed":
                if current["status"] not in _IN_FLIGHT:
                    return "deferred"
                _record_delivery(operation, current, evidence, "observe", event)
                operation.review_reason = "refund_completion_rewound"
                return "processed"
            if operation.state in _CLOSED_OPERATIONS and current["status"] != operation.state:
                return "ignored"
            if current["status"] in _FINAL:
                _terminal_observation(operation, current, evidence, event)
            else:
                _remember(operation, current, evidence)
                _record_delivery(operation, current, evidence, "observe", event)
                operation._paymos_defer(False, state="pending")
    except (RefundSnapshotError, payment.CheckoutError):
        return "deferred"
    return "processed"


def validate_marker(child, marker):
    operation = child.env["paymos.refund"].sudo().browse(marker.refund_operation_id).exists()
    if (
        not operation or operation.mode != "full" or operation.child_transaction_id != child
        or operation.attempt_id != marker.attempt_id or operation.external_order_id != marker.external_order_id
        or operation.withdrawal_id != marker.provider_reference or operation.state in _CLOSED_OPERATIONS
    ):
        return False
    _lock_refund(operation.attempt_id, child)
    _check_local_operation(operation)
    try:
        _credentials, client, invoice, history = _api_snapshot(operation.attempt_id)
        current = payment._sdk_call(client.refunds.get, operation.withdrawal_id)
        token, network, amount = _check_observation(operation, invoice, history, current)
        if (
            current["status"] != marker.provider_status or current["status"] not in _FINAL
            or (marker.target_state == "done") != (current["status"] == "completed")
            or token != marker.token_currency or network != marker.token_network
            or _decimal_string(amount) != operation.token_amount
            or money.parse_decimal(marker.token_paid) != amount
        ):
            raise RefundSnapshotError("refund_snapshot_changed")
    except RefundSnapshotError as error:
        raise payment.CheckoutError(str(error)) from None
    return True


def finish_projection(child, marker):
    operation = child.env["paymos.refund"].sudo().browse(marker.refund_operation_id).exists()
    if not operation or operation.child_transaction_id != child or not native_operation_matches(operation):
        raise payment.CheckoutError("refund_native_binding_changed")
    status = marker.provider_status
    operation.write({
        "state": status, "review_reason": False if status == "completed" else f"refund_{status}",
    })


def postprocess_marker(child):
    events = child.env["paymos.event"].sudo()
    operation = child.env["paymos.refund"].sudo().search([("child_transaction_id", "=", child.id)], limit=1)
    if (
        not operation or operation.state not in _CLOSED_OPERATIONS
        or operation.review_reason == "refund_completion_rewound"
        or child.state != ("done" if operation.state == "completed" else "cancel")
    ):
        return events
    try:
        _lock_refund(operation.attempt_id, child)
        _check_local_operation(operation)
    except payment.CheckoutError:
        return events
    return events.search([
        ("refund_operation_id", "=", operation.id), ("transaction_id", "=", child.id),
        ("kind", "=", "refund"), ("target_state", "=", child.state), ("consumed", "=", True),
        ("provider_status", "=", operation.state), ("provider_reference", "=", operation.withdrawal_id),
    ], limit=1)


def recover_invoice(attempt):
    """Recover a missed callback or lost create response without ever creating an invoice."""
    try:
        _lock_refund(attempt)
    except payment.CheckoutError:
        return
    try:
        if not attempt.invoice_id:
            credentials = payment.credentials_for_attempt(attempt)
            client = payment._sdk_call(sdk.make_client, credentials)
            page = payment._sdk_call(
                client.invoices.list, external_order_id=attempt.external_order_id, limit=2,
            )
            if (
                not isinstance(page, dict) or "next_cursor" not in page or page["next_cursor"] is not None
                or not isinstance(page.get("items"), list) or len(page["items"]) > 1
            ):
                raise payment.CheckoutError("invoice_recovery_ambiguous")
            if page["items"]:
                item = page["items"][0]
                if not isinstance(item, dict) or not payment.valid_invoice_id(item.get("invoice_id")):
                    raise payment.CheckoutError("invoice_recovery_ambiguous")
                invoice = payment._sdk_call(client.invoices.get, item["invoice_id"])
                invoice_id = payment.check_invoice_binding(invoice, attempt, expected_invoice_id=item["invoice_id"])
                attempt.invoice_id = invoice_id
        if attempt.invoice_id:
            payment.reconcile_attempt(attempt)
    except payment.CheckoutError:
        pass
    count = attempt.paymos_reconcile_count + 1
    attempt.write({
        "paymos_reconcile_count": count,
        "paymos_reconcile_after": fields.Datetime.now() + timedelta(minutes=min(60, 2 ** min(count - 1, 6))),
    })
