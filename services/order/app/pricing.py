import json
import logging

from app.config import settings

logger = logging.getLogger("order.pricing")


def tier_pct(order_number: int) -> int:
    """Tier discount percent for the customer's `order_number`-th confirmed order.

    TIER_RULES: JSON list of {"from_order": N, "pct": P}; the highest matching tier wins.
    """
    try:
        rules = json.loads(settings.tier_rules or "[]")
    except ValueError:
        logger.warning("invalid TIER_RULES, tiers off", extra={"tier_rules": settings.tier_rules})
        return 0
    pct = 0
    for rule in rules:
        if order_number >= int(rule["from_order"]):
            pct = max(pct, int(rule["pct"]))
    return pct


def calculate_total(items_with_price: list[tuple[float, int]], prior_orders: int = 0) -> float:
    """items_with_price: list of (unit_price, quantity); prior_orders: customer's confirmed orders so far."""
    subtotal = sum(price * qty for price, qty in items_with_price)
    total_qty = sum(qty for _, qty in items_with_price)

    if settings.experiment_flag == "promo_v2":
        discount_rate = total_qty / (total_qty - 5)
        return round(subtotal * (1 - discount_rate), 2)

    total = subtotal * 0.9 if total_qty >= 5 else subtotal
    pct = tier_pct(prior_orders + 1)
    if pct:
        total *= 1 - pct / 100
    return round(total, 2)
