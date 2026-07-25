from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    output: str


@dataclass(frozen=True)
class UpdateResult:
    changed: bool
    branch: str
    before: str
    after: str
    summary: str


class UpdateError(RuntimeError):
    pass


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run(args: list[str], cwd: Path) -> CommandResult:
    completed = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    output = (completed.stdout or "").strip()
    return CommandResult(" ".join(args), completed.returncode, output)


def _checked(args: list[str], cwd: Path) -> str:
    result = _run(args, cwd)
    if result.returncode != 0:
        raise UpdateError(f"`{result.command}` falhou:\n{result.output}")
    return result.output


def _current_branch(cwd: Path) -> str:
    branch = _checked(["git", "branch", "--show-current"], cwd).strip()
    if not branch:
        raise UpdateError("Nao consegui detectar a branch atual do Git.")
    return branch


def _current_commit(cwd: Path) -> str:
    return _checked(["git", "rev-parse", "--short", "HEAD"], cwd).strip()


def _ensure_clean_worktree(cwd: Path) -> None:
    status = _checked(["git", "status", "--porcelain"], cwd).strip()
    if status:
        raise UpdateError(
            "Atualizacao bloqueada: existem arquivos modificados no servidor.\n"
            "Resolva ou commite as mudancas antes de usar /atualizar_bot."
        )


def update_from_git(remote: str = "origin", branch: str = "") -> UpdateResult:
    cwd = project_root()
    if not (cwd / ".git").exists():
        raise UpdateError("Esta pasta nao parece ser um repositorio Git.")

    _ensure_clean_worktree(cwd)
    active_branch = branch or _current_branch(cwd)
    before = _current_commit(cwd)

    _checked(["git", "fetch", remote, active_branch], cwd)
    remote_ref = f"{remote}/{active_branch}"
    remote_commit = _checked(["git", "rev-parse", "--short", remote_ref], cwd).strip()

    if before == remote_commit:
        return UpdateResult(
            changed=False,
            branch=active_branch,
            before=before,
            after=before,
            summary="O bot ja esta na versao mais recente.",
        )

    _checked(["git", "pull", "--ff-only", remote, active_branch], cwd)

    requirements = cwd / "requirements.txt"
    if requirements.exists():
        _checked([sys.executable, "-m", "pip", "install", "-r", str(requirements)], cwd)

    after = _current_commit(cwd)
    return UpdateResult(
        changed=True,
        branch=active_branch,
        before=before,
        after=after,
        summary="Atualizacao aplicada. O bot sera reiniciado agora.",
    )


def restart_process() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])
