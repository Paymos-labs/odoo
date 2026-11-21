import json
import re
from dataclasses import dataclass, field
from http.client import HTTPException
from importlib import import_module
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ORIGIN = "https://api.paymos.io"
CHECKOUT_ORIGIN = "https://checkout.paymos.io"
SDK_MAJOR = 1
MIN_SDK_VERSION = (1, 6, 0)
_SDK_VERSION_RE = re.compile(rf"{SDK_MAJOR}\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)
HTTP_TIMEOUT = 8
_API_MAX_RESPONSE = 1024 * 1024
_SAFE_API_CODES = {
    "forbidden",
    "idempotency_conflict",
    "invoice_not_found",
    "not_sandbox",
    "refund_not_found",
    "refunds_disabled",
    "validation_error",
}


class PaymosTransportError(OSError):
    pass


class PaymosSdkError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(repr=False)
    retry_after: int | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def parse_https_url(value):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 2048
        or any(not 33 <= ord(char) <= 126 or char == "\\" for char in value)
        or "#" in value
    ):
        raise ValueError("invalid_https_url")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("invalid_https_url") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or (port is not None and port < 1)
        or parts.path.startswith("//")
    ):
        raise ValueError("invalid_https_url")
    return parts


def trusted_url(value, origin):
    parts = parse_https_url(value)
    if parts.netloc != urlsplit(origin).netloc:
        raise ValueError("untrusted_https_origin")
    return parts


def validate_checkout_url(value):
    parts = trusted_url(value, CHECKOUT_ORIGIN)
    if not parts.path.startswith("/"):
        raise ValueError("invalid_checkout_url")
    return value


def request_https(method, url, headers, body, timeout, *, origin, max_bytes):
    try:
        trusted_url(url, origin)
        deadline = monotonic() + timeout
        request = Request(url, data=body, headers=dict(headers), method=method)
        opener = build_opener(_NoRedirect)
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error

        with response:
            if 300 <= response.status < 400:
                return HttpResponse(response.status, b"")
            chunks = bytearray()
            while monotonic() < deadline:
                chunk = response.read1(min(8192, max_bytes + 1 - len(chunks)))
                if not chunk:
                    header = response.headers.get("Retry-After", "")
                    retry_after = None
                    if len(header) <= 6 and header.isascii() and header.isdigit():
                        retry_after = min(60, int(header))
                    return HttpResponse(response.status, bytes(chunks), retry_after)
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    break
    except (HTTPException, OSError, ValueError):
        raise PaymosTransportError("Paymos HTTPS request failed.") from None
    raise PaymosTransportError("Paymos HTTPS response exceeded its limits.")


def is_supported_sdk_version(value):
    if not isinstance(value, str):
        return False
    match = _SDK_VERSION_RE.fullmatch(value)
    if match is None:
        return False
    return (SDK_MAJOR, int(match.group(1)), int(match.group(2))) >= MIN_SDK_VERSION


def _vendor():
    try:
        vendor = import_module(".._vendor.paymos", package=__package__)
    except ImportError:
        raise PaymosSdkError("The bundled Paymos SDK is unavailable.") from None
    if not is_supported_sdk_version(getattr(vendor, "VERSION", None)):
        raise PaymosSdkError("The bundled Paymos SDK version does not match.")
    return vendor


def _safe_api_error(status, body):
    code = ""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and isinstance(payload.get("code"), str):
            if payload["code"] in _SAFE_API_CODES:
                code = payload["code"]
    except (ValueError, RecursionError):
        pass
    return json.dumps({
        "type": "about:blank",
        "title": "Paymos API request failed.",
        "status": status,
        "code": code,
        "detail": "",
    })


def make_client(credentials):
    vendor = _vendor()

    def transport(method, url, headers, body, timeout):
        response = request_https(
            method, url, headers, body, timeout, origin=API_ORIGIN, max_bytes=_API_MAX_RESPONSE,
        )
        try:
            response_body = response.body.decode("utf-8")
        except UnicodeError:
            raise PaymosTransportError("Paymos returned invalid UTF-8.") from None
        if not 200 <= response.status < 300:
            response_body = _safe_api_error(response.status, response_body)
        response_headers = (
            {"Retry-After": str(response.retry_after)} if response.retry_after is not None else {}
        )
        return vendor.Response(response.status, response_body, response_headers)

    return vendor.Paymos(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        base_url=API_ORIGIN,
        timeout=HTTP_TIMEOUT,
        max_retries=0,
        transport=transport,
    )


def webhook_verifier(credentials):
    return _vendor().WebhookVerifier(credentials.webhook_secret)
