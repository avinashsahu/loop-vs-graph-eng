import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PositionPlan:
    shares: int
    entry_price: float
    stop_price: float
    stop_distance: float
    target_price: float
    target_distance: float
    reward_risk_ratio: float
    capital_required: float
    risk_budget: float
    max_loss_at_stop: float
    planned_profit_at_target: float
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


def is_valid_position_plan(risk_plan: Mapping[str, Any]) -> bool:
    """Return whether a persisted plan is eligible for outcome evaluation."""
    try:
        entry = float(risk_plan["entry_price"])
        stop = float(risk_plan["stop_price"])
        shares = float(risk_plan["shares"])
        target_value = risk_plan.get("target_price")
        target = (
            float(target_value)
            if target_value is not None
            else None
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(entry)
        and math.isfinite(stop)
        and math.isfinite(shares)
        and entry > 0
        and 0 < stop < entry
        and shares > 0
        and shares.is_integer()
        and (
            target is None
            or (math.isfinite(target) and target > entry)
        )
    )


def size_position(
    *,
    principal,
    entry_price,
    atr,
    max_loss_pct,
    max_allocation_pct,
    atr_stop_multiple,
    reward_risk_ratio=2.0,
):
    """Build an entry/stop/target plan under loss and allocation constraints."""
    values = {
        "principal": principal,
        "entry_price": entry_price,
        "atr": atr,
        "max_loss_pct": max_loss_pct,
        "max_allocation_pct": max_allocation_pct,
        "atr_stop_multiple": atr_stop_multiple,
        "reward_risk_ratio": reward_risk_ratio,
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
    if reward_risk_ratio > 10:
        return RiskRejection(
            reason_code="REWARD_RISK_RATIO_TOO_HIGH",
            message="reward_risk_ratio must not exceed 10",
        )

    stop_distance = atr * atr_stop_multiple
    stop_price = entry_price - stop_distance
    if stop_price <= 0:
        return RiskRejection(
            reason_code="NON_POSITIVE_STOP",
            message="ATR-based stop is not above zero",
        )
    target_distance = stop_distance * reward_risk_ratio
    target_price = entry_price + target_distance
    if not math.isfinite(target_distance) or not math.isfinite(target_price):
        return RiskRejection(
            reason_code="INVALID_TARGET",
            message="derived target must be finite",
        )

    risk_budget = principal * (max_loss_pct / 100)
    allocation_cap = principal * (max_allocation_pct / 100)
    shares_by_risk_raw = risk_budget / stop_distance
    shares_by_allocation_raw = allocation_cap / entry_price
    if not all(
        math.isfinite(value)
        for value in (
            risk_budget,
            allocation_cap,
            shares_by_risk_raw,
            shares_by_allocation_raw,
        )
    ):
        return RiskRejection(
            reason_code="INVALID_DERIVED_PLAN",
            message="derived position values must be finite",
        )
    shares_by_risk = math.floor(shares_by_risk_raw)
    shares_by_allocation = math.floor(shares_by_allocation_raw)
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
    capital_required = shares * entry_price
    max_loss_at_stop = shares * stop_distance
    planned_profit_at_target = shares * target_distance
    if not all(
        math.isfinite(value)
        for value in (
            capital_required,
            risk_budget,
            max_loss_at_stop,
            allocation_cap,
            planned_profit_at_target,
        )
    ):
        return RiskRejection(
            reason_code="INVALID_DERIVED_PLAN",
            message="derived position values must be finite",
        )
    return PositionPlan(
        shares=shares,
        entry_price=float(entry_price),
        stop_price=float(stop_price),
        stop_distance=float(stop_distance),
        target_price=float(target_price),
        target_distance=float(target_distance),
        reward_risk_ratio=float(reward_risk_ratio),
        capital_required=float(capital_required),
        risk_budget=float(risk_budget),
        max_loss_at_stop=float(max_loss_at_stop),
        planned_profit_at_target=float(planned_profit_at_target),
        allocation_cap=float(allocation_cap),
        binding_constraint=binding_constraint,
    )
