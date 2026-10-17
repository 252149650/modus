"""Progress is a bounded observability contract, not analysis evidence.

These tests exercise safe rendering, rejected fields and throttling while
preserving the phase timings needed by callers.
"""

from __future__ import annotations

import pytest

from modus.machine.progress import ProgressEvent, ProgressReporter


# 格式使用协议字段而非源码内容，方便终端与日志消费者共用。
def test_progress_event_renders_a_stable_safe_line() -> None:
    event = ProgressEvent(
        "package.catalog.evidence-write",
        completed=2048,
        total=8192,
        unit="records",
        elapsed_ms=1234.6,
    )

    assert event.render() == (
        "package.catalog.evidence-write progress=2048/8192 unit=records elapsedMs=1235"
    )


# 缺少 completed 的 total 没有可解释进度，应在事件构造时拒绝。
@pytest.mark.parametrize(
    "event",
    (
        lambda: ProgressEvent("Package.Verify"),
        lambda: ProgressEvent("package.verify", completed=-1),
        lambda: ProgressEvent("package.verify", total=1),
        lambda: ProgressEvent("package.verify", completed=1, unit="Source Files"),
    ),
)
def test_progress_event_rejects_ambiguous_or_unsafe_fields(event) -> None:
    with pytest.raises(ValueError):
        event()


# 注入时钟隔离墙钟波动；节流丢弃展示事件，不能丢失阶段耗时。
def test_progress_reporter_throttles_updates_and_retains_phase_timings() -> None:
    now = [0.0]
    lines: list[str] = []
    reporter = ProgressReporter(
        lines.append,
        min_update_interval=5.0,
        clock=lambda: now[0],
    )

    reporter.transition("package.verify-inputs")
    now[0] = 1.0
    reporter.advance(1, 10, unit="files")
    now[0] = 5.0
    reporter.advance(5, 10, unit="files")
    now[0] = 6.0
    reporter.transition("package.portable.snapshot")
    now[0] = 8.0
    reporter.finish()

    assert lines == [
        "package.verify-inputs",
        "package.verify-inputs progress=5/10 unit=files elapsedMs=5000",
        "package.portable.snapshot",
    ]
    assert reporter.timings() == {
        "package.verify-inputs": 6000.0,
        "package.portable.snapshot": 2000.0,
    }
