# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Database models."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import DateTime, Dialect, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from .extensions import db

STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes. Stored as naive UTC, because SQLite drops tzinfo."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:  # noqa: ARG002
        """Convert to naive UTC before storing."""
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "Naive datetime given, expected timezone-aware"
            raise ValueError(msg)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:  # noqa: ARG002
        """Attach UTC to what was read."""
        return None if value is None else value.replace(tzinfo=timezone.utc)


def new_token() -> str:
    """Create a URL-safe secret token (32 random bytes)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hash a token for storage and lookup. Only the hash is stored, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()


class Booking(db.Model):  # type: ignore[ty:unsupported-base]
    """A booked meeting. It is the only state the app keeps."""

    __tablename__ = "booking"
    __table_args__ = (Index("ix_booking_status_start", "status", "start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    duration: Mapped[int] = mapped_column(Integer, nullable=False)  # minutes
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    room: Mapped[str] = mapped_column(String(100), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_CONFIRMED)
    booker_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    admin_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Apply the defaults at construction, so that they also exist before the first flush."""
        kwargs.setdefault("message", "")
        kwargs.setdefault("sequence", 0)
        kwargs.setdefault("status", STATUS_CONFIRMED)
        super().__init__(**kwargs)

    @property
    def end(self) -> datetime:
        """End of the meeting (UTC)."""
        return self.start + timedelta(minutes=self.duration)

    @property
    def cancelled(self) -> bool:
        """Whether the booking was cancelled."""
        return self.status == STATUS_CANCELLED
