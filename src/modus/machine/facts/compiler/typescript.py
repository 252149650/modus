"""Observe TypeScript with an installed compiler API, without install or emit.

The provider runs against the selected build context and returns semantic
records through the shared JSONL boundary. Missing tooling follows the same
optional-versus-required policy as the other compiler providers.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from modus.config import ModusConfig
from modus.machine.corpus.input_snapshot import read_bytes
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.semantic_ir import SemanticFacts, parse_jsonl
from modus.paths import ModusPaths

from .models import CompilerSemanticError
from .process import _asset, _run_process


# 源码从冻结快照读取并以参数文件传递，提供器不会自行重新选择项目文件。
def _run_typescript(paths: ModusPaths, config: ModusConfig, unit: BuildUnit) -> SemanticFacts:
    """Analyze captured local sources; absence is handled by the provider layer."""
    del config
    executable = str(unit.metadata.get("nodeExecutable") or shutil.which("node") or "")
    module = Path(str(unit.metadata.get("typescriptModule") or ""))
    library = module / "lib" / "typescript.js" if module.is_dir() else module
    if not executable or not library.is_file() or library.name != "typescript.js":
        raise CompilerSemanticError(
            "typescript.local-tool-unavailable: requires installed node and TypeScript compiler API"
        )
    sources = []
    total_bytes = 0
    for source in sorted(unit.files):
        raw = read_bytes(paths.root, source)
        total_bytes += len(raw)
        if total_bytes > 128 * 1024 * 1024:
            raise CompilerSemanticError(
                "typescript.source-budget: context exceeds 128 MiB; structural facts remain available"
            )
        sources.append(
            {
                "path": source.relative_to(paths.root).as_posix(),
                "text": raw.decode("utf-8", "replace"),
            }
        )
    request = {
        "root": str(paths.root),
        "unitId": unit.id,
        "contextDigest": unit.context_digest,
        "analysisContextId": unit.analysis_context_id,
        "typescriptModule": str(library.resolve()),
        "configFile": str(unit.metadata.get("tsconfigFile") or ""),
        "files": sources,
    }
    with tempfile.TemporaryDirectory(prefix="modus-typescript-") as temporary:
        request_file = Path(temporary) / "request.json"
        request_file.write_text(json.dumps(request), encoding="utf-8")
        completed = _run_process(
            [
                executable,
                "--max-old-space-size=1024",
                str(_asset("modus_typescript_analyzer.cjs")),
                str(request_file),
            ],
            cwd=unit.root,
            timeout=180,
        )
    if completed.returncode:
        raise CompilerSemanticError(
            "typescript.provider-failed: " + completed.stderr.strip()[-2000:]
        )
    try:
        return parse_jsonl(completed.stdout.splitlines())
    except (ValueError, TypeError, KeyError) as exc:
        raise CompilerSemanticError("typescript.invalid-provider-output") from exc
