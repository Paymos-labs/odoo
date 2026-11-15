import hashlib
import hmac
import json
import re
import secrets

from odoo.tools import SQL

from . import connect, money, refund, sdk


START_URL = "/payment/paymos/start"
_REPLACEABLE = ("cancelled", "expired")
_RESUMABLE = ("prepared", "open")
_OPEN_INVOICES = ("awaiting_client", "awaiting_payment", "confirming", "underpaid_waiting")


class CheckoutError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def start_token_values(tx_id, attempt_id, session_id):
    return ("payment_paymos/start/v1", tx_id, attempt_id, session_id)


def valid_invoice_id(value):
    return isinstance(value, str) and re.fullmatch(r"inv_[A-Za-z0-9_-]{1,124}", value) is not None


def credentials_for_attempt(attempt):
    connection = attempt.connection_id
    if (
        not connection
        or connection.company_id != attempt.company_id
        or connection.provider_id != attempt.provider_id
        or connection.environment != attempt.environment
        or connection.project_id != attempt.project_id
    ):
        raise CheckoutError("binding_mismatch")
    try:
        return connect.open_credentials(
            connection.credentials_envelope,
            provider_id=connection.provider_id.id,
            company_id=connection.company_id.id,
            generation=connection.generation,
            source_url=connection.source_url,
            environment=connection.environment,
            project_id=connection.project_id,
        )
    except connect.ConnectError:
        raise CheckoutError("connection_unavailable") from None


def check_invoice_binding(invoice, attempt, *, expected_invoice_id=None):
    if not isinstance(invoice, dict):
        raise CheckoutError("invoice_binding_mismatch")
    invoice_id = invoice.get("invoice_id")
    order = invoice.get("order")
    if (
        not valid_invoice_id(invoice_id)
        or (expected_invoice_id is not None and invoice_id != expected_invoice_id)
        or (attempt.invoice_id and invoice_id != attempt.invoice_id)
        or invoice.get("project_id") != attempt.project_id
        or invoice.get("is_test") is not (attempt.environment == "sandbox")
        or not isinstance(order, dict)
        or order.get("external_id") != attempt.external_order_id
        or order.get("currency") != attempt.currency
    ):
        raise CheckoutError("invoice_binding_mismatch")
    try:
        if money.parse_decimal(order.get("amount")) != money.parse_decimal(attempt.amount):
            raise CheckoutError("invoice_binding_mismatch")
    except money.AmountError:
        raise CheckoutError("invoice_binding_mismatch") from None
    return invoice_id


def _lock_checkout(order, provider):
    if not order.try_lock_for_update(allow_referencing=True):
        raise CheckoutError("checkout_busy")
    # Provider settings updates conflict with this lock; unrelated checkouts can share it.
    if not provider.env.execute_query(SQL(
        "SELECT id FROM %s WHERE id = %s FOR SHARE SKIP LOCKED",
        SQL.identifier(provider._table), provider.id,
    )):
        raise CheckoutError("checkout_busy")


def _checkout_order(transaction):
    order = transaction.sale_order_ids
    if (
        len(order) != 1
        or transaction.operation != "online_redirect"
        or transaction.source_transaction_id
        or transaction.tokenize
        or transaction.token_id
        or transaction.provider_code != "paymos"
    ):
        raise CheckoutError("order_not_payable")
    return order


def _checkout_snapshot(transaction, order, provider):
    if (
        order.state not in ("draft", "sent")
        or not order.website_id
        or order.website_id.company_id != order.company_id
        or (provider.website_id and provider.website_id != order.website_id)
        or transaction.provider_id != provider
        or transaction.company_id != order.company_id
        or provider.company_id != order.company_id
        or transaction.partner_id != order.partner_invoice_id
        or transaction.currency_id != order.currency_id
        or transaction.payment_method_id.provider_id != provider
        or transaction.payment_method_id.code != "paymos"
        or not transaction.payment_method_id.active
        or transaction.state not in ("draft", "pending")
    ):
        raise CheckoutError("order_not_payable")

    connection = provider._paymos_checkout_connection()
    if not connection or transaction.is_live is not (connection.environment == "live"):
        raise CheckoutError("connection_unavailable")
    currency = order.currency_id
    try:
        amount = money.native_amount(transaction.amount, currency)
        total = money.native_amount(order.amount_total, currency)
        paid = money.native_amount(order.amount_paid, currency, allow_zero=True)
        if money.parse_decimal(amount) != money.remaining_amount(total, paid):
            raise CheckoutError("order_changed")
    except money.AmountError:
        raise CheckoutError("invalid_amount") from None
    return connection, amount, currency.name


def _assert_attempt_snapshot(attempt, order, provider, connection, amount, currency):
    if (
        attempt.disposition not in _RESUMABLE
        or attempt.sale_order_id != order
        or attempt.provider_id != provider
        or attempt.company_id != order.company_id
        or attempt.connection_id != connection
        or attempt.project_id != connection.project_id
        or attempt.environment != connection.environment
        or attempt.amount != amount
        or attempt.currency != currency
    ):
        raise CheckoutError("order_changed")


def _current_attempt(attempt_model, order):
    return attempt_model.search([
        ("sale_order_id", "=", order.id), ("disposition", "not in", _REPLACEABLE),
    ], limit=1)


def prepare_attempt(transaction):
    """Persist the checkout intent in the native render request, without making an API call."""
    order = _checkout_order(transaction)
    provider = transaction.provider_id
    _lock_checkout(order, provider)
    connection, amount, currency = _checkout_snapshot(transaction, order, provider)

    attempts = transaction.env["paymos.attempt"].sudo()
    current = _current_attempt(attempts, order)
    if current:
        _assert_attempt_snapshot(current, order, provider, connection, amount, currency)
        original = current.transaction_id
        if _checkout_order(original) != order:
            raise CheckoutError("order_changed")
        _checkout_snapshot(original, order, provider)
        return current

    if attempts.search_count([("transaction_id", "=", transaction.id)], limit=1):
        # A new generation requires a new native transaction.
        raise CheckoutError("order_changed")
    previous = attempts.search([("sale_order_id", "=", order.id)], order="generation desc", limit=1)
    return attempts.create({
        "company_id": order.company_id.id,
        "provider_id": provider.id,
        "connection_id": connection.id,
        "sale_order_id": order.id,
        "transaction_id": transaction.id,
        "project_id": connection.project_id,
        "environment": connection.environment,
        "generation": previous.generation + 1 if previous else 1,
        "external_order_id": f"odoo_{secrets.token_hex(16)}",
        "amount": amount,
        "currency": currency,
    })


def _request_invoice(attempt):
    credentials = credentials_for_attempt(attempt)
    client = _sdk_call(sdk.make_client, credentials)
    if attempt.invoice_id:
        return _sdk_call(client.invoices.get, attempt.invoice_id)
    return _sdk_call(
        client.invoices.create, project_id=attempt.project_id, external_order_id=attempt.external_order_id,
        amount=attempt.amount, currency=attempt.currency,
    )


def _checkout_url(invoice, invoice_id):
    try:
        url = sdk.validate_checkout_url(invoice.get("payment_url"))
        parts = sdk.parse_https_url(url)
        if parts.path != f"/invoice/{invoice_id}" or "?" in url:
            raise ValueError("invalid_checkout_url")
    except ValueError:
        raise CheckoutError("invalid_checkout_url") from None
    return url


def assert_accounting_ready(provider, currency):
    """Require the native payment currency and exact company bank method line."""
    if not currency.active:
        raise CheckoutError("accounting_unavailable")
    provider = provider.sudo()
    journal = provider.journal_id
    if not journal or journal.company_id != provider.company_id or journal.type != "bank":
        raise CheckoutError("accounting_unavailable")
    lines = journal.inbound_payment_method_line_ids.filtered(
        lambda line: line.payment_provider_id == provider
    )
    if (
        len(lines) != 1
        or lines.payment_method_id.code != "paymos"
        or lines.payment_method_id.payment_type != "inbound"
        or not lines.payment_account_id
    ):
        raise CheckoutError("accounting_unavailable")


def start_attempt(attempt):
    """Open the durable attempt from the authenticated, CSRF-protected second request."""
    transaction = attempt.transaction_id
    order = _checkout_order(transaction)
    provider = transaction.provider_id
    _lock_checkout(order, provider)
    if not transaction.try_lock_for_update(allow_referencing=True):
        raise CheckoutError("checkout_busy")
    connection, amount, currency = _checkout_snapshot(transaction, order, provider)
    if _current_attempt(attempt.env["paymos.attempt"], order) != attempt:
        raise CheckoutError("order_changed")
    _assert_attempt_snapshot(attempt, order, provider, connection, amount, currency)

    assert_accounting_ready(provider, transaction.currency_id)
    transaction._paymos_monitor_checkout()
    transaction._paymos_mark_checkout_pending()
    invoice = _request_invoice(attempt)
    invoice_id = check_invoice_binding(invoice, attempt)
    try:
        if (
            invoice.get("status") not in _OPEN_INVOICES
            or invoice.get("is_final") is not False
            or invoice.get("paid_late_at") is not None
        ):
            raise CheckoutError("invoice_not_open")
        url = _checkout_url(invoice, invoice_id)
    except CheckoutError as error:
        attempt.write({"invoice_id": invoice_id, "disposition": "review", "review_reason": error.code})
        raise CheckoutError("invoice_review") from None

    attempt.write({"invoice_id": invoice_id, "disposition": "open", "review_reason": False})
    return url


MAX_WEBHOOK_BYTES = 65536
WEBHOOK_URL = "/payment/paymos/webhook"
_PAID_STATUSES = ("paid", "paid_over")
_CLOSED_STATUSES = ("cancelled", "expired")
_INVOICE_STATUSES = (*_OPEN_INVOICES, *_PAID_STATUSES, *_CLOSED_STATUSES, "underpaid")
_INVOICE_EVENTS = tuple(f"invoice.{status}" for status in _INVOICE_STATUSES) + ("invoice.paid_late",)
_WITHDRAWAL_EVENTS = (
    "withdrawal.created", "withdrawal.processing", "withdrawal.completed",
    "withdrawal.failed", "withdrawal.cancelled", "withdrawal.completion_rewound",
)
_HARD_REVIEW = ("paid_late", "previous_attempt_paid", "event_payload_conflict")


class WebhookError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Paymos webhook status {status}")


def _sdk_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except refund.RefundSnapshotError:
        raise
    except Exception as error:
        if isinstance(error, (OSError, sdk.PaymosSdkError)) or isinstance(error, sdk._vendor().PaymosError):
            raise CheckoutError("connection_unavailable") from None
        raise


def _read_webhook(raw):
    try:
        event = json.loads(
            raw.decode("utf-8"), object_pairs_hook=connect._unique_object,
            parse_constant=connect._invalid_json_constant,
        )
    except (ValueError, RecursionError):
        raise WebhookError(400) from None
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("event_id"), str)
        or not re.fullmatch(r"evt_[A-Za-z0-9_-]{1,124}", event["event_id"])
        or event.get("event_type") not in (*_INVOICE_EVENTS, *_WITHDRAWAL_EVENTS)
        or type(event.get("version")) is not int
        or event["version"] != 1
        or type(event.get("occurred_at")) is not int
        or event["occurred_at"] <= 0
        or not isinstance(event.get("data"), dict)
    ):
        raise WebhookError(400)
    return event


def _webhook_attempt(env, data, *, withdrawal):
    invoice_id = data.get("invoice_id")
    project_id = data.get("project_id")
    if (
        not valid_invoice_id(invoice_id)
        or not isinstance(project_id, str)
        or not re.fullmatch(r"prj_[A-Za-z0-9_-]{1,128}", project_id)
        or type(data.get("is_test")) is not bool
    ):
        raise WebhookError(400)
    scope = [
        ("project_id", "=", project_id),
        ("environment", "=", "sandbox" if data["is_test"] else "live"),
    ]
    attempts = env["paymos.attempt"].sudo()
    attempt = attempts.search(scope + [("invoice_id", "=", invoice_id)], limit=1)
    if not attempt and not withdrawal:
        order = data.get("order")
        external_id = order.get("external_id") if isinstance(order, dict) else None
        if isinstance(external_id, str) and re.fullmatch(r"odoo_[a-f0-9]{32}", external_id):
            attempt = attempts.search(scope + [
                ("invoice_id", "=", False), ("external_order_id", "=", external_id),
            ], limit=1)
    if not attempt:
        raise WebhookError(401)
    return attempt


def _attempt_lock(attempt):
    if not attempt.sale_order_id.try_lock_for_update(allow_referencing=True):
        raise CheckoutError("checkout_busy")


def assert_native_attempt(attempt, *, check_order_amount=True):
    """Validate the actual native relations, including links added from the order side."""
    transaction = attempt.transaction_id
    order = attempt.sale_order_id
    if (
        _checkout_order(transaction) != order
        or transaction.provider_id != attempt.provider_id
        or transaction.company_id != attempt.company_id
        or order.company_id != attempt.company_id
        or order.website_id.company_id != attempt.company_id
        or transaction.payment_method_id.provider_id != attempt.provider_id
        or transaction.payment_method_id.code != "paymos"
        or transaction.partner_id != order.partner_invoice_id
        or transaction.currency_id != order.currency_id
        or transaction.currency_id.name != attempt.currency
        or transaction.is_live is not (attempt.environment == "live")
        or (check_order_amount and order.state not in ("draft", "sent", "sale"))
    ):
        raise CheckoutError("native_binding_changed")
    try:
        amount = money.native_amount(transaction.amount, order.currency_id)
        if amount != attempt.amount:
            raise CheckoutError("native_binding_changed")
        if check_order_amount:
            currency = order.currency_id
            total = money.native_amount(order.amount_total, currency)
            paid = money.native_amount(order.amount_paid, currency, allow_zero=True)
            outstanding = money.remaining_amount(total, paid)
            if transaction.state in ("authorized", "done"):
                outstanding += money.parse_decimal(amount)
            if outstanding != money.parse_decimal(amount):
                raise CheckoutError("native_binding_changed")
    except money.AmountError:
        raise CheckoutError("native_binding_changed") from None


def _coverage(invoice):
    data = invoice.get("payment")
    if not isinstance(data, dict):
        raise CheckoutError("insufficient_payment")
    token = data.get("currency")
    network = data.get("network")
    if (
        not isinstance(token, str)
        or not re.fullmatch(r"[A-Z0-9]{2,16}", token)
        or not isinstance(network, str)
        or not re.fullmatch(r"[A-Z0-9_-]{2,24}", network)
    ):
        raise CheckoutError("insufficient_payment")
    # Callers require a final paid/paid_over status: the server already applied its underpayment tolerance.
    try:
        expected = money.parse_decimal(data.get("expected"))
        paid = money.parse_decimal(data.get("paid"), allow_zero=True)
    except money.AmountError:
        raise CheckoutError("insufficient_payment") from None
    return token, network, expected, paid


def _zero_paid(invoice):
    # The Merchant API omits paid/transfers when no confirmed or included money exists.
    data = invoice.get("payment")
    if data is None:
        return True
    if not isinstance(data, dict) or data.get("transfers") not in (None, []):
        return False
    try:
        money.parse_decimal(data.get("expected"))
        paid = data.get("paid")
        return paid is None or money.parse_decimal(paid, allow_zero=True) == 0
    except money.AmountError:
        return False


def _mark_review(attempt, reason):
    values = {"review_reason": reason}
    if attempt.disposition not in (*_REPLACEABLE, "paid"):
        values["disposition"] = "review"
    attempt.write(values)


def _event_marker(attempt, event_id):
    return attempt.env["paymos.event"].sudo().search([
        ("environment", "=", attempt.environment),
        ("project_id", "=", attempt.project_id),
        ("event_id", "=", event_id),
    ], limit=1)


def _existing_event(attempt, event_id, raw_sha256):
    marker = _event_marker(attempt, event_id)
    if marker and (marker.attempt_id != attempt or not hmac.compare_digest(marker.raw_sha256, raw_sha256)):
        _mark_review(attempt, "event_payload_conflict")
        raise CheckoutError("event_conflict")
    return marker


def _has_native_queue(transaction, payload):
    data = transaction.env["payment.data"]
    data.flush_model(["transaction_id", "payload", "errored"])
    return bool(transaction.env.execute_query(SQL(
        "SELECT 1 FROM payment_data WHERE transaction_id = %s AND errored IS NOT TRUE "
        "AND payload = %s::jsonb LIMIT 1", transaction.id, json.dumps(payload),
    )))


def record_projection(
    attempt, *, event_id, raw_sha256, provider_status, target_state, transaction=None,
    kind="invoice", refund_operation_id=0, provider_reference=None, coverage=None,
    invoice_id=None, external_order_id=None, retry=False,
):
    """Atomically store the checked proof and hand only its identity to Odoo."""
    transaction = transaction if transaction is not None else attempt.transaction_id
    existing = _existing_event(attempt, event_id, raw_sha256)
    if existing and (
        existing.transaction_id != transaction or existing.kind != kind
        or existing.refund_operation_id != refund_operation_id
        or existing.target_state != target_state or existing.provider_status != provider_status
    ):
        _mark_review(attempt, "event_payload_conflict")
        raise CheckoutError("event_conflict")
    try:
        with attempt.env.cr.savepoint():
            if existing:
                payload = {"paymos_event_id": existing.id}
                if retry and not existing.consumed and not _has_native_queue(transaction, payload):
                    transaction._record(payload)
                return existing
            if invoice_id is not None:
                attempt.invoice_id = invoice_id
            values = {
                "company_id": attempt.company_id.id,
                "provider_id": attempt.provider_id.id,
                "connection_id": attempt.connection_id.id,
                "attempt_id": attempt.id,
                "transaction_id": transaction.id,
                "project_id": attempt.project_id,
                "environment": attempt.environment,
                "generation": attempt.generation,
                "event_id": event_id,
                "raw_sha256": raw_sha256,
                "kind": kind,
                "refund_operation_id": refund_operation_id,
                "invoice_id": attempt.invoice_id,
                "external_order_id": external_order_id or attempt.external_order_id,
                "provider_reference": provider_reference or attempt.invoice_id,
                "provider_status": provider_status,
                "target_state": target_state,
                "amount": attempt.amount,
                "currency": attempt.currency,
                "consumed": target_state == "observe",
            }
            if coverage:
                token, network, expected, paid = coverage
                values.update(
                    token_currency=token, token_network=network,
                    token_expected=format(expected, "f"), token_paid=format(paid, "f"),
                )
            marker = attempt.env["paymos.event"].sudo().create(values)
            if target_state != "observe":
                transaction._record({"paymos_event_id": marker.id})
            return marker
    except Exception:
        raise CheckoutError("queue_unavailable") from None


def _blocking_refund(client, credentials, attempt, invoice_id):
    try:
        return _sdk_call(refund.blocking_full_refund, client, credentials, attempt, invoice_id)
    except refund.RefundSnapshotError as error:
        raise CheckoutError(str(error)) from None
    except CheckoutError:
        raise CheckoutError("refund_snapshot_unavailable") from None


def _invoice_observation(attempt, fresh, signed, event_type, credentials, client):
    status = fresh.get("status")
    final = fresh.get("is_final")
    if (
        status not in _INVOICE_STATUSES
        or type(final) is not bool
        or (signed is not None and (signed.get("status") != status or signed.get("is_final") is not final))
        or (event_type not in (None, "invoice.paid_late") and event_type != f"invoice.{status}")
    ):
        raise CheckoutError("invoice_status_changed")

    late = fresh.get("paid_late_at") is not None
    if event_type == "invoice.paid_late" and not late:
        raise CheckoutError("invoice_status_changed")
    current = _current_attempt(attempt.env["paymos.attempt"], attempt.sale_order_id)
    if late or (attempt != current and status in _PAID_STATUSES):
        if current and current != attempt:
            _mark_review(current, "previous_attempt_paid")
        return "observe", "paid_late" if late else "previous_attempt_paid", None
    if attempt.disposition in ("paid", *_REPLACEABLE) or attempt.review_reason in _HARD_REVIEW:
        return "observe", None, None

    if status in _PAID_STATUSES:
        if not final:
            raise CheckoutError("invoice_status_changed")
        try:
            coverage = _coverage(fresh)
            if signed is not None and (
                signed.get("paid_late_at") is not None or _coverage(signed) != coverage
            ):
                raise CheckoutError("invoice_status_changed")
            assert_native_attempt(attempt)
        except CheckoutError as error:
            if error.code == "invoice_status_changed":
                raise
            return "observe", error.code, None
        assert_accounting_ready(attempt.provider_id, attempt.transaction_id.currency_id)
        if _blocking_refund(client, credentials, attempt, fresh["invoice_id"]):
            return "observe", "external_full_refund", None
        return "done", None, coverage

    if status in _CLOSED_STATUSES:
        if not final:
            raise CheckoutError("invoice_status_changed")
        if not _zero_paid(fresh) or (signed is not None and not _zero_paid(signed)):
            return "observe", "closed_with_money", None
        try:
            assert_native_attempt(attempt, check_order_amount=False)
        except CheckoutError as error:
            return "observe", error.code, None
        return "cancel", None, None

    if status == "underpaid" and final:
        return "observe", "insufficient_payment", None
    return "observe", None, None


def _queue_invoice(attempt, fresh, signed, event_type, event_id, raw_sha256, credentials, client, *, retry=False):
    try:
        target_state, reason, coverage = _invoice_observation(
            attempt, fresh, signed, event_type, credentials, client,
        )
    except CheckoutError as error:
        if error.code.startswith("refund_") or error.code == "accounting_unavailable":
            attempt.invoice_id = fresh["invoice_id"]
            _mark_review(attempt, error.code)
        raise
    if retry:
        raw_sha256 = hashlib.sha256(f"{raw_sha256}|{target_state}|{reason or ''}".encode("ascii")).hexdigest()
        event_id = f"reconcile_{raw_sha256}"
    marker = record_projection(
        attempt, event_id=event_id, raw_sha256=raw_sha256,
        provider_status=fresh["status"], target_state=target_state, coverage=coverage,
        invoice_id=fresh["invoice_id"], retry=retry,
    )
    if reason:
        _mark_review(attempt, reason)
    elif target_state == "observe" and attempt.disposition == "prepared" and fresh["status"] in _OPEN_INVOICES:
        attempt.disposition = "open"
    return marker


def handle_webhook(env, raw, signature):
    if not raw or len(raw) > MAX_WEBHOOK_BYTES:
        raise WebhookError(413 if raw else 400)
    if not isinstance(signature, str) or not signature or len(signature) > 2048:
        raise WebhookError(401)

    event = _read_webhook(raw)
    data = event["data"]
    event_type = event["event_type"]
    withdrawal = event_type in _WITHDRAWAL_EVENTS
    attempt = _webhook_attempt(env, data, withdrawal=withdrawal)
    try:
        credentials = credentials_for_attempt(attempt)
        verifier = _sdk_call(sdk.webhook_verifier, credentials)
        if not verifier.verify(signature, raw):
            raise WebhookError(401)
        if not withdrawal:
            try:
                check_invoice_binding(data, attempt, expected_invoice_id=data["invoice_id"])
            except CheckoutError:
                raise WebhookError(400) from None

        _attempt_lock(attempt)
        transaction = attempt.transaction_id
        if not transaction.try_lock_for_update(allow_referencing=True):
            raise CheckoutError("checkout_busy")
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if _existing_event(attempt, event["event_id"], raw_sha256):
            return
        if withdrawal:
            result = transaction._paymos_process_withdrawal(event | {
                "attempt_id": attempt.id, "raw_sha256": raw_sha256,
            })
            if result not in ("processed", "ignored"):
                raise WebhookError(503)
            return

        client = _sdk_call(sdk.make_client, credentials)
        fresh = _sdk_call(client.invoices.get, data["invoice_id"])
        check_invoice_binding(fresh, attempt, expected_invoice_id=data["invoice_id"])
        _queue_invoice(
            attempt, fresh, data, event_type, event["event_id"], raw_sha256, credentials, client,
        )
    except CheckoutError as error:
        raise WebhookError(409 if error.code == "event_conflict" else 503) from None


def validate_invoice_marker(marker):
    """Recheck an unconsumed exact marker before native settlement."""
    attempt = marker.attempt_id
    _attempt_lock(attempt)
    assert_native_attempt(attempt, check_order_amount=marker.target_state == "done")
    if (
        _current_attempt(attempt.env["paymos.attempt"], attempt.sale_order_id) != attempt
        or attempt.review_reason in _HARD_REVIEW
        or attempt.disposition in (*_REPLACEABLE, "paid")
    ):
        return False
    credentials = credentials_for_attempt(attempt)
    client = _sdk_call(sdk.make_client, credentials)
    fresh = _sdk_call(client.invoices.get, marker.invoice_id)
    check_invoice_binding(fresh, attempt, expected_invoice_id=marker.invoice_id)
    if fresh.get("status") != marker.provider_status or fresh.get("is_final") is not True:
        raise CheckoutError("invoice_status_changed")
    if fresh.get("paid_late_at") is not None:
        _mark_review(attempt, "paid_late")
        return False

    if marker.target_state == "cancel":
        return marker.provider_status in _CLOSED_STATUSES and _zero_paid(fresh)
    if marker.target_state != "done" or marker.provider_status not in _PAID_STATUSES:
        return False
    if _coverage(fresh) != (
        marker.token_currency, marker.token_network,
        money.parse_decimal(marker.token_expected), money.parse_decimal(marker.token_paid),
    ):
        raise CheckoutError("invoice_status_changed")
    assert_accounting_ready(attempt.provider_id, attempt.transaction_id.currency_id)
    if _blocking_refund(client, credentials, attempt, marker.invoice_id):
        _mark_review(attempt, "external_full_refund")
        return False
    return True


def reconcile_attempt(attempt):
    """Re-read a saved invoice through its generation and reuse the native queue."""
    if not attempt.invoice_id:
        return "deferred"
    _attempt_lock(attempt)
    if not attempt.transaction_id.try_lock_for_update(allow_referencing=True):
        raise CheckoutError("checkout_busy")
    credentials = credentials_for_attempt(attempt)
    client = _sdk_call(sdk.make_client, credentials)
    fresh = _sdk_call(client.invoices.get, attempt.invoice_id)
    check_invoice_binding(fresh, attempt, expected_invoice_id=attempt.invoice_id)
    token = fresh.get("payment")
    amounts = [token.get(key) for key in ("currency", "network", "expected", "paid")] if isinstance(token, dict) else []
    evidence = json.dumps([
        attempt.id, attempt.invoice_id, fresh.get("status"), fresh.get("is_final"),
        fresh.get("paid_late_at"), amounts,
    ], separators=(",", ":"))
    digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    marker = _queue_invoice(
        attempt, fresh, None, None, f"reconcile_{digest}", digest, credentials, client, retry=True,
    )
    return "observed" if marker.target_state == "observe" else "queued"
