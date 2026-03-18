"""admin users auth

Revision ID: 0007_admin_users
Revises: 0006_system_settings
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TEXT


revision = "0007_admin_users"
down_revision = "0006_system_settings"
branch_labels = None
depends_on = None


def _hash_pbkdf2_sha256(password: str, *, salt: str, iterations: int) -> str:
  import hashlib

  salt_bytes = salt.encode("utf-8")
  dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
  return dk.hex()


def upgrade() -> None:
  op.create_table(
    "admin_users",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("email", sa.String(length=255), nullable=False),
    sa.Column("password_hash", TEXT(), nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
  )
  op.create_index("ix_admin_users_email", "admin_users", ["email"], unique=True)

  # Seed initial admin user.
  # Password is provided by the user request: Qwekoil123
  iterations = 250_000
  salt = "jobeco_default_admin_salt"
  pw_hash = _hash_pbkdf2_sha256("Qwekoil123", salt=salt, iterations=iterations)
  formatted = f"pbkdf2_sha256${iterations}${salt.encode('utf-8').hex()}${pw_hash}"
  op.execute(
    "INSERT INTO admin_users (email, password_hash, is_active, created_at) "
    f"VALUES ('vladstit@gmail.com', '{formatted}', true, now()) "
    "ON CONFLICT (email) DO NOTHING;"
  )


def downgrade() -> None:
  op.drop_table("admin_users")

