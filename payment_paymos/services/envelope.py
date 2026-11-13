import json
import os
import re

from cryptography.fernet import Fernet, InvalidToken


_KEY_ENV = "PAYMOS_ODOO_ENVELOPE_KEY"
_MAX_TOKEN_LENGTH = 32768


class EnvelopeError(ValueError):
    pass


class Envelope:
    def __init__(self):
        key = os.environ.get(_KEY_ENV, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}=", key):
            raise EnvelopeError("encryption_key_unavailable")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (TypeError, ValueError):
            raise EnvelopeError("encryption_key_unavailable") from None

    def seal(self, kind, binding, payload):
        body = json.dumps(
            {"version": 1, "kind": kind, "binding": binding, "payload": payload},
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        token = self._fernet.encrypt(body).decode("ascii")
        if len(token) > _MAX_TOKEN_LENGTH:
            raise EnvelopeError("invalid_envelope")
        return token

    def open(self, token, kind, binding):
        if not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN_LENGTH:
            raise EnvelopeError("invalid_envelope")
        try:
            body = self._fernet.decrypt(token.encode("ascii"))
            record = json.loads(body)
        except (InvalidToken, ValueError):
            raise EnvelopeError("invalid_envelope") from None

        if (
            not isinstance(record, dict)
            or type(record.get("version")) is not int
            or record["version"] != 1
            or record.get("kind") != kind
            or not isinstance(record.get("payload"), dict)
        ):
            raise EnvelopeError("invalid_envelope")
        if record.get("binding") != binding:
            raise EnvelopeError("binding_mismatch")
        return record["payload"]
