# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Gunicorn configuration file."""

# Server socket. All interfaces, so that a reverse proxy in another container or on another host can
# reach it (as in castmail2list). The app has no login: do not expose this port to the internet
# directly, only the proxy. For localhost only, start with `--bind 127.0.0.1:5262`
bind = "0.0.0.0:5262"

# Worker processes
workers = 1
worker_class = "sync"
timeout = 60  # slow calendar feeds and SMTP servers must not kill the worker
# Restart workers after handling this many requests to prevent memory leaks
max_requests = 500
max_requests_jitter = 50

# Logging
accesslog = "-"  # Log to stdout
errorlog = "-"  # Log to stderr
loglevel = "info"

# Process naming
proc_name = "justbookmycal"
