"""DT-10: hub profiles (personal / shared-dev / demo), ports, data paths and who may connect.

Each profile has its own port and its own database, so real activity never mixes with shared or demo data.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Single source of truth for profile names (the CLI and scripts validate against this).
PROFILES: tuple[str, ...] = ("personal", "shared-dev", "demo")

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Networks a hub may be reached from. Everything else (the public internet) is always refused.
LOOPBACK = ("127.0.0.0/8", "::1/128")
PRIVATE_LAN = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")
# Tailscale hands out addresses from these ranges; only profiles meant for your teammate accept them.
TAILSCALE = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")
# Name suffixes that public DNS never answers (mDNS and reserved private-use names), so they cannot be rebound.
PRIVATE_NAME_SUFFIXES = (".local", ".home.arpa", ".internal", ".lan", ".home", ".localdomain")


@dataclass(frozen=True)
class Profile:
    """How one hub profile listens and who it accepts requests from."""

    name: str
    port: int
    allow_tailscale: bool
    description: str
    # False for profiles that hold real data: the seed generator (DT-15) refuses them.
    seedable: bool = True
    # 0.0.0.0 (IPv4) so phones on the same Wi-Fi can connect even when the router hands out a new address;
    # which clients are actually accepted is decided by client_allowed() and host_allowed().
    host: str = "0.0.0.0"

    @property
    def database_filename(self) -> str:
        return f"daytrace-{self.name}.db"


PROFILE_SETTINGS: dict[str, Profile] = {
    "personal": Profile(
        name="personal",
        port=8765,
        allow_tailscale=False,
        description=(
            "Your real data. Reachable from your local network (your phone syncs here), never over Tailscale."
            " On Wi-Fi you do not trust, set DAYTRACE_LAN_NETWORKS or stop this profile."
        ),
        seedable=False,
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


def _networks(*groups: Iterable[str]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(cidr) for group in groups for cidr in group)


LOOPBACK_NETWORKS = _networks(LOOPBACK)
TAILSCALE_NETWORKS = _networks(TAILSCALE)
DEFAULT_LAN_NETWORKS = _networks(PRIVATE_LAN)


def parse_lan_networks(text: str) -> tuple[IPNetwork, ...]:
    """DAYTRACE_LAN_NETWORKS, e.g. "192.168.1.0/24, fd00:1::/64". Only private ranges are accepted."""
    networks: list[IPNetwork] = []
    for part in text.split(","):
        cidr = part.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ValueError(f"DAYTRACE_LAN_NETWORKS: {cidr!r} is not a network like 192.168.1.0/24") from None
        if not any(network.subnet_of(lan) for lan in DEFAULT_LAN_NETWORKS if lan.version == network.version):
            raise ValueError(f"DAYTRACE_LAN_NETWORKS: {cidr!r} is not a private LAN range")
        networks.append(network)
    if not networks:
        raise ValueError("DAYTRACE_LAN_NETWORKS is set but lists no networks")
    return tuple(networks)


def parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An IP address from a client or Host value, or None. IPv4-mapped IPv6 becomes plain IPv4."""
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 zone such as fe80::1%eth0
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def client_allowed(
    host: str | None, profile: Profile, lan_networks: tuple[IPNetwork, ...] = DEFAULT_LAN_NETWORKS
) -> bool:
    """True when a request from this client address may reach the profile.

    Loopback is always allowed, LAN addresses when they are in lan_networks (all private ranges unless
    DAYTRACE_LAN_NETWORKS narrows them), Tailscale addresses only for profiles with allow_tailscale.
    Anything else (public addresses, unparseable hosts) is refused.
    """
    address = parse_ip(host) if host else None
    if address is None:
        return False
    if any(address in network for network in TAILSCALE_NETWORKS):
        return profile.allow_tailscale
    return any(address in network for network in (*LOOPBACK_NETWORKS, *lan_networks))


def host_allowed(host_header: str | None, profile: Profile) -> bool:
    """Blocks DNS rebinding: a web page on evil.example must not be able to talk to the hub as a local client.

    Allowed Host values: IP addresses, single-label names (localhost, a PC name), names ending in one of
    PRIVATE_NAME_SUFFIXES (daytrace-<pc>.local, router names like pc.lan) and, on profiles that accept
    Tailscale, MagicDNS names (*.ts.net). Public DNS names can be pointed at any address by whoever owns
    them, so they are refused. A request without a Host header comes from a tool, not a browser, and is
    allowed.

    Someone on the same LAN can still answer a local name for a browser on the hub computer, so trusting a
    request as the hub computer itself takes more than this: see auth.is_trusted_local().
    """
    if not host_header:
        return True
    name = host_name(host_header)
    if not name:
        return False
    if parse_ip(name) is not None or "." not in name or name.endswith(PRIVATE_NAME_SUFFIXES):
        return True
    return profile.allow_tailscale and name.endswith(".ts.net")


def host_name(host_header: str) -> str:
    """The name part of a Host header or URL authority, lowercase, without port, brackets or final dot.

    Returns "" for values that do not parse (such as "[not-an-ip]:80").
    """
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        end = host.find("]")
        inner = host[1:end] if end > 0 else ""
        return inner if parse_ip(inner) is not None else ""
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name.rstrip(".")


def default_data_dir(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Where hub databases live when DAYTRACE_DATA_DIR is not set: the usual per-user data folder."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Daytrace"
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Daytrace"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "daytrace"


MDNS_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TRUE = ("1", "on", "true", "yes")
_FALSE = ("0", "off", "false", "no")


def default_mdns_name(computer_name: str) -> str:
    """daytrace-<pc name> as one DNS label, so two hubs on the same Wi-Fi never claim the same .local name."""
    label = re.sub(r"[^a-z0-9-]+", "-", computer_name.split(".", 1)[0].lower()).strip("-")
    name = f"daytrace-{label}"[:63].rstrip("-") if label else "daytrace-hub"
    return name if MDNS_NAME.fullmatch(name) else "daytrace-hub"


@dataclass(frozen=True)
class Settings:
    """Everything the hub needs to start one profile."""

    profile: Profile
    data_dir: Path
    lan_networks: tuple[IPNetwork, ...] = field(default=DEFAULT_LAN_NETWORKS)
    # Off unless load_settings() turns it on, so tests and scripts never announce anything on the network.
    advertise_mdns: bool = False
    mdns_name: str = "daytrace-hub"
    # Off unless load_settings() turns it on (DT-16), so tests never record this computer's screen.
    track_desktop: bool = False

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.profile.database_filename

    @property
    def tracker_lock_path(self) -> Path:
        return self.data_dir / f"{self.profile.database_filename}.tracker.lock"


def load_settings(profile_name: str = "personal", env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    configured = env.get("DAYTRACE_DATA_DIR", "").strip()
    if configured:
        data_dir = Path(os.path.expandvars(configured)).expanduser()
        if not data_dir.is_absolute():
            # A relative folder would move with the current directory and silently start an empty database.
            raise ValueError(f"DAYTRACE_DATA_DIR must be an absolute path, got {configured!r}")
    else:
        data_dir = default_data_dir(env)
    lan_text = env.get("DAYTRACE_LAN_NETWORKS", "").strip()
    lan_networks = parse_lan_networks(lan_text) if lan_text else DEFAULT_LAN_NETWORKS
    advertise_text = env.get("DAYTRACE_MDNS", "").strip().lower() or "on"
    if advertise_text not in (*_TRUE, *_FALSE):
        raise ValueError(f"DAYTRACE_MDNS must be on or off, got {advertise_text!r}")
    mdns_name = env.get("DAYTRACE_MDNS_NAME", "").strip().lower() or default_mdns_name(socket.gethostname())
    if not MDNS_NAME.fullmatch(mdns_name):
        raise ValueError(f"DAYTRACE_MDNS_NAME must be one DNS label like daytrace-hub, got {mdns_name!r}")
    advertise = advertise_text in _TRUE
    profile = get_profile(profile_name)
    # The desktop tracker records this computer: on by default only for your own data (personal).
    tracker_text = env.get("DAYTRACE_TRACKER", "").strip().lower() or ("on" if profile.name == "personal" else "off")
    if tracker_text not in (*_TRUE, *_FALSE):
        raise ValueError(f"DAYTRACE_TRACKER must be on or off, got {tracker_text!r}")
    return Settings(
        profile=profile,
        data_dir=data_dir,
        lan_networks=lan_networks,
        advertise_mdns=advertise,
        mdns_name=mdns_name,
        track_desktop=tracker_text in _TRUE,
    )
