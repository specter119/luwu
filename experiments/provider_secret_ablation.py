# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fast pre-implementation ablation for the reviewed M4 contract.

This probe checks the contract's smallest state model before implementation.
The post-implementation M4 gate must exercise the real provider, renderer,
record, writer, CLI, and cache paths with the same counterexamples.
"""

from __future__ import annotations

from dataclasses import dataclass

SENTINEL = "m4-secret-sentinel"


@dataclass(frozen=True)
class Capability:
    declared: frozenset[str]
    granted: frozenset[str]

    def allows(self, capability: str) -> bool:
        return capability in self.declared and capability in self.granted


def provider_start(capability: Capability, *, remove_authority: bool = False) -> bool:
    """Model the only external side effect: it needs both declarations."""

    if remove_authority:
        return "subprocess" in capability.declared
    return capability.allows("subprocess")


def public_provider_error(detail: str, *, remove_boundary: bool = False) -> str:
    if remove_boundary:
        return f"provider failed: {detail}"
    return "provider unavailable"


def record_projection(
    *,
    resource: str,
    target_mode: int,
    secret_size: int,
    provider_reference: str,
    remove_privacy: bool = False,
) -> dict[str, object]:
    if remove_privacy:
        return {
            "resource": resource,
            "target_mode": target_mode,
            "size": secret_size,
            "provider": provider_reference,
        }
    return {
        "resource_count": 1,
        "target_mode": target_mode,
        "state": "planned",
    }


def target_allowed(mode: int, *, remove_owner_only: bool = False) -> bool:
    return remove_owner_only or mode & 0o077 == 0


def decision_from_cache(
    live: str,
    cache_status: str,
    *,
    remove_exclusion: bool = False,
) -> str:
    if remove_exclusion and cache_status == "fresh":
        return "in_sync"
    return "in_sync" if live == "desired" else "drifted"


def main() -> None:
    denied = Capability(frozenset({"subprocess"}), frozenset())
    granted = Capability(frozenset({"subprocess"}), frozenset({"subprocess"}))
    assert not provider_start(denied)
    assert provider_start(granted)
    assert provider_start(denied, remove_authority=True)
    print(
        "authority: denied execution has zero provider start; removing it bypasses the guard"
    )

    assert SENTINEL not in public_provider_error(SENTINEL)
    assert SENTINEL in public_provider_error(SENTINEL, remove_boundary=True)
    print(
        "provider error boundary: fixed message hides sentinel; removing it leaks one"
    )

    safe_record = record_projection(
        resource="database",
        target_mode=0o600,
        secret_size=len(SENTINEL),
        provider_reference="database-prod/password",
    )
    assert SENTINEL not in repr(safe_record)
    assert "database-prod" not in repr(safe_record)
    unsafe_record = record_projection(
        resource="database",
        target_mode=0o600,
        secret_size=len(SENTINEL),
        provider_reference="database-prod/password",
        remove_privacy=True,
    )
    assert "database-prod" in repr(unsafe_record)
    print(
        "record privacy: secret-derived metadata/reference are excluded; removing it leaks one"
    )

    assert target_allowed(0o600)
    assert not target_allowed(0o644)
    assert target_allowed(0o644, remove_owner_only=True)
    print(
        "owner-only target: 0600 passes and broad permissions block; removal bypasses one case"
    )

    assert decision_from_cache("drifted", "fresh") == "drifted"
    assert decision_from_cache("drifted", "fresh", remove_exclusion=True) == "in_sync"
    print(
        "cache exclusion: cache cannot change a live decision; removing it masks drift"
    )

    # One provider, one capability, and one record contract do not need the
    # abstractions rejected by the review.
    for removed in (
        "network capability",
        "provider registry/plugin discovery",
        "lazy provider",
        "generic transaction engine",
        "secret hash/value fingerprint",
        "automatic replay/rollback",
        "second ledger",
        "platform-check authorization cache",
        "per-item cache state",
    ):
        print(f"remove {removed}: no reference scenario depends on it")


if __name__ == "__main__":
    main()
