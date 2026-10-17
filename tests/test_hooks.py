"""Executable contracts for the session-start integration boundary."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from modus.hooks import HOST_CONTRACTS, SUPPORTED_HOSTS, bootstrap
from modus.hooks.contracts import require_host
from modus.hooks.environment import persist_claude_environment
from modus.hooks.protocol import (
    MAX_HOOK_INPUT_BYTES,
    MAX_SESSION_ID_BYTES,
    HookEvent,
    parse_hook_event,
    read_hook_event,
)
from modus.hooks.runtime import run_hook


# 使用最小合法事件隔离协议边界；序列化方式不依赖宿主进程。
def _event_payload(*, event_name: str = "SessionStart", session_id: str = "session-123") -> bytes:
    return json.dumps(
        {"hook_event_name": event_name, "session_id": session_id},
        separators=(",", ":"),
    ).encode("utf-8")


# 先验证配置形状再读取事件名，避免坏配置被空集合断言掩盖。
def _hook_event_names(config: dict[str, object]) -> set[str]:
    hooks = config.get("hooks")
    assert isinstance(hooks, dict)
    assert all(isinstance(name, str) for name in hooks)
    return {name for name in hooks if isinstance(name, str)}


# 注册表、启动资产和运行时必须共享平台词汇。
# 新增宿主需要同时补齐这三处投影，不能只接受名称。
def test_host_registry_is_the_single_complete_platform_vocabulary() -> None:
    assert tuple(HOST_CONTRACTS) == SUPPORTED_HOSTS == bootstrap.PLATFORMS
    assert all(contract.name == name for name, contract in HOST_CONTRACTS.items())


# 静态配置与运行时回包是不同协议，不能共用一个想当然的 JSON 形状。
# 逐宿主检查信封，再检查其中承载的共同工作流指引。
@pytest.mark.parametrize(
    ("host", "static_key", "runtime_is_json"),
    [
        ("claude", "hookSpecificOutput", True),
        ("cursor", "additional_context", False),
        ("codex", "additionalContext", True),
        ("codebuddy", "hookSpecificOutput", False),
    ],
)
def test_each_host_preserves_its_static_and_runtime_envelopes(
    tmp_path: Path,
    host: str,
    static_key: str,
    runtime_is_json: bool,
) -> None:
    static_payload = require_host(host).static_payload("context")
    assert isinstance(static_payload, dict)
    assert static_key in static_payload

    output = run_hook(_event_payload(), root=tmp_path, platform=host, environ={})
    if runtime_is_json:
        parsed = json.loads(output)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        context = parsed["hookSpecificOutput"]["additionalContext"]
    else:
        assert output.startswith("<MODUS_CONTEXT>")
        context = output
    assert "using-modus" in context
    assert "知识影响检查点" in context
    assert "最终回复前" in context


# 底层解析器负责拒绝歧义，宿主适配器负责保护会话启动。
# 同一坏输入在两层有不同结果，这是有意的失败策略。
@pytest.mark.parametrize(
    "payload",
    [
        b'{"hook_event_name":"SessionStart","session_id":"a","session_id":"b"}',
        b'{"hook_event_name":"SessionStart","session_id":"a","value":NaN}',
        b"[]",
        b"{not-json}",
    ],
)
def test_strict_parser_explains_invalid_input_while_runtime_adapter_fails_open(
    payload: bytes,
) -> None:
    with pytest.raises(ValueError):
        parse_hook_event(payload, host="codex")
    assert read_hook_event(payload, host="codex") is None


# 使用多字节字符证明限制按 UTF-8 字节计量，而非字符数。
# 非启动事件应被忽略，不应当作坏输入报错。
def test_protocol_enforces_wire_and_identifier_byte_limits() -> None:
    oversized_wire_payload = b"{" + b" " * MAX_HOOK_INPUT_BYTES + b"}"
    with pytest.raises(ValueError, match="64 KiB"):
        parse_hook_event(BytesIO(oversized_wire_payload), host="codex")

    oversized_session_id = "界" * (MAX_SESSION_ID_BYTES // 3 + 1)
    with pytest.raises(ValueError, match="标识长度上限"):
        parse_hook_event(_event_payload(session_id=oversized_session_id), host="codex")

    assert parse_hook_event(_event_payload(event_name="UserPromptSubmit"), host="codex") is None


# 预置用户内容且不带末尾换行，覆盖追加时的分隔边界。
# 重复启动只能保留一份绑定，不覆盖用户已有环境变量。
def test_claude_environment_binding_preserves_content_and_is_idempotent(tmp_path: Path) -> None:
    environment_file = tmp_path / "claude.env"
    environment_file.write_text("export TEAM_VALUE=kept", encoding="utf-8")
    event = HookEvent(host="claude", session_key="sha256:" + "a" * 64)

    for _ in range(2):
        persist_claude_environment(event, {"CLAUDE_ENV_FILE": str(environment_file)})

    lines = environment_file.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "export TEAM_VALUE=kept",
        f"export MODUS_HOOK_SESSION_KEY={event.session_key}",
        "export MODUS_HOOK_PLATFORM=claude",
    ]


# 检查生成资产本身，以覆盖尚未安装 Python 的静态启动路径。
# 转义后的指引仍须完整，不能因跨 shell 序列化丢失中文。
def test_generated_assets_cover_every_host_without_runtime_dependencies() -> None:
    dispatcher = bootstrap.run_hook_cmd()
    fallback = bootstrap.session_start_script()

    assert dispatcher.startswith(":;")
    assert "@echo off" in dispatcher
    assert "知识影响检查点".encode("unicode_escape").decode("ascii") in dispatcher
    assert "知识影响检查点".encode("unicode_escape").decode("ascii") in fallback
    for host in SUPPORTED_HOSTS:
        assert f"modus_static_{host}" in dispatcher
        assert f"  {host})" in fallback


# 事件集合限定自动执行时机，防止新增配置意外扩展到每轮对话。
# 大小写遵守各宿主协议，不在公共层统一转换。
def test_generated_hook_configs_remain_session_start_only() -> None:
    hook_events = (
        _hook_event_names(bootstrap.claude_hook_config()),
        _hook_event_names(bootstrap.cursor_hook_config()),
        _hook_event_names(bootstrap.codex_hook_config()),
        _hook_event_names(bootstrap.codebuddy_hook_config()),
    )

    assert hook_events == (
        {"SessionStart"},
        {"sessionStart"},
        {"SessionStart"},
        {"SessionStart"},
    )
    assert all(
        not events.intersection({"Stop", "UserPromptSubmit", "PostToolUse"})
        for events in hook_events
    )
