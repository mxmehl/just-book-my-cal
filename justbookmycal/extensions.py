# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Shared Flask extension instances for just-book-my-cal."""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

# Initialised without an app here; init_app() is called in create_app().
db = SQLAlchemy(
    session_options={"expire_on_commit": False}
)  # no reloads after commit: mails follow
limiter = Limiter(key_func=get_remote_address)


@event.listens_for(Engine, "connect")
def _sqlite_connect(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    """Stop pysqlite from managing transactions itself, so that `_sqlite_begin` can."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        dbapi_connection.isolation_level = None


@event.listens_for(Engine, "begin")
def _sqlite_begin(conn: Connection) -> None:
    """Take the SQLite write lock when a transaction starts.

    This makes "check that the slot is free, then insert" atomic across gunicorn workers.
    ponytail: also serialises plain reads, which is fine for one person's booking page. Upgrade
    path: BEGIN IMMEDIATE only in the booking routes, or a real database.
    """
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("BEGIN IMMEDIATE")
