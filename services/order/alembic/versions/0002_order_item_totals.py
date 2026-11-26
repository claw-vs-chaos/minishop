"""per-line totals for finance reporting

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26

Each order line stores its discounted total; the sum of lines must equal
orders.total_amount to the kopeck (finance invariant).
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_items",
        sa.Column("total", sa.Numeric(10, 2), nullable=True),
        schema="orders",
    )
    op.execute(
        "UPDATE orders.order_items SET total = round(price_at_order * quantity, 2) WHERE total IS NULL"
    )
    op.alter_column("order_items", "total", schema="orders", nullable=False)


def downgrade() -> None:
    op.drop_column("order_items", "total", schema="orders")
