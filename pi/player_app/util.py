"""Small helpers shared across main.py's orchestration loops and the
background services/*_service.py workers."""
from __future__ import annotations

import asyncio
import socket
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

_THERMAL_ZONE_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
# vcgencmd get_throttled's bitmask - see Raspberry Pi's own documentation.
# Only the two "right now" bits are surfaced (not e.g. bit 1/frequency-
# capped, or the "has happened since boot" bits 16-19) - under-voltage and
# active throttling are the two that actually mean "something's wrong with
# this device at this moment," which is what a live status display needs.
_UNDER_VOLTAGE_NOW_BIT = 0x1
_THROTTLED_NOW_BIT = 0x4

# A raw TCP connect to a public DNS resolver's HTTPS port answers "can this
# device actually reach the internet" without depending on DNS working, and
# without sending anything to a real service. Cached so that repeated status
# polls from the iOS app don't each open a fresh connection.
_INTERNET_CHECK_HOST = "1.1.1.1"
_INTERNET_CHECK_PORT = 443
_INTERNET_CHECK_TIMEOUT_SECONDS = 2.0
_INTERNET_CHECK_CACHE_SECONDS = 30.0
_internet_cache: Optional[Tuple[float, bool]] = None

# vcgencmd is a subprocess spawn: cache it so status polls (several phones,
# the settings panel) never cost a spawn each - important while a movie plays.
_THROTTLE_CACHE_SECONDS = 30.0
_throttle_cache: Optional[Tuple[float, Optional["ThrottleStatus"]]] = None


async def sleep_unless_stopped(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleeps for `seconds`, waking early if `stop_event` is set. Shared by
    every "retry/refresh on an interval, but stop immediately if asked to"
    loop, so each one doesn't need its own try/except TimeoutError around
    asyncio.wait_for."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def host_resolves(url: str, timeout: float = 3.0) -> bool:
    """Whether the host in `url` currently resolves in DNS. The home server
    (magicboxie.local, an mDNS name) only exists on the home network, so this is a cheap
    "are we on that network" test that needs no SSID or interface knowledge
    and also works over Ethernet. False on any failure or timeout."""
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    return True


def local_ip() -> str:
    """Best-effort LAN IP: opens a UDP "connection" (no packets sent) to a
    public address just to see which local interface routing would use.
    Lives here (not main.py) so web_service.py can report it too (see
    GET /api/version) without an import cycle back into main.py."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def cpu_temperature_celsius() -> Optional[float]:
    """Reads the SoC's own thermal sensor - the same value `vcgencmd
    measure_temp` reports, but as a plain world-readable sysfs file rather
    than a subprocess call that needs the `video` group. Millidegrees C as
    a plain integer (e.g. "48312" -> 48.312). None if the file isn't there
    at all (e.g. running in the Docker dev container, which has no real
    thermal zone) or its contents aren't parseable - a missing sensor
    reading shouldn't take the whole status endpoint down with it."""
    try:
        return int(_THERMAL_ZONE_PATH.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


@dataclass
class ThrottleStatus:
    under_voltage: bool
    throttled: bool


async def get_throttle_status() -> Optional[ThrottleStatus]:
    """Cached for _THROTTLE_CACHE_SECONDS - see _read_throttle_status."""
    global _throttle_cache
    now = time.monotonic()
    if _throttle_cache is not None and now - _throttle_cache[0] < _THROTTLE_CACHE_SECONDS:
        return _throttle_cache[1]
    status = await _read_throttle_status()
    _throttle_cache = (now, status)
    return status


async def _read_throttle_status() -> Optional[ThrottleStatus]:
    """Runs `vcgencmd get_throttled` - the project's own prior suspicion
    (a Pi Zero W repeatedly going unreachable under load) was an
    undervoltage/power-supply issue, and this is the actual, authoritative
    way to confirm that rather than guess from symptoms. None if vcgencmd
    isn't available at all (not real Pi hardware, e.g. the Docker dev
    container) or its output isn't in the expected "throttled=0x..."
    form - same graceful-degradation contract as cpu_temperature_celsius."""
    try:
        process = await asyncio.create_subprocess_exec(
            "vcgencmd", "get_throttled",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None

    text = stdout.decode(errors="replace").strip()
    if not text.startswith("throttled=0x"):
        return None
    try:
        value = int(text.removeprefix("throttled="), 16)
    except ValueError:
        return None

    return ThrottleStatus(
        under_voltage=bool(value & _UNDER_VOLTAGE_NOW_BIT),
        throttled=bool(value & _THROTTLED_NOW_BIT),
    )


async def internet_reachable() -> bool:
    """Whether this device currently has an internet route, checked with a
    short TCP connect (see _INTERNET_CHECK_HOST). Result is cached for
    _INTERNET_CHECK_CACHE_SECONDS. A failed or timed-out connect counts as
    offline - the status display shows what it can tell, and a stale
    "online" reading on a car deployment would be the more misleading error."""
    global _internet_cache
    now = time.monotonic()
    if _internet_cache is not None and now - _internet_cache[0] < _INTERNET_CHECK_CACHE_SECONDS:
        return _internet_cache[1]

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(_INTERNET_CHECK_HOST, _INTERNET_CHECK_PORT),
            timeout=_INTERNET_CHECK_TIMEOUT_SECONDS,
        )
        writer.close()
        reachable = True
    except (OSError, asyncio.TimeoutError):
        reachable = False

    _internet_cache = (now, reachable)
    return reachable
