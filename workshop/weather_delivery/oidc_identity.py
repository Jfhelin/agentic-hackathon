"""Construct the supported exact GitHub OIDC branch subject formats."""

import re


def delivery_subjects(repository, owner_id, repository_id, ref):
    parts = repository.split("/") if isinstance(repository, str) else []
    if (
        len(parts) != 2 or not all(parts)
        or any("@" in part or ":" in part for part in parts)
        or not re.fullmatch(r"[1-9][0-9]*", str(owner_id))
        or not re.fullmatch(r"[1-9][0-9]*", str(repository_id))
        or not isinstance(ref, str) or not ref.startswith("refs/heads/")
        or len(ref) == len("refs/heads/")
    ):
        raise ValueError("Invalid repository, immutable IDs or branch ref")
    owner, name = parts
    return (
        f"repo:{repository}:ref:{ref}",
        f"repo:{owner}@{owner_id}/{name}@{repository_id}:ref:{ref}",
    )
