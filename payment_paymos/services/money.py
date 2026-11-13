import math
import re
from decimal import ROUND_HALF_UP, Decimal, localcontext


_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,27})(?:\.[0-9]{1,28})?")
_CURRENCY = re.compile(r"[A-Z]{3}")


class AmountError(ValueError):
    pass


def parse_decimal(value, *, allow_zero=False):
    """Read a bounded wire decimal without an intermediate float."""
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise AmountError("invalid_decimal")
    result = Decimal(value)
    if not allow_zero and result.is_zero():
        raise AmountError("invalid_decimal")
    return result


def native_amount(value, currency, *, allow_zero=False):
    """Validate the native currency quantum and return a stable amount string."""
    if not currency or not _CURRENCY.fullmatch(currency.name or ""):
        raise AmountError("invalid_currency")
    if type(value) is float:
        if not math.isfinite(value) or value < 0:
            raise AmountError("invalid_decimal")
        amount = parse_decimal(format(Decimal(str(value)), "f"), allow_zero=allow_zero)
    elif type(value) is int:
        amount = parse_decimal(str(value), allow_zero=allow_zero)
    else:
        amount = parse_decimal(value, allow_zero=allow_zero)

    rounding = parse_decimal(format(Decimal(str(currency.rounding)), "f"))
    places = currency.decimal_places
    if not 0 <= places <= 28 or rounding.as_tuple().exponent < -places:
        raise AmountError("invalid_currency_rounding")
    with localcontext() as context:
        context.prec = 64
        units = (amount / rounding).to_integral_value(rounding=ROUND_HALF_UP)
        exact = units * rounding
        if exact != amount:
            # Odoo's float rounding can leave a one-ULP binary artifact.
            if type(value) is not float or abs(exact - amount) > Decimal(str(math.ulp(value))):
                raise AmountError("invalid_minor_units")
        if not allow_zero and not exact:
            raise AmountError("invalid_decimal")
        return format(exact, f".{places}f")

def remaining_amount(total, paid):
    with localcontext() as context:
        context.prec = 64
        return parse_decimal(total) - parse_decimal(paid, allow_zero=True)
