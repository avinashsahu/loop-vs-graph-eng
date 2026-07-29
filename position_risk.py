import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PositionPlan:
    shares: int
    entry_price: float
    stop_price: float
    stop_distance: float
    capital_required: float
    risk_budget: float
    max_loss_at_stop: float
    allocation_cap: float
    binding_constraint: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RiskRejection:
    reason_code: str
    message: str

    def to_dict(self):
        return asdict(self)


def size_position(
    *,
    principal,
    entry_price,
    atr,
    max_loss_pct,
    max_allocation_pct,
    atr_stop_multiple,
):
    """Size shares under both loss-at-stop and capital-allocation constraints."""
    values = {
        "principal": principal,
        "entry_price": entry_price,
        "atr": atr,
        "max_loss_pct": max_loss_pct,
        "max_allocation_pct": max_allocation_pct,
        "atr_stop_multiple": atr_stop_multiple,
    }
    invalid = [
        name
        for name, value in values.items()
        if not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ]
    if invalid:
        return RiskRejection(
            reason_code="INVALID_INPUT",
            message=f"positive finite values required for {', '.join(invalid)}",
        )
    if max_loss_pct > 5:
        return RiskRejection(
            reason_code="MAX_LOSS_PCT_TOO_HIGH",
            message="max_loss_pct must not exceed 5%",
        )
    if max_allocation_pct > 100:
        return RiskRejection(
            reason_code="MAX_ALLOCATION_PCT_TOO_HIGH",
            message="max_allocation_pct must not exceed 100%",
        )

    stop_distance = atr * atr_stop_multiple
    stop_price = entry_price - stop_distance
    if stop_price <= 0:
        return RiskRejection(
            reason_code="NON_POSITIVE_STOP",
            message="ATR-based stop is not above zero",
        )

    risk_budget = principal * max_loss_pct / 100
    allocation_cap = principal * max_allocation_pct / 100
    shares_by_risk = int(risk_budget // stop_distance)
    shares_by_allocation = int(allocation_cap // entry_price)
    shares = min(shares_by_risk, shares_by_allocation)

    if shares < 1:
        return RiskRejection(
            reason_code="ZERO_SHARES",
            message=(
                "risk budget or allocation cap cannot fund one share at the "
                "planned entry and stop"
            ),
        )

    binding_constraint = (
        "risk_budget"
        if shares_by_risk <= shares_by_allocation
        else "allocation_cap"
    )
    return PositionPlan(
        shares=shares,
        entry_price=float(entry_price),
        stop_price=float(stop_price),
        stop_distance=float(stop_distance),
        capital_required=float(shares * entry_price),
        risk_budget=float(risk_budget),
        max_loss_at_stop=float(shares * stop_distance),
        allocation_cap=float(allocation_cap),
        binding_constraint=binding_constraint,
    )
