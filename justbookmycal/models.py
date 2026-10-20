# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Database models."""

import base64
import hashlib
import hmac
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


def derive_token(secret_key: str, uid: str, kind: str) -> str:
    """Derive a private link token for a booking from the app secret.

    The token is not stored; the same booking always yields the same token, so every mail can
    carry the links again (also after the other side changed the meeting). Without SECRET_KEY, the
    database alone does not reveal them. If SECRET_KEY changes, links already sent keep working
    (they are looked up by their stored hash), but the links derived afterwards no longer match.

    Args:
        secret_key: the app's SECRET_KEY
        uid: UID of the booking (random)
        kind: "manage" (booker) or "admin" (owner): the two links of a booking differ
    """
    message = f"justbookmycal-link:{kind}:{uid}".encode()
    digest = hmac.new(secret_key.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def hash_token(token: str) -> str:
    """Hash a token for lookup. Only the hash is stored in the database, never the token."""
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
    # The video link of this meeting: rendered from MEETING_URL when booked, editable by the owner
    meeting_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_CONFIRMED)
    booker_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    admin_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Slug of the profile it was booked under; NULL for bookings from before profiles existed
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
