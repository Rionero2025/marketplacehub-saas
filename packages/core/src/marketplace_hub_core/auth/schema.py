from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Uuid,
)

metadata = MetaData()

auth_users = Table(
    "auth_users",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("login", String(254), nullable=False, unique=True),
    Column("display_name", String(160), nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

auth_user_realms = Table(
    "auth_user_realms",
    metadata,
    Column(
        "user_id",
        Uuid(as_uuid=True),
        ForeignKey("auth_users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("realm", String(20), primary_key=True),
    CheckConstraint("realm IN ('seller', 'agency', 'platform')", name="ck_auth_user_realms_realm"),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column(
        "user_id",
        Uuid(as_uuid=True),
        ForeignKey("auth_users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("realm", String(20), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("realm IN ('seller', 'agency', 'platform')", name="ck_auth_sessions_realm"),
)

Index("ix_auth_sessions_user_id", auth_sessions.c.user_id)
Index("ix_auth_sessions_expires_at", auth_sessions.c.expires_at)
