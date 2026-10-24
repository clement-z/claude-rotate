"""Single-process uvicorn runner with systemd readiness and loop liveness.

Uses only the standard library plus the application's existing uvicorn dependency.
No config or application is imported until main(), so this module is inspectable
without reading credentials. Access logging stays disabled: URLs may hold keys.
"""

import asyncio
import os
from pathlib import Path
import socket
import sys

import uvicorn


class SystemdNotifier:
    def __init__(self):
        address = os.environ.get("NOTIFY_SOCKET")
        self.address = ("\0" + address[1:]) if address and address.startswith("@") else address

    def notify(self, message):
        if self.address is None:
            return
        # Each small datagram has its own socket. Never block the ASGI loop on a
        # full notification queue; failure exits and lets systemd restart us.
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(message.encode("utf-8"), self.address)

    def watchdog_interval(self):
        if not self.address:
            return None
        usec = int(os.environ.get("WATCHDOG_USEC", "0"))
        watchpid = os.environ.get("WATCHDOG_PID")
        if usec <= 0 or (watchpid is not None and int(watchpid) != os.getpid()):
            return None
        return usec / 3_000_000  # Three opportunities per watchdog period.


class SystemdServer(uvicorn.Server):
    def __init__(self, config, notifier):
        super().__init__(config)
        self.notifier = notifier
        self.watchdog_interval = None
        self.next_watchdog = 0.0

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started and not self.should_exit:
            self.watchdog_interval = self.notifier.watchdog_interval()
            self.notifier.notify("READY=1")

    async def on_tick(self, counter):
        # uvicorn calls this from its own main loop. A blocked ASGI loop stops
        # these notifications too; there is no thread or extra task to outlive it.
        should_exit = await super().on_tick(counter)
        if should_exit:
            return True
        now = asyncio.get_running_loop().time()
        if self.watchdog_interval is not None and now >= self.next_watchdog:
            self.notifier.notify("WATCHDOG=1")
            self.next_watchdog = now + self.watchdog_interval
        return False

    async def shutdown(self, sockets=None):
        self.notifier.notify("STOPPING=1")
        self.watchdog_interval = None
        await super().shutdown(sockets=sockets)


async def run():
    # An absolute script path puts deploy/, not the application root, on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from rotator import CFG, app

    config = uvicorn.Config(
        app,
        host=CFG.get("bind", "127.0.0.1"),
        port=CFG.get("port", 8484),
        loop="asyncio",
        http="h11",
        workers=1,
        limit_concurrency=32,
        access_log=False,
        log_level="warning",
        timeout_graceful_shutdown=30,
    )
    server = SystemdServer(config, SystemdNotifier())
    await server.serve()


if __name__ == "__main__":
    asyncio.run(run())
