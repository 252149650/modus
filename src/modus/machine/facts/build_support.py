"""Shared, side-effect-bounded support for language build-unit resolvers."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from modus.machine.corpus.input_snapshot import input_exists, read_bytes
from modus.machine.corpus.source_inventory import is_modus_owned_path
from modus.machine.facts.build_contracts import (
    BUILD_CONTEXT_SCHEMA_VERSION,
    Toolchain,
)
from modus.machine.facts.build_contracts import (
    repository_relative as _relative,
)
from modus.machine.facts.semantic_ir import canonical_json
from modus.machine.implementation import layer_implementation_digest
from modus.machine.languages import PROVIDER_VERSIONS
from modus.machine.runtime.toolchains import java_executable
from modus.paths import PROJECT_OUTPUT_DIRS

_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "target",
        "vendor",
        "__pycache__",
        "modus",
        ".modus",
        *PROJECT_OUTPUT_DIRS,
    }
)

_MAVEN_DEPENDENCY_PLUGIN_VERSIONS = ("3.7.0", "3.8.1", "3.6.1", "3.5.0", "3.3.0")
_MAVEN_RISK_MARKERS = (
    "<extensions>true</extensions>",
    "exec-maven-plugin",
    "maven-antrun-plugin",
    "gmaven-plugin",
    "gmavenplus-plugin",
)


def _version(executable: str | None, *args: str) -> tuple[str, str]:
    """Probe a discovered executable with bounded, locale-stable output.

    Arguments remain separate argv elements. A missing tool or unsuccessful probe
    returns a reason for capability reporting rather than failing discovery.
    """

    if not executable:
        return "", "not found on PATH"
    try:
        # NOCA:dangerous-subprocess-use-audit(仅探测本机已发现的工具，调用方使用固定版本参数，不经过 shell)
        completed = subprocess.run(
            [executable, *args],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", type(exc).__name__
    text = (completed.stdout or completed.stderr).strip().splitlines()
    if completed.returncode != 0 or not text:
        return "", "version command failed"
    return text[0][:500], ""


def detect_toolchains() -> tuple[Toolchain, ...]:
    """Probe PATH (and JAVA_HOME) for every supported provider toolchain."""
    specs = (
        ("python", sys.executable, ("--version",)),
        ("java", java_executable("javac"), ("-version",)),
        ("go", shutil.which("go"), ("version",)),
        ("cpp", shutil.which("clang++") or shutil.which("clang"), ("--version",)),
    )
    result: list[Toolchain] = []
    for language, executable, args in specs:
        version, reason = _version(executable, *args)
        result.append(
            Toolchain(
                language=language,
                executable=executable or "",
                version=version,
                available=bool(executable and version),
                reason=reason,
            )
        )
    return tuple(result)


def _descriptors(root: Path, names: frozenset[str]) -> tuple[Path, ...]:
    """Discover build metadata through the active input snapshot when available.

    Directory membership is an input: adding a descriptor must invalidate the
    same context as changing an existing descriptor. Output trees are excluded.
    """

    from modus.machine.corpus.input_snapshot import active_snapshot, directory_members

    snapshot = active_snapshot()
    excluded = tuple(sorted(_EXCLUDED_DIRS))
    members = (
        snapshot.observe_directory(root, names=tuple(sorted(names)), exclude_dirs=excluded)
        if snapshot is not None
        else [
            root / name
            for name in directory_members(root, names=tuple(sorted(names)), exclude_dirs=excluded)
        ]
    )
    return tuple(sorted(path for path in members if not is_modus_owned_path(root, path)))


def _digest_unit(
    repository_root: Path,
    *,
    language: str,
    build_system: str,
    files: tuple[Path, ...],
    descriptors: tuple[Path, ...],
    settings: dict[str, Any],
    toolchain: Toolchain,
) -> str:
    """Bind a build action to its inputs, configuration and producer implementation.

    Paths establish membership; content digests establish identity. C++ command
    selection is already canonicalized in settings, so unrelated database entries
    do not invalidate every action.
    """

    provider_digest = _provider_asset_digest(language)
    digest = hashlib.sha256()
    digest.update(
        canonical_json(
            {
                "language": language,
                "buildSystem": build_system,
                "settings": settings,
                "toolchain": toolchain.to_dict(),
                "providerVersion": PROVIDER_VERSIONS[language],
                "providerAssetDigest": provider_digest,
                "contractVersion": BUILD_CONTEXT_SCHEMA_VERSION,
                "files": [_relative(repository_root, item) for item in files],
                "descriptors": [_relative(repository_root, item) for item in descriptors],
            }
        ).encode("utf-8")
    )
    # Source content is part of the provider snapshot identity. A fact file may
    # never be reused after source changes merely because mtimes happen to match.
    for path in (*descriptors, *files):
        digest.update(_relative(repository_root, path).encode("utf-8"))
        try:
            from modus.machine.corpus.input_snapshot import content_digest

            # The full command database belongs to the input closure, while an
            # individual C++ action depends only on its canonical selected entry.
            if not (
                language == "cpp" and build_system == "compilation-database" and path in descriptors
            ):
                digest.update(content_digest(repository_root, path).encode())
        except (OSError, ValueError) as exc:
            digest.update(f"unreadable:{exc}".encode())
    return digest.hexdigest()


def _provider_asset_digest(language: str) -> str:
    """Fingerprint shipped producer code, including language-specific native assets.

    Memoization belongs to one captured input snapshot. It must not survive an
    implementation change in a later analysis invocation.
    """

    from modus.machine.corpus.input_snapshot import active_snapshot

    snapshot = active_snapshot()
    memo_key = "provider-implementation:" + language
    if snapshot is not None and memo_key in snapshot.memo:
        return str(snapshot.memo[memo_key])
    names = {
        "java": ("ModusJavaAnalyzer.java",),
        "go": ("modus_go_analyzer.go",),
        "cpp": (),
        "python": (),
        "typescript": ("modus_typescript_analyzer.cjs",),
    }[language]
    digest = hashlib.sha256()
    digest.update(PROVIDER_VERSIONS[language].encode("utf-8"))
    digest.update(layer_implementation_digest("facts").encode("ascii"))
    base = Path(__file__).with_name("provider_assets")
    for name in names:
        path = base / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except (OSError, ValueError) as exc:
            digest.update(f"unreadable:{exc}".encode())
    provider = Path(__file__).with_name("semantic_providers.py")
    try:
        digest.update(provider.read_bytes())
    except (OSError, ValueError) as exc:
        digest.update(f"unreadable:{exc}".encode())
    for implementation in sorted(provider.with_name("compiler").glob("*.py")):
        digest.update(implementation.name.encode())
        digest.update(implementation.read_bytes())
    result = digest.hexdigest()
    if snapshot is not None:
        snapshot.memo[memo_key] = result
    return result


def _matches_go_patterns(root: Path, path: Path, patterns: list[str]) -> bool:
    """Apply only patterns whose meaning is known without invoking Go.

    An import-path selector stays eligible here because the compiler owns its
    resolution. Treating it as a filesystem glob would silently drop sources.
    """

    if not patterns or "./..." in patterns:
        return True
    relative = path.relative_to(root).as_posix()
    for raw in patterns:
        pattern = raw.strip()
        if not pattern.startswith("./"):
            # Import-path patterns require `go list`; retain the file here and
            # let the compiler frontend decide rather than silently losing it.
            return True
        target = pattern[2:]
        if target.endswith("/..."):
            prefix = target[:-4].rstrip("/")
            if relative == prefix or relative.startswith(prefix + "/"):
                return True
        elif relative == target or relative.startswith(target.rstrip("/") + "/"):
            return True
    return False


_LOCAL_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"')


def _cpp_header_closure(
    repository_root: Path, source: Path, entry: dict[str, Any]
) -> tuple[Path, ...]:
    """Resolve the project-local quoted-header closure without invoking a compiler."""
    raw = entry.get("arguments")
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        args = list(raw[1:])
    elif isinstance(entry.get("command"), str):
        args = shlex.split(str(entry["command"]))[1:]
    else:
        args = []
    directory = Path(str(entry.get("directory") or source.parent)).resolve()
    include_dirs = [source.parent]
    for position, arg in enumerate(args):
        value = ""
        if arg == "-I" and position + 1 < len(args):
            value = args[position + 1]
        elif arg.startswith("-I") and len(arg) > 2:
            value = arg[2:]
        if value:
            include = Path(value)
            include_dirs.append(
                (directory / include).resolve() if not include.is_absolute() else include.resolve()
            )
    found: set[Path] = set()
    pending = [source]
    while pending:
        current = pending.pop()
        try:
            content = _input_text(repository_root, current)
        except (OSError, ValueError):
            continue
        for line in content.splitlines():
            match = _LOCAL_INCLUDE_RE.match(line)
            if match is None:
                continue
            candidates = [
                current.parent / match.group(1),
                *(item / match.group(1) for item in include_dirs),
            ]
            header = next(
                (item.absolute() for item in candidates if input_exists(repository_root, item)),
                None,
            )
            if header is None or header in found:
                continue
            try:
                header.relative_to(repository_root)
            except ValueError:
                continue
            found.add(header)
            pending.append(header)
    return tuple(sorted(found))


def _input_text(root: Path, path: Path) -> str:
    return read_bytes(root, path).decode("utf-8", errors="replace")


def _java_class_directories(root: Path) -> list[Path]:
    from modus.machine.corpus.input_snapshot import active_snapshot, directory_members

    patterns = ("**/target/classes", "**/build/classes/java/main")
    snapshot = active_snapshot()
    return (
        snapshot.observe_directory(root, patterns=patterns)
        if snapshot is not None
        else [root / item for item in directory_members(root, patterns=patterns)]
    )


def _classpath_fingerprint(classpath: str) -> str:
    """Track class-directory membership and bytes as well as jar content.

    Observation also registers dependencies with the active snapshot. Callers
    must not remove a seemingly redundant invocation merely because they do not
    use its returned digest.
    """
    from modus.machine.corpus.input_snapshot import active_snapshot, directory_members

    snapshot = active_snapshot()
    digest = hashlib.sha256()
    for raw in dict.fromkeys(item for item in classpath.split(os.pathsep) if item):
        path = Path(raw).resolve()
        digest.update(str(path).encode())
        members = (
            (
                snapshot.observe_directory(path)
                if snapshot is not None
                else [path / name for name in directory_members(path)]
            )
            if path.is_dir()
            else [path]
        )
        for member in members:
            digest.update(str(member).encode())
            try:
                content = (
                    snapshot.observe(member, dependency=True)
                    if snapshot
                    else read_bytes(member.parent, member)
                )
                digest.update(hashlib.sha256(content).digest())
            except (OSError, ValueError) as exc:
                digest.update(("unavailable:" + type(exc).__name__).encode())
    return digest.hexdigest()


def _maven_query_is_safe(root: Path, poms: tuple[Path, ...]) -> tuple[bool, str]:
    """Reject known executable extension hooks before optional Maven discovery.

    This guard complements the caller's offline command and subprocess limits;
    it is a conservative marker check, not a general Maven sandbox.
    """

    extension = root / ".mvn" / "extensions.xml"
    if input_exists(root, extension):
        return False, ".mvn/extensions.xml may load executable Maven extensions"
    for name in ("maven.config", "jvm.config"):
        target = root / ".mvn" / name
        try:
            content = _input_text(root, target).lower()
        except (OSError, ValueError):
            continue
        if any(marker in content for marker in ("maven.ext.class.path", "-javaagent", "-agentlib")):
            return False, f".mvn/{name} requests a Maven/JVM extension"
    for pom in poms:
        try:
            compact = "".join(_input_text(root, pom).lower().split())
        except (OSError, ValueError):
            continue
        for marker in _MAVEN_RISK_MARKERS:
            if marker in compact:
                return (
                    False,
                    f"{_relative(root, pom)} contains high-risk build extension/plugin {marker}",
                )
    return True, ""


__all__ = [
    "_MAVEN_DEPENDENCY_PLUGIN_VERSIONS",
    "_classpath_fingerprint",
    "_cpp_header_closure",
    "_descriptors",
    "_digest_unit",
    "_input_text",
    "_matches_go_patterns",
    "_maven_query_is_safe",
    "_provider_asset_digest",
    "_java_class_directories",
    "_version",
    "detect_toolchains",
]
