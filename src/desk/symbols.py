"""Explicit desk/Webull aliases, never a general punctuation heuristic.

Checked: user iMac metadata observation 2026-10-01; see checkpoint 08a.
Other share classes need their own observed identity before adding a mapping.
"""

# Canonical desk symbol -> (Webull wire symbol, verified Webull instrument ID).
WEBULL_IDENTITIES = {"BRK.B": ("BRK B", "916040668")}
ALIASES = {"BRK B": "BRK.B", "BRK-B": "BRK.B"}


def canonical_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    return ALIASES.get(normalized, normalized)


def webull_symbol(symbol: str) -> str:
    canonical = canonical_symbol(symbol)
    return WEBULL_IDENTITIES.get(canonical, (canonical, None))[0]
