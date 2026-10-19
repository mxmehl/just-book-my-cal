# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Gunicorn configuration file."""

# Server socket. Binds to localhost: put a reverse proxy with TLS in front of it
bind = "127.0.0.1:5262"

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
