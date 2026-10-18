"""Minimal systemd readiness notification (``sd_notify``), standard library only.

With ``Type=notify`` systemd waits for the service to send ``READY=1`` before it considers the
service started, so ``systemctl start/restart`` returns non-zero if the process dies (or never
reports ready) during startup. Outside systemd (``$NOTIFY_SOCKET`` unset, e.g. running in the
foreground or on Windows) every call is a no-op.

Protocol: https://www.freedesktop.org/software/systemd/man/sd_notify.html
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def notify(*states: str) -> bool:
    """Send ``KEY=value`` lines to systemd. Returns True if a message was sent."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):  # abstract namespace socket
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall("\n".join(states).encode("utf-8"))
    except OSError as exc:
        log.warning("could not notify systemd via %s: %s", os.environ.get("NOTIFY_SOCKET"), exc)
        return False
    return True
