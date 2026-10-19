<!--
  SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
  SPDX-License-Identifier: CC-BY-4.0
-->

# just-book-my-cal — Agent Instructions

Single-user booking page (a minimal self-hosted cal.com alternative): reads busy times from public ICS feeds, offers free slots under configured rules, books them in SQLite and sends ICS invitations. See [README.md](README.md).

- **Stack:** Python 3.10–3.14, Flask, SQLAlchemy (SQLite), icalendar, recurring-ical-events, Jinja2
- **Package manager:** uv — always use `uv run` / `uv sync`; never `pip` directly
- **Entry points** (do not rename): `justbookmycal-cli` (development server, DB commands), `justbookmycal` (gunicorn/WSGI)

## Quick Commands

```sh
uv sync                                   # install / sync deps
mise run test-all                         # all checks at once (ruff, ty, reuse, pytest)
mise run fix-all                          # auto-fix lint/format issues
uv run justbookmycal-cli -c config.yaml --db upgrade  # create or migrate the database
uv run justbookmycal-cli -c config.yaml --debug       # run the dev server (copy config.example.yaml to config.yaml first)
```

## Architecture

| Layer                          | Key file(s)                      |
| ------------------------------ | -------------------------------- |
| App factory & CLI              | `justbookmycal/app.py`           |
| WSGI / gunicorn                | `justbookmycal/wsgi.py`          |
| Config loading & schema        | `justbookmycal/config.py`, `config_schema.json` |
| Feeds, busy times, slot engine | `justbookmycal/availability.py`  |
| ICS, mails, Jinja2 texts       | `justbookmycal/invite.py`        |
| Routes (public, booker, owner) | `justbookmycal/views.py`         |
| Booking model                  | `justbookmycal/models.py`        |
| Extensions, SQLite locking     | `justbookmycal/extensions.py`    |
| Tests (in-memory SQLite)       | `tests/`                         |

Flow: `GET /` → `availability.free_slots()` (feeds + DB bookings → `compute_slots()`, a pure function) → `POST /book` re-checks the slot inside one DB transaction, inserts, commits, then `invite.send_invites()` → redirect to `/manage/<token>`. `/manage/<token>` (booker) and `/admin/<token>` (owner) reschedule or cancel; every change commits first and then sends mails, so a mail failure never loses a change.

## Conventions

- **Docstrings:** Required on all functions/classes in `justbookmycal/` — **Google style**, enforced by ruff. Tests are exempt.
- **Type annotations:** Required; checked by `ty`. Scope: `justbookmycal/` (migrations excluded).
- **Line length:** 100 characters.
- **REUSE:** Every new file needs an SPDX header (or a `REUSE.toml` annotation). Run `uv run reuse lint` to verify.
- **Config:** A new setting needs an entry in `config_schema.json`, a default in `config.py`, a documented example in `config.example.yaml`, and a mention in the README if users must know about it.
- **DB migrations:** Any schema change requires a new Alembic migration (`uv run justbookmycal-cli -c config.yaml --db-migrate "message"`). Migrations must not import application code.
- **Commits:** Conventional commits (`feat:`, `fix:`, `docs:` …); release-please builds the changelog from them.
- **Conservative changes:** Prefer minimal, well-tested edits. Mark deliberate shortcuts with a `ponytail:` comment that names the ceiling and the upgrade path.

## Pitfalls

- **Naive vs. aware datetimes:** All datetimes in the app are timezone-aware. `Booking.start` is stored as naive UTC and converted by `UTCDateTime`. Never compare naive with aware values.
- **SQLite write lock:** `extensions.py` makes every SQLite transaction start with `BEGIN IMMEDIATE`, which is what makes "check slot free, then insert" safe. Do slow things (feed downloads, SMTP) outside of an open transaction: `views._prefetch_feeds` warms the feed cache before the first query, `expire_on_commit` is off so that sending mails after a commit does not open a new transaction.
- **Tests and the shared connection:** The in-memory test database uses one shared connection. Call `db.session.remove()` after querying in a test and before issuing requests (the `bookings()` helper does this), otherwise the next request fails with "cannot start a transaction within a transaction".
- **Flash messages in tests:** Follow the redirect after each POST so the flash is consumed; otherwise the next page shows stale messages.
- **Time in tests:** `availability.utcnow` and `availability._download` are replaced by the autouse `feed` fixture in `tests/conftest.py`; use `NOW` (Monday 2026-10-19 06:00 UTC) and `tests/fixtures/busy.ics`.
- **Links in mails:** The booker/admin tokens are derived from `SECRET_KEY` and the booking's UID (`models.derive_token`), so `views._links()` can rebuild both links at any time and every mail carries its recipient's link. Only hashes are stored, for lookup. Never put the other side's link into a mail.
- **Macros need context:** Templates import `macros.html` `with context`, otherwise `timezone_name` is empty.
- **mise:** In a fresh clone `mise` asks to trust `mise.toml` (`mise trust`) before `uv` shims work.

## Validation Checklist Before PR

```sh
uv sync
mise run test-all
```

All steps must pass. Fix failures; do not disable tests.
