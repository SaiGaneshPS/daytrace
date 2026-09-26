"""DT-12: mDNS advertisement (_daytrace._tcp and daytrace-<pc>.local), and the addresses phones can use.

Phones find the hub with service discovery (Android NsdManager, `dns-sd -B _daytrace._tcp` on a Mac) and
pair with the URL in the QR code, which uses the PC's LAN IP: Android browsers do not reliably resolve
.local names. mDNS never crosses Tailscale, so only LAN addresses are advertised.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from collections.abc import Callable, Iterable
from typing import Any

import psutil
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .config import TAILSCALE_NETWORKS, Settings, parse_ip

SERVICE_TYPE = "_daytrace._tcp.local."
REFRESH_SECONDS = 30.0  # how often the advertised addresses are checked (Wi-Fi changes, DHCP renewals)
# Adapters a phone can never reach: VMs, WSL, containers, VPN tunnels, Bluetooth.
VIRTUAL_ADAPTER = re.compile(
    r"vethernet|wsl|virtualbox|vbox|vmware|vmnet|hyper-v|docker|^br-|^veth|^virbr|^tun|^tap|wintun|wireguard"
    r"|^wg\d|^ppp|zerotier|^utun|vpn|^awdl|^llw|bluetooth",
    re.IGNORECASE,
)
logger = logging.getLogger("daytrace_hub")


def primary_ipv4() -> str | None:
    """The address of the interface that holds the default route. No packet is sent (UDP connect only)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: only used to ask the OS which interface it would use
            return str(probe.getsockname()[0])
        except OSError:
            return None


def interface_ipv4s() -> list[tuple[str, str]]:
    """(adapter name, IPv4 address) for every adapter that is up."""
    stats = psutil.net_if_stats()
    found: list[tuple[str, str]] = []
    for name, addresses in psutil.net_if_addrs().items():
        stat = stats.get(name)
        if stat is not None and not stat.isup:
            continue
        found.extend((name, address.address) for address in addresses if address.family == socket.AF_INET)
    return found


def is_tailscale(address: str) -> bool:
    parsed = parse_ip(address)
    return parsed is not None and any(parsed in network for network in TAILSCALE_NETWORKS)


def phone_addresses(settings: Settings, primary: str | None, candidates: Iterable[tuple[str, str]]) -> list[str]:
    """IPv4 addresses a phone can reach this hub on: LAN first (the default-route one first), then Tailscale
    addresses on profiles that accept Tailscale.

    Left out: loopback, link-local and public addresses, addresses outside DAYTRACE_LAN_NETWORKS, and
    adapters that look virtual (VMs, WSL, VPN tunnels), even when a VPN holds the default route.
    """
    adapters = list(candidates)
    adapter_of = {address: name for name, address in adapters}
    ordered = ([(adapter_of.get(primary, ""), primary)] if primary else []) + adapters
    lan: list[str] = []
    tailscale: list[str] = []
    for adapter, text in ordered:
        address = parse_ip(text)
        if address is None or address.version != 4:
            continue
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        if is_tailscale(text):
            if settings.profile.allow_tailscale and text not in tailscale:
                tailscale.append(text)
        elif VIRTUAL_ADAPTER.search(adapter):
            continue
        elif any(address in network for network in settings.lan_networks) and text not in lan:
            lan.append(text)
    return [*lan, *tailscale]


def detect_phone_addresses(settings: Settings) -> list[str]:
    return phone_addresses(settings, primary_ipv4(), interface_ipv4s())


def hub_url(settings: Settings, host: str) -> str:
    return f"http://{host}:{settings.profile.port}"


def mdns_url(settings: Settings) -> str:
    return hub_url(settings, f"{settings.mdns_name}.local")


def service_info(settings: Settings, addresses: list[str], name: str | None = None) -> ServiceInfo:
    profile = settings.profile
    return ServiceInfo(
        SERVICE_TYPE,
        name or f"Daytrace hub ({profile.name}).{SERVICE_TYPE}",
        port=profile.port,
        properties={"profile": profile.name, "version": __version__, "api": "/api/v1"},
        server=f"{settings.mdns_name}.local.",
        addresses=[socket.inet_aton(address) for address in addresses],
    )


class Advertiser:
    """Announces the running profile on the local network in the background while the hub runs.

    It never delays or stops the hub: registration runs as a task after startup, failures are logged and
    retried, and the addresses are checked every REFRESH_SECONDS so a new Wi-Fi or DHCP address (or Wi-Fi
    connecting after the hub started) is picked up without a restart.
    """

    def __init__(
        self,
        settings: Settings,
        zeroconf_factory: Callable[..., Any] = AsyncZeroconf,
        find_addresses: Callable[[], list[str]] | None = None,
        refresh_seconds: float = REFRESH_SECONDS,
    ) -> None:
        self.settings = settings
        self._factory = zeroconf_factory
        self._find = find_addresses or (lambda: detect_phone_addresses(settings))
        self.refresh_seconds = refresh_seconds
        self._zeroconf: Any = None
        self._task: asyncio.Task[None] | None = None
        self.info: ServiceInfo | None = None
        self.addresses: list[str] = []

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="daytrace-mdns")

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.warning("mDNS advertisement failed; pairing by QR code still works", exc_info=True)
            await asyncio.sleep(self.refresh_seconds)

    async def refresh(self) -> bool:
        """Announce the current LAN addresses, re-announcing when they changed. True while advertised."""
        lan = [address for address in await asyncio.to_thread(self._find) if not is_tailscale(address)]
        if self.info is not None and lan == self.addresses:
            return True
        if not lan:
            if self.info is not None:
                logger.info("mDNS: no LAN address any more, withdrawing the announcement")
                await self._close()
            return False
        try:
            if self._zeroconf is None:
                self._zeroconf = self._factory(ip_version=IPVersion.V4Only)
            if self.info is None:
                info = service_info(self.settings, lan)
                await self._zeroconf.async_register_service(info, allow_name_change=True)
            else:
                info = service_info(self.settings, lan, name=self.info.name)  # keep a name zeroconf changed
                await self._zeroconf.async_update_service(info)
        except Exception:
            await self._close()
            raise
        self.info, self.addresses = info, lan
        logger.info("mDNS: advertising %s on %s", info.name, ", ".join(lan))
        return True

    async def _close(self) -> None:
        zeroconf, self._zeroconf = self._zeroconf, None
        self.info, self.addresses = None, []
        if zeroconf is None:
            return
        try:
            await zeroconf.async_unregister_all_services()
        except Exception:
            logger.debug("mDNS: unregister failed", exc_info=True)
        try:
            await zeroconf.async_close()
        except Exception:
            logger.debug("mDNS: close failed", exc_info=True)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close()
