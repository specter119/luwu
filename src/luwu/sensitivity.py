"""The single predicate that decides whether a public name looks secret-bearing."""

from __future__ import annotations

import re
import unicodedata

_FRAGMENTS = (
    "apikey",
    "accesskey",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)
_NAMES = frozenset(
    {
        "access_key",
        "api_key",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secrets",
        "session_token",
        "token",
    }
)
_SUFFIXES = ("_credential", "_credentials", "_key", "_password", "_secret", "_token")


def is_sensitive_name(name: str, *, public: frozenset[str] = frozenset()) -> bool:
    """Return whether a key or declared name must stay out of public documents.

    Every layer that admits public names (manifest declarations, input layers,
    and parsed documents) uses this predicate, so the layers cannot disagree
    about what counts as a secret-bearing name. ``public`` lists exact names
    the manifest author has declared public, such as a key that only names an
    environment variable.
    """

    if name in public:
        return False

    normalized = unicodedata.normalize("NFKC", name)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    normalized = normalized.casefold().replace("-", "_")
    compact = "".join(char for char in normalized if char.isalnum())
    return (
        normalized in _NAMES
        or normalized.endswith(_SUFFIXES)
        or any(fragment in compact for fragment in _FRAGMENTS)
    )
