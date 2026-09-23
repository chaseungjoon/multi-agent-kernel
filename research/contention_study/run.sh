#!/usr/bin/env bash
# Thin launcher: puts the study package and the read-only kernel on PYTHONPATH
# and runs a mining module inside the isolated research venv.
#
#   ./run.sh mining.fetch_prs django/django
#
# The per-repository data under data/ is too large for git, so it is published
# as a GitHub release asset. On first use it is downloaded and extracted into
# data/; once present, it is never fetched again.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

data_url="https://github.com/chaseungjoon/multi-agent-kernel/releases/download/contention-study-data-v1/contention-study-data.tar.gz"
data_sha256="395bc181b3d586a7c597ba84cf5f88c70a85188cb5b4eb953e59d092c6fdb925"
data_repos=(
    apache__airflow
    django__django
    home-assistant__core
    huggingface__transformers
    pandas-dev__pandas
    scikit-learn__scikit-learn
)

sha256_of() {
    if command -v sha256sum >/dev/null; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

ensure_data() {
    local repo missing=0
    for repo in "${data_repos[@]}"; do
        [[ -d "${here}/data/${repo}" ]] || missing=1
    done
    [[ ${missing} -eq 0 ]] && return 0

    # Global, not local: the EXIT trap fires after this function has returned.
    archive="$(mktemp "${TMPDIR:-/tmp}/contention-study-data.XXXXXX")"
    trap 'rm -f "${archive}"' EXIT
    echo "run.sh: data/ is incomplete; downloading the study data (~32 MB)..." >&2
    curl -fL --progress-bar -o "${archive}" "${data_url}"
    if [[ "$(sha256_of "${archive}")" != "${data_sha256}" ]]; then
        echo "run.sh: checksum mismatch for ${data_url}; refusing to extract." >&2
        exit 1
    fi
    # Extract missing repositories only, so a partially present data/ keeps its
    # local caches.
    local wanted=()
    for repo in "${data_repos[@]}"; do
        [[ -d "${here}/data/${repo}" ]] || wanted+=("${repo}")
    done
    mkdir -p "${here}/data"
    tar -xzf "${archive}" -C "${here}/data" "${wanted[@]}"
    rm -f "${archive}"
    echo "run.sh: extracted ${wanted[*]} into data/." >&2
}

ensure_data
export PYTHONPATH="${here}:${here}/../.."
exec "${here}/.venv/bin/python" -m "$@"
