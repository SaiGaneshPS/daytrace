"""DT-10: hub profiles (personal / shared-dev / demo), ports, data paths and who may connect.

Each profile has its own port and its own database, so real activity never mixes with shared or demo data.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Single source of truth for profile names (the CLI and scripts validate against this).
PROFILES: tuple[str, ...] = ("personal", "shared-dev", "demo")

# Networks a hub may be reached from. Everything else (the public internet) is always refused.
LOOPBACK = ("127.0.0.0/8", "::1/128")
PRIVATE_LAN = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10")
# Tailscale hands out addresses from these ranges; only profiles meant for your teammate accept them.
TAILSCALE = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")


@dataclass(frozen=True)
class Profile:
    """How one hub profile listens and who it accepts requests from."""

    name: str
    port: int
    allow_tailscale: bool
    description: str
    # 0.0.0.0 so phones on the same Wi-Fi can connect even when the router hands out a new address;
    # which clients are actually accepted is decided by client_allowed().
    host: str = "0.0.0.0"

    @property
    def database_filename(self) -> str:
        return f"daytrace-{self.name}.db"


PROFILE_SETTINGS: dict[str, Profile] = {
    "personal": Profile(
        name="personal",
        port=8765,
        allow_tailscale=False,
        description="Your real data. Reachable from your home Wi-Fi (your phone syncs here), never over Tailscale.",
    ),
    "shared-dev": Profile(
        name="shared-dev",
        port=8766,
        allow_tailscale=True,
        description="Seed and test data you share with your teammate over Tailscale.",
    ),
    "demo": Profile(
        name="demo",
        port=8767,
        allow_tailscale=False,
        description="Seeded demo data for the hackathon demo, reachable on the local network.",
    ),
}

if tuple(PROFILE_SETTINGS) != PROFILES:
    raise RuntimeError("PROFILE_SETTINGS must list every profile in PROFILES, in the same order")


def get_profile(name: str) -> Profile:
    try:
        return PROFILE_SETTINGS[name]
    except KeyError:
        raise ValueError(f"unknown profile {name!r}; choose one of {', '.join(PROFILES)}") from None


def _networks(*groups: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(cidr) for group in groups for cidr in group)


_ALWAYS_ALLOWED = _networks(LOOPBACK, PRIVATE_LAN)
_TAILSCALE_NETWORKS = _networks(TAILSCALE)


def client_allowed(host: str | None, profile: Profile) -> bool:
    """True when a request from this client address may reach the profile.

    Loopback and private LAN addresses are always allowed; Tailscale addresses only for profiles with
    allow_tailscale; anything else (public addresses, unparseable hosts) is refused.
    """
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone such as fe80::1%eth0
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in _TAILSCALE_NETWORKS):
        return profile.allow_tailscale
    return any(address in network for network in _ALWAYS_ALLOWED)


def default_data_dir(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Where hub databases live when DAYTRACE_DATA_DIR is not set: the usual per-user data folder."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Daytrace"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Daytrace"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "daytrace"


@dataclass(frozen=True)
class Settings:
    """Everything the hub needs to start one profile."""

    profile: Profile
    data_dir: Path

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.profile.database_filename


def load_settings(profile_name: str = "personal", env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    configured = env.get("DAYTRACE_DATA_DIR", "").strip()
    data_dir = Path(configured).expanduser() if configured else default_data_dir(env)
    return Settings(profile=get_profile(profile_name), data_dir=data_dir)
