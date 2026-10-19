<!--
  SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
  SPDX-License-Identifier: CC-BY-4.0
-->

# just-book-my-cal

[![Test suites](https://github.com/mxmehl/just-book-my-cal/actions/workflows/test.yaml/badge.svg)](https://github.com/mxmehl/just-book-my-cal/actions/workflows/test.yaml)
[![REUSE status](https://api.reuse.software/badge/github.com/mxmehl/just-book-my-cal)](https://api.reuse.software/info/github.com/mxmehl/just-book-my-cal)
[![The latest version can be found on PyPI.](https://img.shields.io/pypi/v/justbookmycal.svg)](https://pypi.org/project/justbookmycal/)
[![Information on what versions of Python are supported can be found on PyPI.](https://img.shields.io/pypi/pyversions/justbookmycal.svg)](https://pypi.org/project/justbookmycal/)

A small, self-hosted alternative to cal.com for one person: others book a meeting with you in the free times of your calendar. No accounts, no admin interface, no external services.

- Reads your busy times from one or more public ICS feeds, including recurring events and moved or cancelled instances.
- Offers only slots that fit your rules: working hours per weekday (with breaks), the meeting lengths you choose (e.g. 15/30/60 minutes), minimum notice, booking horizon and buffers before and after your meetings.
- Books instantly. You and the booker both get a calendar invitation (ICS) by email, with a video link for each meeting (a room of its own, or a fixed address) and an optional reminder.
- Both sides get a private link to reschedule or cancel. The booker can only move the meeting into slots that are free under your rules. You can move it anywhere, with a warning if that overlaps with something.
- Visitors see the times in their own time zone (a few lines of JavaScript; without them, the times are shown in your time zone). Free times are listed one calendar week at a time, with arrows to the previous and next week.
- Abuse protection: rate limiting, a honeypot field, CSRF protection and a cap on upcoming meetings per email address.

The only thing it stores is the bookings, in SQLite. Everything else is in one YAML file.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```sh
uv sync --no-dev
cp config.example.yaml config.yaml    # then edit it, at least the keys marked REQUIRED
uv run justbookmycal-cli --db upgrade # creates the database; run again after updates
uv run justbookmycal-cli --debug      # development server on http://127.0.0.1:5262
```

Before you publish the page, check that the app sees your calendar the way you expect:

```sh
export FLASK_APP='justbookmycal.wsgi:main()' CONFIG_FILE=config.yaml
uv run flask busy --days 7        # busy blocks found in your feeds
uv run flask slots --duration 30  # slots people could book
```

## Configuration

All settings are explained in [config.example.yaml](config.example.yaml), and validated against [a JSON schema](justbookmycal/config_schema.json) on start. Only five keys are required: `SECRET_KEY`, `OWNER_NAME`, `OWNER_EMAIL`, `SYSTEM_EMAIL` and `ICS_FEEDS`.

How your feed is read:

- Events with `TRANSP:TRANSPARENT` ("free" in most calendar apps) and `STATUS:CANCELLED` never block time.
- Everything else blocks, including all-day events. A missing `TRANSP` counts as busy, as the standard says. If birthdays or holidays in your feed block whole days, mark them as free or move them to another calendar.
- Feeds are cached for `FEED_CACHE_SECONDS`. If a feed cannot be loaded, the last copy (up to 24 hours old) is used. If there is none, the page says that the calendar is unavailable and takes no bookings, rather than risking a double booking.
- Bookings made through the app block their time immediately, even if your calendar app has not published the invitation in the feed yet.

Texts: `EVENT_TITLE`, `EVENT_DESCRIPTION`, `EMAIL_BODY_BOOKER` and `EMAIL_BODY_OWNER` are [Jinja2](https://jinja.palletsprojects.com/) templates, so you can use the names of the participants, the message of the booker and conditions like `{% if booker_message %}`. The available variables are listed in the example configuration.

## The invitation

The system address (`SYSTEM_EMAIL`, e.g. `noreply@example.com`) sends the mails and is the ORGANIZER of the event. Its display name is `SYSTEM_NAME` (default: your name). You and the booker are both ATTENDEEs. This is deliberate: an invitation that arrives in the mailbox of its own organizer is not handled as a normal invitation by Outlook, so with the system address as organizer both of you get an ordinary invitation, and updates and cancellations are recognised as such. A rescheduled meeting keeps its UID and gets a higher SEQUENCE, a cancellation is sent as `METHOD:CANCEL`.

Each mail contains a plain text part, the invitation as `text/calendar; method=…` (for the Accept/Decline buttons of mail clients) and the same ICS as an attachment. Times are sent in UTC. Each side receives its own private link in every mail (the first one and all updates and cancellations), never the link of the other side. The links are not stored: they are derived from `SECRET_KEY` and the booking, and only their SHA-256 hashes are in the database. Changing `SECRET_KEY` later breaks the links in mails sent afterwards for bookings that already exist (links sent before keep working), so avoid changing it while there are upcoming meetings.

If the mail server rejects the mails, the booking is kept and the booker is told. Because the owner's admin link would be lost, it is written to the log in this case.

Each meeting gets a video link rendered from the `MEETING_URL` template. The default creates a room like `https://meet.jit.si/MaxMehl-JaneDoe-20261020-k7p2x`: the random suffix makes it hard to guess, but the room name contains both names, so the operator of the video server can read them. Use your own Jitsi instance in the template if that matters to you, or a fixed address without variables to use one permanent room. You can change the link of a single meeting on its admin page, for example if the room needs to be different; the update is sent to both of you like a reschedule.

## Deployment

Run the app with gunicorn behind a reverse proxy that terminates TLS:

```sh
uv run justbookmycal --app-config /path/to/config.yaml   # listens on 0.0.0.0:5262
```

The bundled gunicorn configuration uses a single worker on purpose: the rate limiter and the feed cache live in memory of the process. Pass your own with `--gunicorn-config` if you need something else.

By default, gunicorn listens on all interfaces (`0.0.0.0:5262`), so that a reverse proxy on the same machine, in another container or on another host can reach it. Only do this where the port is not reachable from the internet except through the proxy: the app has no login, and it trusts the `X-Forwarded-For` header of the proxy (see `TRUSTED_PROXIES` below), so visitors who reach the port directly could fake their IP and escape the rate limit. If the proxy runs on the same machine, restrict the app to localhost with `--bind`:

```sh
uv run justbookmycal --app-config /path/to/config.yaml --bind 127.0.0.1:5262
```

An nginx location for it:

```nginx
location / {
    proxy_pass http://127.0.0.1:5262;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

`TRUSTED_PROXIES` (default `1`) tells the app how many proxies to believe when determining the client IP, which the rate limit depends on. Set `0` only if the app is reachable directly. With too high a value, visitors can fake their IP and escape the rate limit. Use a systemd service or a container to keep it running, and create the database with `--db upgrade` after every update.

## Security notes

- The booking page is public and has no login, by design. It exposes your free and busy pattern (not the content of your events), and anyone can use it to put a meeting into your calendar, within the limits set by the rate limit, the per-email cap and your rules.
- The manage and admin links are capabilities: whoever has the link can change or cancel the meeting. They must not be forwarded. The pages are not cached and send no `Referer`. Only a SHA-256 hash of each link is stored.
- The database contains names, email addresses and messages of the people who booked. Keep the file out of any web root, restrict its permissions and treat backups as sensitive.
- The mail text is built from your templates and the input of visitors. Names are restricted to characters that are safe in mail headers and ICS files.
- Anyone can enter any email address when booking. The invitation goes to that address, so the app can be used to send mails to third parties (one invitation per booking, limited by the rate limit and the cap). If this worries you, lower `RATE_LIMIT_POST`.

## Known limitations

- Your own copy of a booking can block neighbouring times. When you accept the invitation in your calendar app and that app publishes it in your feed, the event is counted twice (once from the database, once from the feed). This is harmless for new bookings. When a booker moves a meeting, the feed's copy of the old time keeps blocking until the feed shows the change. Calendar apps that keep the invitation's UID (e.g. Thunderbird, Nextcloud) are ignored correctly during rescheduling. Exchange/Outlook assigns its own UID, so the old copy stays in the way until it disappears from your feed.
- Only one organiser, one language (English) and one set of rules.
- Calendar clients differ in how they show invitations. The mails follow the common conventions for Outlook and Thunderbird, but check that they look right in your setup before you rely on them.

## Development

```sh
uv sync                  # with dev tools
mise run test-all        # ruff, ty, reuse and pytest
mise run fix-all         # auto-fix formatting and lint issues
```

See [AGENTS.md](AGENTS.md) for the layout of the code. Please use [conventional commits](https://www.conventionalcommits.org/). Releases are created by release-please.

## License

Apache-2.0 for the code, CC0-1.0 for configuration files, CC-BY-4.0 for documentation. See the [REUSE](https://reuse.software/) headers and the `LICENSES` directory.
