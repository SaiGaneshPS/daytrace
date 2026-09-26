"""DT-12: mDNS advertisement (_daytrace._tcp and daytrace-hub.local), and the addresses phones can use.

Phones find the hub with service discovery (Android NsdManager, `dns-sd -B _daytrace._tcp` on a Mac) and
pair with the URL in the QR code, which uses the PC's LAN IP: Android browsers do not reliably resolve
.local names. mDNS never crosses Tailscale, so only LAN addresses are advertised.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable, Iterable
from typing import Any

import psutil
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .config import TAILSCALE, Settings

SERVICE_TYPE = "_daytrace._tcp.local."
_TAILSCALE = tuple(ipaddress.ip_network(cidr) for cidr in TAILSCALE)
logger = logging.getLogger("daytrace_hub")


def primary_ipv4() -> str | None:
    """The address of the interface that holds the default route. No packet is sent (UDP connect only)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: only used to ask the OS which interface it would use
            return str(probe.getsockname()[0])
        except OSError:
            return None


def interface_ipv4s() -> list[str]:
    return [
        address.address
        for addresses in psutil.net_if_addrs().values()
        for address in addresses
        if address.family == socket.AF_INET
    ]


def phone_addresses(settings: Settings, primary: str | None, candidates: Iterable[str]) -> list[str]:
    """IPv4 addresses a phone can reach this hub on: LAN first (the default-route one first), then Tailscale
    addresses on profiles that accept Tailscale. Loopback, link-local and public addresses are left out."""
    lan: list[str] = []
    tailscale: list[str] = []
    ordered = [primary, *candidates] if primary else list(candidates)
    for text in ordered:
        try:
            address = ipaddress.IPv4Address(text)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        if any(address in network for network in _TAILSCALE):
            if settings.profile.allow_tailscale and text not in tailscale:
                tailscale.append(text)
        elif any(address in network for network in settings.lan_networks) and text not in lan:
            lan.append(text)
    return [*lan, *tailscale]


def detect_phone_addresses(settings: Settings) -> list[str]:
    return phone_addresses(settings, primary_ipv4(), interface_ipv4s())


def is_tailscale(address: str) -> bool:
    return any(ipaddress.IPv4Address(address) in network for network in _TAILSCALE)


def hub_url(settings: Settings, host: str) -> str:
    return f"http://{host}:{settings.profile.port}"


def mdns_url(settings: Settings) -> str:
    return hub_url(settings, f"{settings.mdns_name}.local")


def service_info(settings: Settings, addresses: list[str]) -> ServiceInfo:
    profile = settings.profile
    return ServiceInfo(
        SERVICE_TYPE,
        f"Daytrace hub ({profile.name}).{SERVICE_TYPE}",
        port=profile.port,
        properties={"profile": profile.name, "version": __version__, "api": "/api/v1"},
        server=f"{settings.mdns_name}.local.",
        addresses=[socket.inet_aton(address) for address in addresses],
    )


class Advertiser:
    """Announces the running profile on the local network while the hub runs. Never stops the hub from starting."""

    def __init__(self, settings: Settings, zeroconf_factory: Callable[..., Any] = AsyncZeroconf) -> None:
        self.settings = settings
        self._factory = zeroconf_factory
        self._zeroconf: Any = None
        self.info: ServiceInfo | None = None

    async def start(self, addresses: list[str] | None = None) -> bool:
        found = detect_phone_addresses(self.settings) if addresses is None else addresses
        lan = [address for address in found if not is_tailscale(address)]
        if not lan:
            logger.warning("mDNS: no LAN address found, phones must use the QR code or type the hub address")
            return False
        self.info = service_info(self.settings, lan)
        try:
            self._zeroconf = self._factory(ip_version=IPVersion.V4Only)
            await self._zeroconf.async_register_service(self.info, allow_name_change=True)
        except Exception:
            logger.warning("mDNS advertisement failed; pairing by QR code still works", exc_info=True)
            await self.stop()
            return False
        logger.info("mDNS: advertising %s on %s", self.info.name, ", ".join(lan))
        return True

    async def stop(self) -> None:
        zeroconf, self._zeroconf = self._zeroconf, None
        if zeroconf is None:
            return
        try:
            await zeroconf.async_unregister_all_services()
        except Exception:
            logger.debug("mDNS: unregister failed", exc_info=True)
        finally:
            await zeroconf.async_close()
