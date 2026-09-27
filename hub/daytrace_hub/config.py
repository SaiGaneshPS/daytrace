"""DT-10: hub profiles (personal / shared-dev / demo), ports, data paths and who may connect.

Each profile has its own port and its own database, so real activity never mixes with shared or demo data.
DT-45 adds where the hub listens (discovery.listen_addresses), which web pages may call it (origin_allowed), and
the ledger of every connection it made or refused (LEDGER, shown by GET /privacy/network).
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import sys
import threading
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

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
    # Where it listens is worked out from this computer's addresses (discovery.listen_addresses, DT-45): this
    # computer, your LAN, and Tailscale only with allow_tailscale. Never a public address.

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
    # Off unless load_settings() turns it on (DT-42), so tests never have the model sort their apps behind their back.
    ai_categories: bool = False
    # The local model reads meals sent as text (DT-42); off, their text is only split. It runs with the request.
    ai_meals: bool = True
    # The built dashboard (DT-30) from DAYTRACE_DASHBOARD_DIR; None: dashboard/dist next to the hub in this repo.
    dashboard_dir: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.profile.database_filename

    @property
    def tracker_lock_path(self) -> Path:
        return self.data_dir / f"{self.profile.database_filename}.tracker.lock"


def _folder(env: Mapping[str, str], name: str) -> Path | None:
    """An absolute folder from the environment (~ and %VAR% or $VAR expanded), or None when it is not set. A
    relative one would move with the current directory, so it is refused."""
    configured = env.get(name, "").strip()
    if not configured:
        return None
    folder = Path(os.path.expandvars(configured)).expanduser()
    if not folder.is_absolute():
        raise ValueError(f"{name} must be an absolute path, got {configured!r}")
    return folder


def load_settings(profile_name: str = "personal", env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    # A relative data folder would silently start an empty database somewhere else.
    data_dir = _folder(env, "DAYTRACE_DATA_DIR") or default_data_dir(env)
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
    # The local model sorts apps nothing knows into categories (DT-42): on for every profile, as it stays local.
    ai_text = env.get("DAYTRACE_AI_CATEGORIES", "").strip().lower() or "on"
    if ai_text not in (*_TRUE, *_FALSE):
        raise ValueError(f"DAYTRACE_AI_CATEGORIES must be on or off, got {ai_text!r}")
    meals_text = env.get("DAYTRACE_AI_MEALS", "").strip().lower() or "on"
    if meals_text not in (*_TRUE, *_FALSE):
        raise ValueError(f"DAYTRACE_AI_MEALS must be on or off, got {meals_text!r}")
    return Settings(
        profile=profile,
        data_dir=data_dir,
        lan_networks=lan_networks,
        advertise_mdns=advertise,
        mdns_name=mdns_name,
        track_desktop=tracker_text in _TRUE,
        ai_categories=ai_text in _TRUE,
        ai_meals=meals_text in _TRUE,
        dashboard_dir=_folder(env, "DAYTRACE_DASHBOARD_DIR"),
    )


# --- which web pages may call the hub (DT-45) ---------------------------------------------------------------------

# Browser extensions (DT-18) call the hub from their own pages, with their own token; a web site can't claim one.
EXTENSION_SCHEMES = frozenset({"chrome-extension", "moz-extension", "safari-web-extension", "ms-browser-extension"})


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """Whether a request's Origin may call the hub (no CORS headers are ever sent, so a browser can't read an answer
    from another origin; this refuses the request itself, so another site can't make one with side effects).

    - No Origin: not from a web page (a phone app, a Shortcut, a script) or a plain page load: allowed.
    - The hub's own pages (the same host and port as the request, or, on this computer, the same port under another
      loopback name: http://127.0.0.1:8765 is the hub at localhost:8765): allowed. The dashboard's dev server
      (Vite) sends the hub's own origin through its proxy, so it counts as the hub's pages.
    - The browser extension's pages: allowed (its token says who it is).
    - Anything else: refused, including another page on this computer (another profile's dashboard, a local web
      app), "null" (a local file, a sandboxed frame) and an Origin that doesn't parse."""
    if origin is None:
        return True
    try:
        parts = urlsplit(origin.strip())
        netloc = parts.netloc
        parts.port  # noqa: B018 (raises for a port that isn't a number)
    except ValueError:
        return False
    if parts.scheme in EXTENSION_SCHEMES:
        return True
    if parts.scheme not in ("http", "https") or not netloc or not host_header:
        return False
    if netloc.lower() == host_header.strip().lower():
        return True
    return _loopback_port(netloc, parts.scheme) is not None and _loopback_port(netloc, parts.scheme) == _loopback_port(host_header, "http")


def _loopback_port(authority: str, scheme: str) -> int | None:
    """The port of a loopback authority (localhost, 127.x, [::1]), or None when it isn't one."""
    name = host_name(authority)
    address = parse_ip(name)
    if name != "localhost" and (address is None or not address.is_loopback):
        return None
    try:
        port = urlsplit(f"//{authority.strip()}").port
    except ValueError:
        return None
    return port or (443 if scheme == "https" else 80)


# --- the network ledger (DT-45) -----------------------------------------------------------------------------------

Where = Literal["localhost", "lan", "tailscale", "internet"]
MAX_LISTED = 20  # blocked destinations listed by name (the counts go on)


def network_of(text: str | None) -> Where:
    """Which kind of network an address is on: this computer, a private LAN, the tailnet, or anything else (the
    internet, or something that isn't an address)."""
    address = parse_ip(text.strip("[]")) if text else None
    if address is None:
        return "internet"
    if any(address in network for network in LOOPBACK_NETWORKS):
        return "localhost"
    if any(address in network for network in TAILSCALE_NETWORKS):  # its IPv6 range sits inside fc00::/7
        return "tailscale"
    if any(address in network for network in DEFAULT_LAN_NETWORKS):
        return "lan"
    return "internet"


class NetworkLedger:
    """Every connection this hub made or refused since it started, by the kind of network at the other end: the
    proof behind "Daytrace never talks to the internet" (GET /privacy/network).

    - Outgoing: each connection to the model server (llm.LocalOnlyBackend, the only thing the hub reaches out to),
      and each attempt refused: by that transport, or by the socket guard (app.network_audit) that refuses any
      connection to the internet from anything in the hub process (`guarded` says whether it is on).
    - Incoming: each request served, and each refused (from a network the profile doesn't serve, a Host name that
      could be DNS rebinding, or another web site's page).
    - Where the hub listens right now."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.since = datetime.now(UTC)
            self.outgoing: Counter[str] = Counter()
            self.blocked: dict[tuple[str, int], dict[str, Any]] = {}
            self.blocked_count = 0
            self.incoming: Counter[str] = Counter()
            self.refused: Counter[str] = Counter()
            self.listening: list[str] = []
            self.guarded = getattr(self, "guarded", False)  # the socket guard can't be taken off once it is on

    def connected(self, where: Where) -> None:
        with self._lock:
            self.outgoing[where] += 1

    def block(self, host: str, port: int) -> None:
        with self._lock:
            self.blocked_count += 1
            entry = self.blocked.get((host, port))
            if entry is None and len(self.blocked) < MAX_LISTED:
                entry = self.blocked[(host, port)] = {"host": host, "port": port, "count": 0}
            if entry is not None:
                entry["count"] += 1
                entry["last"] = datetime.now(UTC)

    def served(self, where: Where) -> None:
        with self._lock:
            self.incoming[where] += 1

    def refuse(self, where: Where) -> None:
        with self._lock:
            self.refused[where] += 1

    def listen(self, addresses: Iterable[str]) -> None:
        with self._lock:
            self.listening = list(addresses)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "since": self.since,
                "outgoing": dict(self.outgoing),
                "blocked": {"count": self.blocked_count, "destinations": [dict(entry) for entry in self.blocked.values()]},
                "incoming": dict(self.incoming),
                "refused": dict(self.refused),
                "listening": list(self.listening),
                "guarded": self.guarded,
            }


LEDGER = NetworkLedger()  # one per hub process (a hub serves one profile)
