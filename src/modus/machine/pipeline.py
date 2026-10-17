"""Thin orchestration for the six-stage machine lifecycle.

All semantic work lives behind the stage verbs below. This module owns only
the run lock, resource policy, progress reporting and observational timings.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import perf_counter

from modus.artifacts_io import required_reentrant_file_lock
from modus.config import ModusConfig
from modus.paths import ModusPaths

from .capability import compose
from .corpus import freeze
from .errors import ConfigurationError
from .facts import observe
from .implementation import implementation_digest
from .packet import package
from .progress import Progress, ProgressReporter
from .projection import project
from .resolution import resolve
from .resolution.evidence_graph import build_evidence_graph as build_evidence_graph
from .runtime.resources import resolve_policy, use_policy
from .store.contracts import AnalysisOptions, AnalysisReceipt


# 流水线只组合阶段，不在入口复制任一阶段的语义判断。
# 必需重入锁覆盖冻结至发布，防止同项目的两个运行交错提交。
# 激活快照与资源策略具有作用域，退出后不污染下一次分析。
# 工作区需逐成员分析，外层导航目录不能被误认为单一源码仓库。
# 耗时是观测数据，在发布回执返回后追加，不参与证据身份。
# 进度回调由调用者提供，流水线无需知道终端或日志实现。
def build_pipeline(
    root: str | Path,
    options: AnalysisOptions,
    *,
    progress: Progress | None = None,
) -> AnalysisReceipt:
    """Run ``freeze -> observe -> resolve -> compose -> project -> package``."""
    paths = ModusPaths.for_root(root)
    if not paths.root.is_dir():
        raise ConfigurationError(
            "machine.corpus.invalid-root",
            "repository root is not a directory",
        )
    if ModusConfig.read_available(paths).kind == "workspace":
        raise ConfigurationError(
            "machine.corpus.workspace-root",
            "analyze confirmed workspace members independently",
        )
    policy = resolve_policy(
        options.profile,
        workers=options.workers,
        memory_budget=options.memory_bytes,
    )
    timing_started = perf_counter()
    stage_started = timing_started
    current_stage = ""
    stage_timings: dict[str, float] = {}
    package_progress = ProgressReporter(progress)

    def begin_stage(name: str) -> None:
        nonlocal current_stage, stage_started
        now = perf_counter()
        if current_stage != "":
            stage_timings[current_stage] = (
                stage_timings.get(current_stage, 0.0) + (now - stage_started) * 1000
            )
        current_stage = name
        stage_started = now
        if progress:
            progress(name)

    with required_reentrant_file_lock(paths.init_analysis_lock):
        begin_stage("freeze")
        corpus = freeze(paths.root, compiler_mode=options.compiler_mode)
        with corpus.activate(), use_policy(policy):
            begin_stage("observe")
            facts = observe(corpus)
            begin_stage("resolve")
            model = resolve(facts, graph_builder=build_evidence_graph)
            begin_stage("compose")
            capabilities = compose(model)
            begin_stage("project")
            projections = project(model, capabilities)
            begin_stage("package")
            receipt = package(
                corpus,
                facts.sealed_corpus,
                projections,
                progress=package_progress,
            )

    finished = perf_counter()
    if current_stage != "":
        stage_timings[current_stage] = (
            stage_timings.get(current_stage, 0.0) + (finished - stage_started) * 1000
        )
    if progress:
        progress("complete")
    return replace(
        receipt,
        timings={
            "totalMs": round((finished - timing_started) * 1000, 3),
            "stages": {
                name: round(milliseconds, 3) for name, milliseconds in stage_timings.items()
            },
            "packageStages": {
                name.removeprefix("package."): round(milliseconds, 3)
                for name, milliseconds in package_progress.timings().items()
            },
        },
    )


__all__ = ["build_pipeline", "implementation_digest"]
