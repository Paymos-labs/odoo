import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode

from . import sdk
from .envelope import Envelope, EnvelopeError


CONNECT_ORIGIN = "https://app.paymos.io"
_CLIENT_ID = "paymos-cms"
_SCOPE = "cms:bootstrap"
_DEVICE_PATH = "/connect/cms/device"
_TOKEN_PATH = "/connect/token"
_APPROVAL_PATH = "/connect/cms/verify"
_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_MAX_RESPONSE = 65536
_MAX_TTL = 3600
_MAX_INTERVAL = 60


class ConnectError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DeviceBinding:
    provider_id: int
    company_id: int
    user_id: int
    source_url: str
    session_id: str = field(repr=False)


@dataclass(frozen=True)
class DeviceChallenge:
    flow_id: str
    user_code: str = field(repr=False)
    verification_url: str = field(repr=False)
    expires_at: int
    retry_after: int
    envelope: str = field(repr=False)


@dataclass(frozen=True)
class Credentials:
    project_id: str
    environment: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    capabilities: tuple[str, ...]
    refund_contract_version: int | None
    refunds_enabled: bool


@dataclass(frozen=True)
class DevicePoll:
    status: str
    retry_after: int = 0
    envelope: str | None = field(default=None, repr=False)
    credentials: dict[str, Credentials] | None = field(default=None, repr=False)


def canonical_source(value):
    try:
        parts = sdk.parse_https_url(value)
    except ValueError:
        raise ConnectError("invalid_source") from None
    if parts.path not in ("", "/") or "?" in value or parts.netloc.endswith(":"):
        raise ConnectError("invalid_source")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    elif not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        host,
    ):
        raise ConnectError("invalid_source")
    port = "" if parts.port in (None, 443) else f":{parts.port}"
    return f"https://{host}{port}"


def _record_id(value):
    return type(value) is int and value > 0


def _matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _device_context(binding, flow_id):
    if (
        not all(_record_id(value) for value in (binding.provider_id, binding.company_id, binding.user_id))
        or not _matches(binding.session_id, r"[!-~]{1,512}")
        or not _matches(flow_id, r"[0-9a-f]{32}")
    ):
        raise ConnectError("binding_mismatch")
    return {
        "provider_id": binding.provider_id,
        "company_id": binding.company_id,
        "user_id": binding.user_id,
        "source_url": canonical_source(binding.source_url),
        "session_hash": hashlib.sha256(binding.session_id.encode("ascii")).hexdigest(),
        "flow_id": flow_id,
    }


def _credential_context(*, provider_id, company_id, generation, source_url, environment, project_id):
    if (
        not all(_record_id(value) for value in (provider_id, company_id, generation))
        or environment not in ("sandbox", "live")
        or not _matches(project_id, r"prj_[A-Za-z0-9_-]{1,128}")
    ):
        raise ConnectError("binding_mismatch")
    return {
        "provider_id": provider_id,
        "company_id": company_id,
        "generation": generation,
        "source_url": canonical_source(source_url),
        "environment": environment,
        "project_id": project_id,
    }


def _credential(payload, environment, project_id):
    stage = "test" if environment == "sandbox" else "live"
    key = payload.get("api_key")
    secret = payload.get("api_secret")
    webhook_secret = payload.get("webhook_secret")
    if not all(
        _matches(value, f"{prefix}_{stage}_[A-Za-z0-9_-]{{1,512}}")
        for prefix, value in (("pk", key), ("sk", secret), ("whsec", webhook_secret))
    ):
        raise ConnectError("invalid_credentials")

    capabilities = payload.get("capabilities", [])
    version = payload.get("refund_contract_version")
    enabled = payload.get("refunds_enabled")
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 32
        or not all(_matches(capability, r"[a-z][a-z0-9_]{0,63}") for capability in capabilities)
        or len(set(capabilities)) != len(capabilities)
        or (version is None and enabled is not None)
        or (version is not None and (not _record_id(version) or type(enabled) is not bool))
        or ("invoice_refunds_v1" in capabilities) != (version == 1)
    ):
        raise ConnectError("invalid_credentials")
    return Credentials(
        project_id, environment, key, secret, webhook_secret, tuple(capabilities), version,
        version == 1 and enabled is True,
    )


def seal_credentials(credentials, *, provider_id, company_id, generation, source_url):
    context = _credential_context(
        provider_id=provider_id, company_id=company_id, generation=generation, source_url=source_url,
        environment=credentials.environment, project_id=credentials.project_id,
    )
    payload = {
        "api_key": credentials.api_key,
        "api_secret": credentials.api_secret,
        "webhook_secret": credentials.webhook_secret,
        "capabilities": list(credentials.capabilities),
        "refund_contract_version": credentials.refund_contract_version,
    }
    if credentials.refund_contract_version is not None:
        payload["refunds_enabled"] = credentials.refunds_enabled
    try:
        return Envelope().seal("credentials", context, payload)
    except EnvelopeError as error:
        raise ConnectError(str(error)) from None


def open_credentials(token, *, provider_id, company_id, generation, source_url, environment, project_id):
    context = _credential_context(
        provider_id=provider_id, company_id=company_id, generation=generation, source_url=source_url,
        environment=environment, project_id=project_id,
    )
    try:
        payload = Envelope().open(token, "credentials", context)
    except EnvelopeError as error:
        raise ConnectError(str(error)) from None
    return _credential(payload, environment, project_id)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_member")
        result[key] = value
    return result


def _invalid_json_constant(_):
    raise ValueError("invalid_json_constant")


def _json_response(response):
    if len(response.body) > _MAX_RESPONSE:
        raise ConnectError("invalid_response")
    try:
        payload = json.loads(
            response.body.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_invalid_json_constant,
        )
    except (ValueError, RecursionError):
        raise ConnectError("invalid_response") from None
    if not isinstance(payload, dict):
        raise ConnectError("invalid_response")
    return payload


def _approval_url(payload, user_code, source):
    try:
        parts = sdk.trusted_url(payload.get("verification_uri_complete"), CONNECT_ORIGIN)
        query = parse_qsl(parts.query, strict_parsing=True, max_num_fields=2)
        if parts.path != _APPROVAL_PATH or query != [("user_code", user_code)]:
            raise ValueError("invalid_approval_url")
        if "verification_uri" in payload and payload["verification_uri"] != CONNECT_ORIGIN + _APPROVAL_PATH:
            raise ValueError("invalid_approval_url")
    except ValueError:
        raise ConnectError("invalid_approval_url") from None
    return f"{CONNECT_ORIGIN}{_APPROVAL_PATH}?" + urlencode({
        "user_code": user_code, "cms": "odoo", "source_url": source,
    })


def _approved_credentials(payload, source):
    if (
        not _matches(payload.get("access_token"), r"[A-Za-z0-9._~+/-]{1,8192}={0,2}")
        or not isinstance(payload.get("token_type"), str)
        or payload["token_type"].lower() != "bearer"
        or ("scope" in payload and payload["scope"] != _SCOPE)
        or "refresh_token" in payload
        or ("expires_in" in payload and not _record_id(payload["expires_in"]))
    ):
        raise ConnectError("invalid_token")
    config = payload.get("paymos_config")
    if not isinstance(config, dict) or type(config.get("schema")) is not int or config["schema"] != 1:
        raise ConnectError("invalid_response")
    if config.get("plugin") != "odoo" or config.get("source_url") != source:
        raise ConnectError("binding_mismatch")
    environments = config.get("environments")
    if not isinstance(environments, dict) or set(environments) != {"sandbox", "live"}:
        raise ConnectError("invalid_credentials")
    credentials = {}
    for environment, values in environments.items():
        if (
            not isinstance(values, dict)
            or not _matches(values.get("project_id"), r"prj_[A-Za-z0-9_-]{1,128}")
            or values.get("base_url") != sdk.API_ORIGIN
        ):
            raise ConnectError("invalid_credentials")
        credentials[environment] = _credential(values, environment, values["project_id"])
    if credentials["sandbox"].project_id != credentials["live"].project_id:
        raise ConnectError("binding_mismatch")
    return credentials


class DeviceConnect:
    def __init__(self, *, transport=None, clock=None):
        self._transport = transport if transport is not None else sdk.request_https
        self._clock = clock if clock is not None else time.time

    def _request(self, path, fields):
        return self._transport(
            "POST",
            CONNECT_ORIGIN + path,
            {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            urlencode(fields).encode("ascii"),
            sdk.HTTP_TIMEOUT,
            origin=CONNECT_ORIGIN,
            max_bytes=_MAX_RESPONSE,
        )

    def start(self, binding):
        flow_id = secrets.token_hex(16)
        context = _device_context(binding, flow_id)
        try:
            envelope = Envelope()
        except EnvelopeError as error:
            raise ConnectError(str(error)) from None

        now = int(self._clock())
        try:
            response = self._request(_DEVICE_PATH, {"client_id": _CLIENT_ID, "scope": _SCOPE})
        except sdk.PaymosTransportError:
            raise ConnectError("connection_unavailable") from None
        if response.status != 200:
            raise ConnectError("connection_unavailable")

        payload = _json_response(response)
        user_code = payload.get("user_code")
        device_code = payload.get("device_code")
        interval = payload.get("interval", 5)
        ttl = payload.get("expires_in")
        if (
            not _matches(user_code, r"[A-Z0-9-]{4,32}")
            or not _matches(device_code, r"[!-~]{1,4096}")
            or type(interval) is not int
            or type(ttl) is not int
            or not 1 <= interval <= _MAX_INTERVAL
            or not interval < ttl <= _MAX_TTL
        ):
            raise ConnectError("invalid_response")
        verification_url = _approval_url(payload, user_code, context["source_url"])
        expires_at = now + ttl
        now = int(self._clock())
        if now >= expires_at:
            raise ConnectError("device_expired")
        retry_after = min(interval, expires_at - now)
        state = {
            "device_code": device_code, "expires_at": expires_at, "interval": interval,
            "poll_at": now + retry_after,
        }
        return DeviceChallenge(
            flow_id, user_code, verification_url, expires_at, retry_after,
            envelope.seal("device", context, state),
        )

    def poll(self, binding, token, flow_id):
        context = _device_context(binding, flow_id)
        try:
            envelope = Envelope()
            state = envelope.open(token, "device", context)
        except EnvelopeError as error:
            raise ConnectError(str(error)) from None
        if (
            not _matches(state.get("device_code"), r"[!-~]{1,4096}")
            or not all(_record_id(state.get(key)) for key in ("interval", "expires_at", "poll_at"))
            or not 1 <= state["interval"] <= _MAX_INTERVAL
            or state["poll_at"] > state["expires_at"]
        ):
            raise ConnectError("invalid_envelope")
        now = int(self._clock())
        if now >= state["expires_at"]:
            return DevicePoll("expired")
        if now < state["poll_at"]:
            return DevicePoll("waiting", state["poll_at"] - now, token)

        try:
            response = self._request(_TOKEN_PATH, {
                "grant_type": _GRANT, "device_code": state["device_code"], "client_id": _CLIENT_ID,
            })
        except sdk.PaymosTransportError:
            response = None

        now = int(self._clock())
        if now >= state["expires_at"]:
            return DevicePoll("expired")
        interval = state["interval"]
        if response is None or 500 <= response.status < 600:
            interval *= 2
        elif response.status == 429:
            interval = max(interval + 5, response.retry_after or 0)
        else:
            if response.status not in (200, 400):
                raise ConnectError("connection_unavailable")
            payload = _json_response(response)
            if response.status == 200:
                return DevicePoll("connected", credentials=_approved_credentials(payload, context["source_url"]))

            error = payload.get("error")
            if error in ("invalid_grant", "expired_token"):
                return DevicePoll("expired")
            if error == "access_denied":
                return DevicePoll("denied")
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                raise ConnectError("connection_unavailable")

        interval = min(interval, _MAX_INTERVAL)
        retry_after = min(interval, state["expires_at"] - now)
        state.update(interval=interval, poll_at=now + retry_after)
        return DevicePoll("waiting", retry_after, envelope.seal("device", context, state))
