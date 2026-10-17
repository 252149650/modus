"""Registry-driven language capability coordinates shared by all six layers.

The registry describes extension discovery, compiler producers and
framework entry adapters in one place. Every lookup yields one of three
monotone evidence tiers: semantic (T1), structural (T2), or bounded generic
text (T3). Framework support is orthogonal metadata, never an excuse to raise
the evidence tier.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final, TypedDict, cast

from .errors import ConfigurationError


class CapabilityTier(StrEnum):
    """Highest evidence fidelity available for one source language."""

    SEMANTIC = "T1"
    STRUCTURAL = "T2"
    GENERIC = "T3"


class CapabilityRecord(TypedDict):
    """Normalized registry row used by N1 and N2."""

    language: str
    role: str
    extractor: str
    tier: str
    compiler_provider: str
    framework_adapters: tuple[str, ...]
    entry_detectors: tuple[str, ...]


# 框架适配器是附加能力，不能提升语言本身的证据精度上限。
@dataclass(frozen=True, slots=True)
class LanguageCapability:
    """Capability coordinates for a single path."""

    language: str
    role: str
    extractor: str
    tier: CapabilityTier
    compiler_provider: str = ""
    framework_adapters: tuple[str, ...] = ()
    entry_detectors: tuple[str, ...] = ()

    @property
    def degradation_reason(self) -> str:
        """Explain why this path cannot provide T1 evidence, if applicable."""
        if self.tier is CapabilityTier.GENERIC:
            return "machine.facts.generic-text-fallback"
        if self.tier is CapabilityTier.STRUCTURAL:
            return "machine.facts.compiler-provider-unsupported"
        return ""


# JSON 重复键在读取时拒绝，避免同扩展名后声明静默覆盖。
# 注册表按进程缓存，包升级由新的实现和缓存身份处理。
@lru_cache(maxsize=1)
def _registry_document() -> dict[str, object]:
    def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigurationError(
                    "machine.facts.duplicate-language-declaration",
                    "language registry keys must be unique",
                )
            result[key] = value
        return result

    raw = json.loads(
        Path(__file__).with_name("language-registry.json").read_text(encoding="utf-8"),
        object_pairs_hook=object_without_duplicates,
    )
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        raise ConfigurationError(
            "machine.facts.invalid-language-registry",
            "language registry must use schema version 1",
        )
    return cast(dict[str, object], raw)


def _string_mapping(value: object, *, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            "machine.facts.invalid-language-registry",
            "language registry field must be an object",
            details={"field": field},
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not key or not item:
            raise ConfigurationError(
                "machine.facts.invalid-language-registry",
                "language registry names and values must be non-empty strings",
                details={"field": field},
            )
        result[key] = item
    return result


#: Semantic-provider identity and version per T1 language. Version bumps
#: invalidate provider caches and come from the same registry as discovery.
PROVIDER_VERSIONS: Final[dict[str, str]] = _string_mapping(
    _registry_document().get("compilerProviders"), field="compilerProviders"
)


def compiler_language(language: str) -> str:
    """Map a source language to the compiler producer that serves it."""
    return "cpp" if language == "c" else language


def _profile(language: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw_profiles = _registry_document().get("frameworkProfiles")
    if not isinstance(raw_profiles, Mapping):
        return (), ()
    raw = raw_profiles.get(language)
    if not isinstance(raw, Mapping):
        return (), ()

    def values(field: str) -> tuple[str, ...]:
        source = raw.get(field)
        if not isinstance(source, list) or any(not isinstance(item, str) for item in source):
            return ()
        return tuple(sorted(cast(list[str], source)))

    return values("frameworkAdapters"), values("entryDetectors")


# 证据层级从实际提供者与提取器推导，不能任意填写更高层级。
# 语言、用途与提取器为必填坐标，声明不完整应尽早失败。
@lru_cache(maxsize=1)
def capability_registry() -> dict[str, CapabilityRecord]:
    """Return validated extension rows enriched with tier and adapter metadata."""
    raw_extensions = _registry_document().get("extensions")
    if not isinstance(raw_extensions, Mapping):
        raise ConfigurationError(
            "machine.facts.invalid-language-registry",
            "extensions must be an object",
        )
    records: dict[str, CapabilityRecord] = {}
    for suffix, value in raw_extensions.items():
        if not isinstance(suffix, str) or not isinstance(value, Mapping):
            raise ConfigurationError(
                "machine.facts.invalid-language-registry",
                "extension declarations must be named objects",
            )
        language = value.get("language")
        role = value.get("role")
        extractor = value.get("structuralExtractor") or value.get("extractor")
        if not all(isinstance(item, str) and item for item in (language, role, extractor)):
            raise ConfigurationError(
                "machine.facts.invalid-language-registry",
                "extension declarations require language, role, and extractor",
            )
        language = cast(str, language)
        role = cast(str, role)
        extractor = cast(str, extractor)
        provider_language = compiler_language(language)
        provider = PROVIDER_VERSIONS.get(provider_language, "")
        tier = (
            CapabilityTier.SEMANTIC
            if provider
            else CapabilityTier.GENERIC
            if extractor == "extract_generic_text"
            else CapabilityTier.STRUCTURAL
        )
        adapters, detectors = _profile(language)
        records[suffix] = {
            "language": language,
            "role": role,
            "extractor": extractor,
            "tier": str(tier),
            "compiler_provider": provider,
            "framework_adapters": adapters,
            "entry_detectors": detectors,
        }
    return records


def _capability(record: CapabilityRecord) -> LanguageCapability:
    return LanguageCapability(
        record["language"],
        record["role"],
        record["extractor"],
        CapabilityTier(record["tier"]),
        record["compiler_provider"],
        record["framework_adapters"],
        record["entry_detectors"],
    )


# 未知扩展名回退到有界通用文本，仍保留其语言标签。
# 模板复合后缀优先于普通后缀，以免 Blade 被当作一般 PHP 源码。
def capability_for(path: str | Path) -> LanguageCapability:
    """Resolve a path, defaulting unknown extensions to bounded T3 evidence."""
    path = Path(path)
    registry = capability_registry()
    if path.name.endswith(".blade.php"):
        base = registry.get(".php")
        if base is not None:
            blade_record = dict(base)
            blade_record["role"] = "template"
            blade_record["extractor"] = "extract_blade"
            return _capability(cast(CapabilityRecord, blade_record))
    selected = registry.get(path.suffix) or registry.get(path.suffix.lower())
    if selected is not None:
        return _capability(selected)
    language = path.suffix.lower().lstrip(".") or "generic"
    return LanguageCapability(
        language,
        "source",
        "extract_generic_text",
        CapabilityTier.GENERIC,
    )


@lru_cache(maxsize=1)
def discovery_suffixes() -> frozenset[str]:
    """Return every registered suffix whose role is source, config or schema."""
    return frozenset(
        suffix for suffix, record in capability_registry().items() if record["role"] != "document"
    )


def compiler_supported(language: str) -> bool:
    """Return whether a T1 compiler producer exists for *language*."""
    return compiler_language(language) in PROVIDER_VERSIONS


# 声明与实现双向比对，既拒绝未知实现也拒绝未登记提供者。
# 版本必须一致，使能力说明与缓存失效契约共用同一事实。
# 框架解析器和入口探测器也需注册，不能只校验编译器。
# 延迟导入扩展表避免注册表与实现模块形成初始化环。
def validate_language_registry() -> None:
    """Fail fast when declarations and built-in implementations diverge."""
    from .facts.extension_registry import builtin_extensions

    implementations = builtin_extensions()
    document = _registry_document()
    declared_providers = _string_mapping(
        document.get("compilerProviders"), field="compilerProviders"
    )
    for name, version in declared_providers.items():
        implementation = implementations.compiler_providers.get(name)
        if implementation is None:
            raise ConfigurationError(
                "machine.facts.unknown-compiler-provider",
                "language registry references an unknown compiler provider",
                details={"name": name},
            )
        if implementation.version != version:
            raise ConfigurationError(
                "machine.facts.provider-version-mismatch",
                "compiler provider declaration does not match its implementation",
                details={"name": name},
            )
    if set(implementations.compiler_providers) != set(declared_providers):
        raise ConfigurationError(
            "machine.facts.undeclared-compiler-provider",
            "every built-in compiler provider must be declared",
        )

    for suffix, row in capability_registry().items():
        if row["extractor"] not in implementations.structural_extractors:
            raise ConfigurationError(
                "machine.facts.unknown-structural-extractor",
                "language registry references an unknown structural extractor",
                details={"suffix": suffix, "name": row["extractor"]},
            )
        for name in row["framework_adapters"]:
            if name not in implementations.boundary_resolvers:
                raise ConfigurationError(
                    "machine.facts.unknown-boundary-resolver",
                    "language registry references an unknown boundary resolver",
                    details={"suffix": suffix, "name": name},
                )
        for name in row["entry_detectors"]:
            if name not in implementations.entry_detectors:
                raise ConfigurationError(
                    "machine.facts.unknown-entry-detector",
                    "language registry references an unknown entry detector",
                    details={"suffix": suffix, "name": name},
                )


__all__ = [
    "CapabilityRecord",
    "CapabilityTier",
    "LanguageCapability",
    "PROVIDER_VERSIONS",
    "capability_for",
    "capability_registry",
    "compiler_language",
    "compiler_supported",
    "discovery_suffixes",
    "validate_language_registry",
]
