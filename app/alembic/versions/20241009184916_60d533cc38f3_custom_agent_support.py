"""custom agent support

Revision ID: 20241009184916_60d533cc38f3
Revises: 20240927094023_fb0b353e69d0
Create Date: 2024-10-09 18:49:16.617442

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20241009184916_60d533cc38f3"
down_revision: Union[str, None] = "20240927094023_fb0b353e69d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "custom_agents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("goal", sa.String(), nullable=True),
        sa.Column("backstory", sa.String(), nullable=True),
        sa.Column("tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("deployment_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.uid"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_custom_agents_id"), "custom_agents", ["id"], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_custom_agents_id"), table_name="custom_agents")
    op.drop_table("custom_agents")
    # ### end Alembic commands ###
