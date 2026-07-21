#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'
umask 077

if (( $# > 2 )); then
  printf 'usage: %s [git-revision] [staging-parent]\n' "$0" >&2
  exit 2
fi

revision="${1:-HEAD}"
staging_parent_input="${2:-${TMPDIR:-/tmp}}"

# Treat the requested revision and both Skill roots as the only project inputs.
# In particular, caller-local Git/uv routing must not redirect validation to a
# different repository, worktree, project, configuration, or object store.
while IFS= read -r environment_name; do
  case "$environment_name" in
    GIT_*|UV_*) unset "$environment_name" ;;
  esac
done < <(compgen -e)
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_NO_REPLACE_OBJECTS=1
export GIT_OPTIONAL_LOCKS=0
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV PYTEST_ADDOPTS NODE_OPTIONS
unset PAPER_READER_DOCS_ISOLATION_CHILD PAPER_READER_TEST_ROOT
unset ZOTERO_PAPER_READER_CDP_BASE_URL
unset ZOTERO_PAPER_READER_CDP_WS_ENDPOINT ZOTERO_PAPER_READER_CDP_HTTP_BASE_URL

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(git -C "$script_dir/.." rev-parse --show-toplevel)"
repo_root="$(CDPATH= cd -- "$repo_root" && pwd -P)"

staging_parent_needs_create=false
if [[ ! -e "$staging_parent_input" ]]; then
  staging_parent_parent="$(dirname -- "$staging_parent_input")"
  staging_parent_leaf="$(basename -- "$staging_parent_input")"
  if [[ ! -d "$staging_parent_parent" || "$staging_parent_leaf" == '.' || "$staging_parent_leaf" == '..' ]]; then
    printf 'parent of staging parent must be an existing directory: %s\n' "$staging_parent_parent" >&2
    exit 2
  fi
  staging_parent_parent="$(CDPATH= cd -- "$staging_parent_parent" && pwd -P)"
  staging_parent="$staging_parent_parent/$staging_parent_leaf"
  staging_parent_needs_create=true
elif [[ ! -d "$staging_parent_input" ]]; then
  printf 'staging parent must be a directory: %s\n' "$staging_parent_input" >&2
  exit 2
else
  staging_parent="$(CDPATH= cd -- "$staging_parent_input" && pwd -P)"
fi

is_same_or_descendant() {
  local candidate="$1"
  local protected_root="$2"
  [[ "$candidate" == "$protected_root" || "$candidate" == "$protected_root/"* ]]
}

# A linked worktree and the shared object/configuration store are both live
# repository state. A release gate must never place disposable staging data in
# any of them, even when invoked from a different linked worktree.
git -C "$repo_root" worktree list --porcelain -z >/dev/null
while IFS= read -r -d '' worktree_field; do
  case "$worktree_field" in
    'worktree '*)
      worktree_path="${worktree_field#worktree }"
      if [[ -d "$worktree_path" ]]; then
        worktree_root="$(CDPATH= cd -- "$worktree_path" && pwd -P)"
        if is_same_or_descendant "$staging_parent" "$worktree_root"; then
          printf 'staging parent must be outside every linked worktree: %s\n' "$staging_parent" >&2
          exit 2
        fi
      fi
      ;;
  esac
done < <(git -C "$repo_root" worktree list --porcelain -z)

git_common_dir_input="$(git -C "$repo_root" rev-parse --git-common-dir)"
case "$git_common_dir_input" in
  /*) ;;
  *) git_common_dir_input="$repo_root/$git_common_dir_input" ;;
esac
git_common_dir="$(CDPATH= cd -- "$git_common_dir_input" && pwd -P)"
if is_same_or_descendant "$staging_parent" "$git_common_dir"; then
  printf 'staging parent must be outside the shared git common directory: %s\n' "$staging_parent" >&2
  exit 2
fi

if [[ "$staging_parent_needs_create" == true ]]; then
  mkdir -m 700 -- "$staging_parent"
  staging_parent="$(CDPATH= cd -- "$staging_parent" && pwd -P)"
fi

commit="$(git -C "$repo_root" rev-parse --verify --end-of-options "${revision}^{commit}")"
if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'revision did not resolve to one exact commit: %s\n' "$revision" >&2
  exit 2
fi
git -C "$repo_root" cat-file -e "${commit}:paper_reader"
git -C "$repo_root" cat-file -e "${commit}:paper_reader_batch"

short_commit="${commit:0:12}"
staging_root="$(mktemp -d "$staging_parent/paper-reader-release.${short_commit}.XXXXXX")"
chmod 700 "$staging_root"

release_reader="$staging_root/release/paper_reader"
release_batch="$staging_root/release/paper_reader_batch"
install_reader="$staging_root/install/paper_reader"
install_batch="$staging_root/install/paper_reader_batch"
logs="$staging_root/logs"
mkdir -p "$release_reader" "$release_batch" "$install_reader" "$install_batch" "$logs"

run_logged() {
  local label="$1"
  local log_path="$2"
  shift 2
  printf 'START %s\n' "$label"
  if "$@" >"$log_path" 2>&1; then
    printf 'PASS  %s\n' "$label"
    return 0
  fi
  printf 'FAIL  %s (log: %s)\n' "$label" "$log_path" >&2
  printf 'STAGING_ROOT=%s\n' "$staging_root" >&2
  return 1
}

run_uv_in_dir() {
  local label="$1"
  local directory="$2"
  local log_path="$3"
  shift 3
  printf 'START %s\n' "$label"
  if uv --directory "$directory" --project "$directory" --no-config "$@" \
    >"$log_path" 2>&1; then
    printf 'PASS  %s\n' "$label"
    return 0
  fi
  printf 'FAIL  %s (log: %s)\n' "$label" "$log_path" >&2
  printf 'STAGING_ROOT=%s\n' "$staging_root" >&2
  return 1
}

archive_skill() {
  local skill_name="$1"
  local destination="$2"
  local log_path="$3"
  printf 'START archive %s\n' "$skill_name"
  if (git -C "$repo_root" archive --format=tar "${commit}:${skill_name}" | tar -xf - -C "$destination") \
    >"$log_path" 2>&1; then
    printf 'PASS  archive %s\n' "$skill_name"
    return 0
  fi
  printf 'FAIL  archive %s (log: %s)\n' "$skill_name" "$log_path" >&2
  printf 'STAGING_ROOT=%s\n' "$staging_root" >&2
  return 1
}

printf 'COMMIT=%s\n' "$commit"
archive_skill paper_reader "$release_reader" "$logs/archive-paper_reader.log"
archive_skill paper_reader_batch "$release_batch" "$logs/archive-paper_reader_batch.log"

run_logged \
  'paper_reader pre-sync release validation' \
  "$logs/release-paper_reader-validator.log" \
  uv --directory "$release_reader" --no-config \
    run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle
run_logged \
  'paper_reader_batch pre-sync release validation' \
  "$logs/release-paper_reader_batch-validator.log" \
  uv --directory "$release_batch" --no-config \
    run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle

run_logged \
  'copy validated release roots into fresh install roots' \
  "$logs/copy-install-roots.log" \
  cp -R "$release_reader/." "$install_reader"
run_logged \
  'copy validated Batch release root into fresh install root' \
  "$logs/copy-install-batch.log" \
  cp -R "$release_batch/." "$install_batch"

run_uv_in_dir 'paper_reader sync' "$install_reader" "$logs/install-paper_reader-sync.log" sync --locked --python 3.13
run_uv_in_dir 'paper_reader full pytest' "$install_reader" "$logs/install-paper_reader-pytest.log" run pytest
run_uv_in_dir 'paper_reader version' "$install_reader" "$logs/install-paper_reader-version.log" run paper_reader --version
run_uv_in_dir 'paper_reader help' "$install_reader" "$logs/install-paper_reader-help.log" run paper_reader --help
run_uv_in_dir \
  'paper_reader minimal PDF smoke' \
  "$install_reader" \
  "$logs/install-paper_reader-smoke.log" \
  run paper_reader maintenance extract-pdf tests/fixtures/minimal.pdf
run_uv_in_dir \
  'paper_reader installed validator' \
  "$install_reader" \
  "$logs/install-paper_reader-validator.log" \
  run python scripts/validate-skill.py .
run_uv_in_dir 'paper_reader build' "$install_reader" "$logs/install-paper_reader-build.log" build

run_uv_in_dir 'paper_reader_batch sync' "$install_batch" "$logs/install-paper_reader_batch-sync.log" sync --locked --python 3.13
run_logged \
  'paper_reader_batch full pytest' \
  "$logs/install-paper_reader_batch-pytest.log" \
  env "PAPER_READER_TEST_ROOT=$install_reader" \
    uv --directory "$install_batch" --project "$install_batch" --no-config run pytest
run_uv_in_dir \
  'paper_reader_batch version' \
  "$install_batch" \
  "$logs/install-paper_reader_batch-version.log" \
  run paper_reader_batch --version
run_uv_in_dir \
  'paper_reader_batch help' \
  "$install_batch" \
  "$logs/install-paper_reader_batch-help.log" \
  run paper_reader_batch --help
run_uv_in_dir \
  'paper_reader_batch installed validator' \
  "$install_batch" \
  "$logs/install-paper_reader_batch-validator.log" \
  run python scripts/validate-skill.py .
run_uv_in_dir 'paper_reader_batch build' "$install_batch" "$logs/install-paper_reader_batch-build.log" build

printf 'PASS  committed release bundles\n'
printf 'STAGING_ROOT=%s\n' "$staging_root"
