"""Repository selection policy with ordered, directory-scoped ignore rules."""

from __future__ import annotations

from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

# This policy belongs to Modus discovery, separately from parsing semantics.
_PRIVATE_DIRECTORIES = frozenset(
    {".ssh", ".gnupg", ".aws", ".gcloud", ".secrets", "secrets", "credentials"}
)
_GENERATED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "__pycache__",
        "site-packages",
        "dist",
        "build",
        "target",
        "out",
        "lib64",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        ".cache",
        "coverage",
        "lcov-report",
        "storybook-static",
        "__snapshots__",
        "snapshots",
        "visual-tests",
        "visual-test",
        "dist-protected",
        ".next",
        ".nuxt",
        ".turbo",
        ".angular",
        ".idea",
        ".parcel-cache",
        ".svelte-kit",
        ".terraform",
        ".serverless",
        ".worktrees",
    }
)


def is_sensitive(path: Path) -> bool:
    """Return True when *path* looks like a credential or secret store.

    Matches well-known secret directories, dotenv files, key material
    extensions, and SSH/service-account key names. Ordinary source modules
    that merely discuss authentication remain discoverable.
    """
    name = path.name.casefold()
    if any(part.casefold() in _PRIVATE_DIRECTORIES for part in path.parts[:-1]):
        return True
    if name in {
        ".netrc",
        ".pgpass",
        ".htpasswd",
        "credentials",
        "aws_credentials",
        "gcloud_credentials",
    }:
        return True
    if name in {".env", ".envrc"} or name.startswith((".env.", ".envrc.")):
        return True
    if path.suffix.casefold() in {".pem", ".key", ".p12", ".pfx", ".cert", ".crt", ".der", ".p8"}:
        return True
    if any(
        name == key or name == key + ".pub"
        for key in ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
    ):
        return True
    # Explicit stores are excluded, while ordinary source modules discussing
    # authentication (password_policy.py, token_parser.ts) remain discoverable.
    return name.startswith("service-account") and path.suffix.casefold() == ".json"


def is_noise_directory(part: str, parent: Path | None = None) -> bool:
    """Return True for VCS metadata, build output, and cache directories."""
    return (
        part in _GENERATED_DIRECTORIES
        or part.endswith((".egg-info", "_venv", "_env"))
        or part == "worktrees"
        and parent is not None
        and parent.name.startswith(".")
    )


def parse_gitignore_line(raw: str) -> str:
    """Normalize one gitignore line; return "" for blanks and comments.

    Trailing unescaped spaces are stripped per gitignore semantics.
    """
    line = raw.rstrip("\r\n")
    if not line or line.startswith("#"):
        return ""
    while line.endswith(" ") and not line.endswith("\\ "):
        line = line[:-1]
    return line


def _glob(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    @cache
    def matches(part_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern):
            return part_index == len(parts)
        segment = pattern[pattern_index]
        if segment == "**":
            return matches(part_index, pattern_index + 1) or (
                part_index < len(parts) and matches(part_index + 1, pattern_index)
            )
        return (
            part_index < len(parts)
            and fnmatchcase(parts[part_index], segment)
            and matches(part_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def is_ignored(
    path: Path,
    root: Path,
    patterns: list[tuple[Path, str]],
    *,
    _cache: dict[Path, bool] | None = None,
) -> bool:
    """Apply ordered ignore rules, each relative to its containing ignore file.

    Ancestors are evaluated first, so negating a file cannot traverse an ignored
    directory. Single-star matching never consumes directory separators.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    cache = _cache if _cache is not None else {}
    parent = root
    targets = []
    for segment in relative.parts:
        parent = parent / segment
        targets.append(parent)
    for target in targets:
        if target not in cache:
            ignored = False
            is_directory = target != path or target.is_dir()
            for anchor, rule in patterns:
                try:
                    parts = target.relative_to(anchor).parts
                except ValueError:
                    continue
                negate = rule.startswith("!")
                value = rule[1:] if negate else rule
                if value.endswith("/") and not is_directory:
                    continue
                anchored = value.startswith("/")
                value = value.strip("/")
                value = value.replace("\\#", "#").replace("\\!", "!").replace("\\ ", " ")
                if not value:
                    continue
                matched = (
                    _glob(parts, tuple(value.split("/")))
                    if "/" in value or anchored
                    else bool(parts) and fnmatchcase(parts[-1], value)
                )
                if matched:
                    ignored = not negate
            cache[target] = ignored
        if cache[target]:
            return True
    return False
