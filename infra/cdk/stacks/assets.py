"""Build-context excludes for the Docker image assets.

Every image builds from the repository root (the Dockerfiles copy paths like
`services/api`), and CDK fingerprints the whole context. With a blanket exclude list any
edit anywhere (an eval result, a doc, a test) changed every image hash, so every deploy
rebuilt and redeployed all fourteen images and bounced the EFS-backed console task. The
helper below excludes everything an image does not copy, so its hash moves only when its
own inputs do. Pure: no CDK import, unit-tested."""

from __future__ import annotations

import os

ALWAYS = (".git", "**/__pycache__", "**/.venv", "**/node_modules", "**/.DS_Store")


def context_excludes(root: str, keep: list[str]) -> list[str]:
    """Exclude patterns (dockerignore style, relative to `root`) that leave only `keep`
    (files or directories, relative paths) in the build context. Siblings of every kept
    path's ancestors are excluded explicitly, so no negation patterns are needed."""
    keep_parts = [tuple(k.strip("/").split("/")) for k in keep]
    excluded: list[str] = []

    def walk(prefix: tuple[str, ...]) -> None:
        directory = os.path.join(root, *prefix) if prefix else root
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            return
        for name in entries:
            path = prefix + (name,)
            if any(k[: len(path)] == path for k in keep_parts):
                # Kept outright, or an ancestor of a kept path: descend for the latter.
                if not any(k == path for k in keep_parts):
                    walk(path)
                continue
            excluded.append("/".join(path))

    walk(())
    return list(ALWAYS) + excluded
