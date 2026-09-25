"""DT-10: hub profiles (personal / shared-dev / demo), ports and data paths.

Owner: You. Goal, instructions and expected result are in the Notion ticket.
"""
from __future__ import annotations

# Single source of truth for profile names. DT-10 adds the host, port and data path for each one.
PROFILES: tuple[str, ...] = ("personal", "shared-dev", "demo")
