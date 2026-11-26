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


def calculate_item_totals(items_with_price: list[tuple[float, int]], prior_orders: int = 0) -> list[float]:
    """Per-line totals including each line's share of the discounts.

    Finance reporting reads these from orders.order_items.total; their sum must
    equal the order total to the kopeck. Volume-discount lines are priced
    independently (legacy batch-pricing path). The tier-only path allocates the
    order-level discount across lines and moves the rounding remainder to the
    last line so the split always reconciles.
    """
    subtotal = sum(price * qty for price, qty in items_with_price)
    total_qty = sum(qty for _, qty in items_with_price)
    pct = tier_pct(prior_orders + 1)

    if len(items_with_price) == 1:
        # A single-line order carries the whole discounted total by definition.
        return [calculate_total(items_with_price, prior_orders)]

    if total_qty >= 5:
        rate = 0.9 * (1 - pct / 100)
        return [round(price * qty * rate, 2) for price, qty in items_with_price]

    if pct:
        rate = 1 - pct / 100
        lines = [round(price * qty * rate, 2) for price, qty in items_with_price]
        remainder = round(subtotal * rate, 2) - round(sum(lines), 2)
        lines[-1] = round(lines[-1] + remainder, 2)
        return lines

    return [round(price * qty, 2) for price, qty in items_with_price]
