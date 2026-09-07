"""La signature d'un échec : ce qui dit qu'une relance répéterait la même chose.

Une tentative `crashed` ou `invalid_result` a droit à sa seconde chance sur
place. Mais si tout ce qui la définit — sa commande, son échec structuré,
le travail durable du workspace et du worktree — est identique à la
précédente, la relance ne fera rien de plus : le noyau l'épargne. La
signature est bornée : un fichier trop grand ou une commande absente rend
la tentative non comparable, et le noyau garde alors sa relance locale.
"""

import hashlib
import json
from pathlib import Path

from .workspace import (EVENT_NAMES, FAILURE_NAME, OUTCOME_NAME, PGID_FILE,
                        PROMPT_NAME, RAIL_PREFIXES, STARVED_NAME, USAGE_NAME,
                        attempt_command, run_workspace)
from .worktree import run_git, run_worktree

SIGNATURE_FILES = 100  # au-delà, conserver la relance plutôt que mal comparer
SIGNATURE_ENTRIES = 256  # borne aussi le parcours des traces ignorées
SIGNATURE_BYTES = 64 * 1024  # borne par commande ou fichier durable
GIT_TIMEOUT_S = 20
STRUCTURED = ("outcome", "error", "timeout", "exit_code", "provider", "reason")


def _file_digest(path: Path) -> str | None:
    """Empreinte bornée d'un fichier durable, ou None s'il est trop grand."""
    try:
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_size > SIGNATURE_BYTES):
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _files_digest(root: Path, names: list[str]) -> str | None:
    """Empreinte stable d'une petite liste de fichiers sous une seule racine."""
    if len(names) > SIGNATURE_FILES:
        return None
    state = []
    for name in sorted(names):
        path = root / name
        if not path.exists():
            state.append((name, None))
            continue
        digest = _file_digest(path)
        if digest is None:
            return None
        state.append((name, digest))
    return hashlib.sha256(json.dumps(state, ensure_ascii=False).encode()).hexdigest()


def _provider_trace(name: str) -> bool:
    """Reconnaît un flux fournisseur actif ou archivé par tentative."""
    return (name in EVENT_NAMES or name in {"codex-errors.log", "opencode-errors.log"}
            or (name.startswith("codex-") and name.endswith(".jsonl"))
            or (name.startswith("opencode-events-") and name.endswith(".jsonl")))


def _durable(path: Path) -> bool:
    """Un fichier du workspace qui compte comme travail — ni prose, ni trace."""
    ignored = {PGID_FILE, OUTCOME_NAME, STARVED_NAME, PROMPT_NAME, USAGE_NAME,
               FAILURE_NAME}
    name = path.name
    return (path.is_file() and name not in ignored and path.suffix != ".md"
            and not _provider_trace(name) and not name.startswith(RAIL_PREFIXES))


def _workspace_digest(workspace: Path) -> str | None:
    """Empreinte bornée du travail durable, sans prose ni traces d'agent.

    Le Markdown du workspace est l'interface humaine des nœuds : critères,
    rapports, verdicts et passations. Le travail durable, documentation
    comprise, vit dans le worktree et reste donc comparé par sa propre
    empreinte. La borne porte sur toutes les entrées parcourues, y compris
    celles que l'on ignore, pour qu'un long historique ne rende pas le coût
    de la comparaison implicite.
    """
    names = []
    try:
        for seen, path in enumerate(workspace.iterdir(), start=1):
            if seen > SIGNATURE_ENTRIES or len(names) > SIGNATURE_FILES:
                return None
            if _durable(path):
                names.append(path.name)
    except OSError:
        return None
    return _files_digest(workspace, names)


def _worktree_digest(tree: Path | None) -> str | list | None:
    """L'état du worktree : sa tête et l'empreinte de ce qui a bougé."""
    if tree is None:
        return "none"
    head_code, head = run_git(tree, "rev-parse", "HEAD", timeout=GIT_TIMEOUT_S)
    diff_code, changed = run_git(tree, "diff", "--name-only", "HEAD",
                                 timeout=GIT_TIMEOUT_S)
    new_code, untracked = run_git(tree, "ls-files", "-o", "--exclude-standard",
                                  timeout=GIT_TIMEOUT_S)
    if head_code or diff_code or new_code:
        return None
    files = sorted(set(changed.splitlines() + untracked.splitlines()))
    content = _files_digest(tree, files)
    return None if content is None else [head, content]


def retry_signature(item_id: int, run: dict, outcome: str) -> str | None:
    """Signature bornée d'un échec, limitée à son item, cycle et candidat.

    Les textes humains complets ne participent pas : ni résumé, ni journal,
    ni rapport. Une commande, un champ d'erreur ou un fichier trop grand rend
    la tentative non comparable ; le noyau garde alors sa relance locale.
    """
    workspace = run_workspace(item_id, run)
    command = _file_digest(attempt_command(workspace, run))
    result = run.get("result") or {}
    structured = {name: result[name] for name in STRUCTURED if name in result}
    encoded = json.dumps(structured, sort_keys=True, ensure_ascii=False).encode()
    if command is None or len(encoded) > SIGNATURE_BYTES:
        return None
    workspace_state = _workspace_digest(workspace)
    worktree_state = _worktree_digest(run_worktree(item_id, run))
    if workspace_state is None or worktree_state is None:
        return None
    signature = [run["node"], outcome, command, structured,
                 workspace_state, worktree_state]
    return hashlib.sha256(json.dumps(
        signature, sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()
