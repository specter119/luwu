from __future__ import annotations

from tests.support.projects import ExecutionProject


def project_bytes(project: ExecutionProject) -> dict[str, bytes | None]:
    return {
        f"source:{name}": path.read_bytes() for name, path in project.sources.items()
    } | {
        f"target:{name}": path.read_bytes() if path.exists() else None
        for name, path in project.targets.items()
    }
