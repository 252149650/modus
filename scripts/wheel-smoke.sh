#!/usr/bin/env bash
# 构建两种 Python 安装包，再验证公开契约。

set -Eeuo pipefail
shopt -s nullglob

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIRECTORY
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/.." && pwd -P)"
readonly REPOSITORY_ROOT
readonly UV_BIN="${UV_BIN:-uv}"
ARTIFACT_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/modus-package-smoke.XXXXXX")"
readonly ARTIFACT_DIRECTORY

cleanup_artifacts() {
    local exit_code=$?
    if [[ "${MODUS_KEEP_SMOKE_ARTIFACTS:-0}" == "1" ]]; then
        printf '构建产物保留于 %s\n' "${ARTIFACT_DIRECTORY}"
    else
        rm -rf -- "${ARTIFACT_DIRECTORY}"
    fi
    return "${exit_code}"
}

fail() {
    printf '发布验证： %s\n' "$*" >&2
    exit 1
}

trap cleanup_artifacts EXIT

cd -- "${REPOSITORY_ROOT}"

printf '==> 构建 wheel 与源码包\n'
"${UV_BIN}" build --out-dir "${ARTIFACT_DIRECTORY}" .

wheel_artifacts=("${ARTIFACT_DIRECTORY}"/*.whl)
sdist_artifacts=("${ARTIFACT_DIRECTORY}"/*.tar.gz)
(( ${#wheel_artifacts[@]} == 1 )) || fail "以下目录应恰好包含一个 wheel： ${ARTIFACT_DIRECTORY}"
(( ${#sdist_artifacts[@]} == 1 )) || fail \
    "以下目录应恰好包含一个源码包： ${ARTIFACT_DIRECTORY}"

printf '==> 核验归档与安装后的运行契约\n'
"${UV_BIN}" run --locked python scripts/package_smoke.py \
    -- \
    "${wheel_artifacts[0]}" \
    "${sdist_artifacts[0]}"

printf '==> 发布冒烟检查通过\n'
