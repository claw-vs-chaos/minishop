import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import catalog_client
from app.database import get_session
from app.events import publish_order_event
from app.models import Order, OrderItem, OrderStatus, User
from app.pricing import calculate_item_totals, calculate_total
from app.schemas import OrderCreate, OrderOut

logger = logging.getLogger("order.orders")
router = APIRouter(prefix="/orders", tags=["orders"])


CUSTOMER_ORDERS_LIMIT = 100


def _out(order: Order, email: str | None) -> OrderOut:
    return OrderOut.model_validate(order).model_copy(update={"user_email": email})


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: int, session: AsyncSession = Depends(get_session)):
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    await session.refresh(order, attribute_names=["items"])
    user = await session.get(User, order.user_id)
    return _out(order, user.email if user else None)


@router.get("", response_model=list[OrderOut])
async def list_orders(email: str | None = None, session: AsyncSession = Depends(get_session)):
    if email is None:
        result = await session.execute(select(Order).order_by(Order.id.desc()))
        orders = result.scalars().all()
        for order in orders:
            await session.refresh(order, attribute_names=["items"])
        return orders

    result = await session.execute(
        select(Order)
        .join(User, User.id == Order.user_id)
        .where(User.email == email)
        .options(selectinload(Order.items))
        .order_by(Order.id.desc())
        .limit(CUSTOMER_ORDERS_LIMIT)
    )
    return [_out(order, email) for order in result.scalars().all()]


@router.post("", response_model=OrderOut, status_code=201)
async def create_order(payload: OrderCreate, session: AsyncSession = Depends(get_session)):
    if not payload.items:
        raise HTTPException(status_code=400, detail="order must contain at least one item")

    result = await session.execute(select(User).where(User.email == payload.user_email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=payload.user_email, name=payload.user_name)
        session.add(user)
        await session.flush()

    prior_orders = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.user_id == user.id, Order.status == OrderStatus.confirmed)
    )

    async with httpx.AsyncClient() as client:
        products_by_id = {}
        try:
            for item in payload.items:
                products_by_id[item.product_id] = await catalog_client.get_product(client, item.product_id)
        except catalog_client.ProductNotFound as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except catalog_client.CatalogUnavailable as exc:
            raise HTTPException(status_code=503, detail="catalog service unavailable") from exc

        order = Order(user_id=user.id, status=OrderStatus.created, total_amount=0)
        session.add(order)
        await session.flush()

        # Everything from here through commit() must release any stock this
        # request reserved if it fails partway - a crash in pricing is just
        # as much a reason to give the stock back as a failed reservation
        # call is. A previous version only covered the reservation loop
        # itself, silently leaking reserved stock on any later exception
        # (e.g. the discount_overflow chaos bug's ZeroDivisionError).
        reserved: list[tuple[int, int]] = []
        try:
            for item in payload.items:
                ok = await catalog_client.reserve_stock(client, item.product_id, item.quantity, order.id)
                if not ok:
                    raise HTTPException(
                        status_code=409,
                        detail=f"insufficient stock for product {item.product_id}",
                    )
                reserved.append((item.product_id, item.quantity))

            items_with_price = []
            order_items = []
            for item in payload.items:
                price = float(products_by_id[item.product_id]["price"])
                order_item = OrderItem(
                    order_id=order.id, product_id=item.product_id, quantity=item.quantity, price_at_order=price
                )
                session.add(order_item)
                order_items.append(order_item)
                items_with_price.append((price, item.quantity))

            order.total_amount = calculate_total(items_with_price, prior_orders or 0)
            item_totals = calculate_item_totals(items_with_price, prior_orders or 0)
            if abs(sum(item_totals) - order.total_amount) > 0.001:
                raise ValueError(
                    f"order total does not match sum of item totals: "
                    f"{order.total_amount:.2f} != {sum(item_totals):.2f}"
                )
            for order_item, item_total in zip(order_items, item_totals):
                order_item.total = item_total
            order.status = OrderStatus.confirmed
            await session.commit()
        except Exception as exc:
            for product_id, quantity in reserved:
                await catalog_client.release_stock(client, product_id, quantity, order.id)
            await session.rollback()
            if isinstance(exc, catalog_client.CatalogUnavailable):
                raise HTTPException(status_code=503, detail="catalog service unavailable") from exc
            raise

        await session.refresh(order, attribute_names=["items", "created_at"])

    logger.info("order created", extra={"order_id": order.id, "total_amount": float(order.total_amount)})
    await publish_order_event(
        "order.created",
        order.id,
        {"user_email": payload.user_email, "total_amount": float(order.total_amount)},
    )
    return _out(order, payload.user_email)
