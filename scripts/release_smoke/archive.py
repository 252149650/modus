"""Safely inventory and extract wheel and source-distribution archives."""

from __future__ import annotations

import tarfile
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from stat import S_ISLNK
from zipfile import BadZipFile, ZipFile, ZipInfo

from .contracts import (
    SDIST_WORKFLOW_ROOT,
    WHEEL_WORKFLOW_ROOT,
    ArtifactInventory,
    ArtifactKind,
    ReleaseCheckError,
)
from .diagnostics import format_paths, require


# 先去除目录尾斜杠再查重，防止文件/目录同名被集合静默吞掉。
# 集合归一化只在重复检查通过之后执行。
def _normalized_members(names: Iterable[str], *, artifact: Path) -> frozenset[str]:
    normalized = tuple(filter(None, (name.rstrip("/") for name in names)))
    duplicates = {name for name, count in Counter(normalized).items() if count > 1}
    require(
        not duplicates,
        f"{artifact.name} 包含重复归档路径：\n{format_paths(duplicates)}",
    )
    return frozenset(normalized)


# 加密条目无法透明核验，链接条目可能逃离目标目录，均拒绝。
# 安全路径要求与解压时共用，避免清单检查与写盘策略分叉。
def _unsafe_wheel_entries(entries: Sequence[ZipInfo]) -> frozenset[str]:
    return frozenset(
        entry.filename
        for entry in entries
        if not _is_safe_archive_path(entry.filename)
        or bool(entry.flag_bits & 0x1)
        or S_ISLNK(entry.external_attr >> 16)
    )


def _validated_wheel_entries(
    archive: ZipFile,
    *,
    artifact: Path,
) -> tuple[tuple[ZipInfo, ...], frozenset[str]]:
    entries = tuple(archive.infolist())
    unsafe = _unsafe_wheel_entries(entries)
    require(
        not unsafe,
        f"{artifact.name} 包含不安全、加密或链接路径：\n{format_paths(unsafe)}",
    )
    members = _normalized_members((entry.filename for entry in entries), artifact=artifact)
    return entries, members


# 元数据必须唯一；多份 dist-info 不能靠排序选择其中一份。
def _single_member(members: frozenset[str], suffix: str, *, artifact: Path) -> str:
    matches = sorted(member for member in members if member.endswith(suffix))
    require(
        len(matches) == 1,
        f"{artifact.name} 必须恰好包含一个 {suffix}; found {len(matches)}",
    )
    return matches[0]


# 先校验所有路径，才读取元数据正文。
# 底层格式与编码错误转为发布契约错误，保留异常链便于排查。
def _read_wheel(path: Path) -> ArtifactInventory:
    try:
        with ZipFile(path) as archive:
            _, members = _validated_wheel_entries(archive, artifact=path)
            metadata_path = _single_member(members, ".dist-info/METADATA", artifact=path)
            entry_points_path = _single_member(
                members,
                ".dist-info/entry_points.txt",
                artifact=path,
            )
            metadata = archive.read(metadata_path).decode("utf-8")
            entry_points = archive.read(entry_points_path).decode("utf-8")
    except ReleaseCheckError:
        raise
    except (BadZipFile, KeyError, OSError, UnicodeError) as exc:
        raise ReleaseCheckError(f"无法读取 wheel：{path.name}: {exc}") from exc

    return ArtifactInventory(
        path=path,
        kind=ArtifactKind.WHEEL,
        members=members,
        workflow_root=WHEEL_WORKFLOW_ROOT,
        metadata=metadata,
        entry_points=entry_points,
    )


# 源码发行包必须只有一个顶层目录，去掉该层才能与源码布局比较。
# 不接受多根结构，以免缺失文件被其他根的同名文件掩盖。
def _strip_sdist_root(member_names: Sequence[str], *, artifact: Path) -> frozenset[str]:
    """Remove the single top-level directory required by the sdist format."""

    paths = tuple(PurePosixPath(name) for name in member_names)
    roots = {path.parts[0] for path in paths if path.parts}
    require(
        len(roots) == 1,
        "source distribution must have exactly one top-level directory; "
        f"found {', '.join(sorted(roots)) or 'none'}",
    )
    return _normalized_members(
        (PurePosixPath(*path.parts[1:]).as_posix() for path in paths if len(path.parts) > 1),
        artifact=artifact,
    )


# 源码包只接受普通文件和目录，不读取链接或设备节点。
# 这里只构造清单，无需把源码包解压到磁盘。
def _read_sdist(path: Path) -> ArtifactInventory:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            entries = tuple(archive.getmembers())
            unsafe = {
                entry.name
                for entry in entries
                if not _is_safe_archive_path(entry.name) or not (entry.isfile() or entry.isdir())
            }
            require(
                not unsafe,
                f"{path.name} 包含不安全或非常规路径：\n{format_paths(unsafe)}",
            )
            members = _strip_sdist_root(
                tuple(entry.name for entry in entries),
                artifact=path,
            )
    except ReleaseCheckError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseCheckError(f"无法读取源码包：{path.name}: {exc}") from exc

    return ArtifactInventory(
        path=path,
        kind=ArtifactKind.SDIST,
        members=members,
        workflow_root=SDIST_WORKFLOW_ROOT,
    )


def inspect_artifact(path: Path) -> ArtifactInventory:
    """Read a supported artifact into a format-independent inventory."""

    require(path.is_file(), f"安装包产物不存在：{path}")
    if path.suffix == ".whl":
        return _read_wheel(path)
    if path.name.endswith(".tar.gz"):
        return _read_sdist(path)
    raise ReleaseCheckError(f"不支持的产物：{path.name!r}；需要 .whl 或 .tar.gz 安装包")


# 解压前重新检查当前归档，不能依赖此前读取到的旧清单。
# 校验异常原样传播，不把不安全归档降级为普通读取失败。
def extract_wheel(wheel: Path, destination: Path) -> None:
    """Extract a wheel after rejecting non-portable or escaping member paths."""

    try:
        with ZipFile(wheel) as archive:
            _validated_wheel_entries(archive, artifact=wheel)
            archive.extractall(destination)
    except ReleaseCheckError:
        raise
    except (BadZipFile, OSError) as exc:
        raise ReleaseCheckError(f"无法解压 wheel：{wheel.name}: {exc}") from exc


# 要求规范的 POSIX 相对路径，拒绝父目录、盘符和混合分隔符。
# 规范化前后必须一致，防止不同平台对同一路径作不同解释。
def _is_safe_archive_path(member: str) -> bool:
    normalized = member.rstrip("/")
    path = PurePosixPath(normalized)
    return (
        bool(path.parts)
        and normalized == path.as_posix()
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in member
        and ":" not in path.parts[0]
        and "\x00" not in member
    )
