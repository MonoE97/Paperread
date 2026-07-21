from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-committed-release-bundles.sh"


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_release_gate_clears_project_redirecting_environment(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace_path = tmp_path / "uv-trace.json"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

names = json.loads(os.environ["RELEASE_GATE_TEST_ENV_NAMES"])
payload = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "present": {name: os.environ[name] for name in names if name in os.environ},
    "safe_git": {
        name: os.environ.get(name)
        for name in json.loads(os.environ["RELEASE_GATE_TEST_SAFE_GIT_NAMES"])
    },
}
with open(os.environ["RELEASE_GATE_TEST_TRACE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, sort_keys=True) + "\\n")
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    poisoned_names = [
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_FUTURE_REDIRECT",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
        "UV_BUILD_CONSTRAINT",
        "UV_CACHE_DIR",
        "UV_CONFIG_FILE",
        "UV_CONSTRAINT",
        "UV_DEFAULT_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "UV_FUTURE_REDIRECT",
        "UV_INDEX",
        "UV_INDEX_STRATEGY",
        "UV_INDEX_URL",
        "UV_PYTHON",
        "UV_PYTHON_DOWNLOADS",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_PREFERENCE",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "UV_TOOL_DIR",
        "UV_WORKING_DIR",
        "ZOTERO_PAPER_READER_CDP_BASE_URL",
        "ZOTERO_PAPER_READER_CDP_HTTP_BASE_URL",
        "ZOTERO_PAPER_READER_CDP_WS_ENDPOINT",
    ]
    safe_git_environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    environment = os.environ.copy()
    environment.update({name: str(tmp_path / "poison") for name in poisoned_names})
    environment.update(
        {name: str(tmp_path / "poison") for name in safe_git_environment}
    )
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["RELEASE_GATE_TEST_ENV_NAMES"] = json.dumps(poisoned_names)
    environment["RELEASE_GATE_TEST_SAFE_GIT_NAMES"] = json.dumps(
        list(safe_git_environment)
    )
    environment["RELEASE_GATE_TEST_TRACE"] = str(trace_path)

    result = subprocess.run(
        [str(SCRIPT), "HEAD", str(tmp_path / "staging")],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert trace_path.is_file(), result.stderr
    traces = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(traces) == 15
    assert all(trace["present"] == {} for trace in traces)
    assert all(trace["safe_git"] == safe_git_environment for trace in traces)
    staging_root = Path(
        next(
            line.removeprefix("STAGING_ROOT=")
            for line in result.stdout.splitlines()
            if line.startswith("STAGING_ROOT=")
        )
    )
    release_reader = staging_root / "release/paper_reader"
    release_batch = staging_root / "release/paper_reader_batch"
    install_reader = staging_root / "install/paper_reader"
    install_batch = staging_root / "install/paper_reader_batch"

    expected_argv = [
        [
            "--directory",
            str(release_reader),
            "--no-config",
            "run",
            "--no-project",
            "--python",
            "3.13",
            "python",
            "scripts/validate-skill.py",
            ".",
            "--release-bundle",
        ],
        [
            "--directory",
            str(release_batch),
            "--no-config",
            "run",
            "--no-project",
            "--python",
            "3.13",
            "python",
            "scripts/validate-skill.py",
            ".",
            "--release-bundle",
        ],
        [
            "--directory",
            str(install_reader),
            "--project",
            str(install_reader),
            "--no-config",
            "sync",
            "--locked",
            "--python",
            "3.13",
        ],
        [
            "--directory",
            str(install_reader),
            "--project",
            str(install_reader),
            "--no-config",
            "run",
            "pytest",
        ],
        [
            "--directory",
            str(install_reader),
            "--project",
            str(install_reader),
            "--no-config",
            "run",
            "paper_reader",
            "--version",
        ],
        [
            "--directory",
            str(install_reader),
            "--project",
            str(install_reader),
            "--no-config",
            "run",
            "paper_reader",
            "--help",
        ],
        [
            "--directory",
            str(install_reader),
            "--project",
            str(install_reader),
            "--no-config",
            "run",
            "paper_reader",
            "maintenance",
            "extract-pdf",
            "tests/fixtures/minimal.pdf",
        ],
        [
            "--directory",
            str(install_reader),
            "--project",
            str(install_reader),
            "--no-config",
            "run",
            "python",
            "scripts/validate-skill.py",
            ".",
        ],
        [
            "--directory",
            str(install_reader),
            "--project",
            str(install_reader),
            "--no-config",
            "build",
        ],
        [
            "--directory",
            str(install_batch),
            "--project",
            str(install_batch),
            "--no-config",
            "sync",
            "--locked",
            "--python",
            "3.13",
        ],
        [
            "--directory",
            str(install_batch),
            "--project",
            str(install_batch),
            "--no-config",
            "run",
            "pytest",
        ],
        [
            "--directory",
            str(install_batch),
            "--project",
            str(install_batch),
            "--no-config",
            "run",
            "paper_reader_batch",
            "--version",
        ],
        [
            "--directory",
            str(install_batch),
            "--project",
            str(install_batch),
            "--no-config",
            "run",
            "paper_reader_batch",
            "--help",
        ],
        [
            "--directory",
            str(install_batch),
            "--project",
            str(install_batch),
            "--no-config",
            "run",
            "python",
            "scripts/validate-skill.py",
            ".",
        ],
        [
            "--directory",
            str(install_batch),
            "--project",
            str(install_batch),
            "--no-config",
            "build",
        ],
    ]
    assert [trace["argv"] for trace in traces] == expected_argv


def test_release_gate_rejects_staging_in_another_linked_worktree(
    tmp_path: Path,
) -> None:
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir()
    _run_git("init", "-b", "main", cwd=main_repo)
    (main_repo / "paper_reader").mkdir()
    (main_repo / "paper_reader" / "marker").write_text("reader\n", encoding="utf-8")
    (main_repo / "paper_reader_batch").mkdir()
    (main_repo / "paper_reader_batch" / "marker").write_text(
        "batch\n", encoding="utf-8"
    )
    _run_git("add", "paper_reader", "paper_reader_batch", cwd=main_repo)
    _run_git(
        "-c",
        "user.name=Release Gate Test",
        "-c",
        "user.email=release-gate@example.invalid",
        "commit",
        "-m",
        "fixture",
        cwd=main_repo,
    )

    linked_worktree = tmp_path / "linked-worktree"
    _run_git(
        "worktree",
        "add",
        "-b",
        "release-gate-test",
        str(linked_worktree),
        cwd=main_repo,
    )
    copied_script = linked_worktree / "scripts" / SCRIPT.name
    copied_script.parent.mkdir()
    copied_script.write_bytes(SCRIPT.read_bytes())
    copied_script.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        [str(copied_script), "HEAD", str(main_repo)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 2
    assert "staging parent must be outside every linked worktree" in result.stderr
    assert list(main_repo.glob("paper-reader-release.*")) == []
