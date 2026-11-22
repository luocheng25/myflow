"""Git helpers: worktrees per lane, attempt snapshots, scope and oracle (protected file) checks."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

from myflow.core import MfError, sha16

DEFAULT_IGNORE = ["__pycache__/**", "**/__pycache__/**", "*.pyc", "*.so", "*.o", "build/**", ".myflow*",
                  "*.egg-info/**"]


def git(args, cwd, check=True) -> str:
    # core.quotePath=false: non-ASCII paths (e.g. docs/设计.md) come back verbatim instead of "\350..."
    r = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise MfError(f"git {' '.join(args)} failed in {cwd}:\n{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def rev_parse(repo, ref: str) -> str:
    return git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo)


def is_repo(path) -> bool:
    return subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(path), capture_output=True).returncode == 0


def ensure_worktree(repo, path: Path, start: str, branch=None) -> Path:
    """Create (or reuse) a worktree. branch=None -> detached at `start`."""
    path = Path(path)
    if (path / ".git").exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch is None:
        git(["worktree", "add", "--detach", str(path), start], repo)
    elif git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False):
        git(["worktree", "add", str(path), branch], repo)
    else:
        git(["worktree", "add", "-b", branch, str(path), start], repo)
    return path


def reset_detached(wt: Path, commit: str, clean: str = "fd") -> None:
    git(["checkout", "-q", "--detach", "--force", commit], wt)
    git(["reset", "-q", "--hard", commit], wt)
    git(["clean", "-q", f"-{clean}"], wt)


def match(path: str, patterns) -> bool:
    for p in patterns or []:
        if p.endswith("/") and path.startswith(p):
            return True
        if fnmatch.fnmatchcase(path, p) or path == p:
            return True
        if p.endswith("/**") and path.startswith(p[:-2]):
            return True
    return False


def _untracked(wt) -> list:
    return [x for x in git(["ls-files", "--others", "--exclude-standard"], wt).splitlines() if x]


def changed_files(wt, base: str) -> list:
    """Files differing from `base`: committed + staged + unstaged + untracked (not ignored)."""
    files = {x for x in git(["diff", "--name-only", base], wt).splitlines() if x}
    files.update(_untracked(wt))
    return sorted(files)


def uncommitted_files(wt) -> list:
    files = {x for x in git(["diff", "--name-only", "HEAD"], wt).splitlines() if x}
    files.update(_untracked(wt))
    return sorted(files)


def scope_violations(files, allow, protect, ignore) -> list:
    bad = []
    for f in files:
        if match(f, ignore):
            continue
        if protect and match(f, protect):
            bad.append(f"{f} (protected)")
        elif allow and not match(f, allow):
            bad.append(f"{f} (outside scope.allow)")
    return bad


def _identity(wt) -> list:
    if git(["config", "user.email"], wt, check=False):
        return []
    return ["-c", "user.name=myflow", "-c", "user.email=myflow@localhost"]


def snapshot(wt, allow, ignore, message: str, protect=()) -> str:
    """Commit in-scope uncommitted changes (only those; never protected files) and return HEAD."""
    files = [f for f in uncommitted_files(wt)
             if not match(f, ignore) and not match(f, protect) and (not allow or match(f, allow))]
    if files:
        git(["add", "-A", "--", *files], wt)
        if git(["diff", "--cached", "--name-only"], wt):
            git([*_identity(wt), "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message], wt)
    return git(["rev-parse", "HEAD"], wt)


def merge(wt, commit: str, message: str) -> None:
    r = subprocess.run(["git", *_identity(wt), "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-edit",
                        "-m", message, commit], cwd=str(wt), capture_output=True, text=True)
    if r.returncode != 0:
        raise MfError(f"merge of {commit[:10]} conflicted in {wt}.\n{(r.stdout + r.stderr).strip()}\n"
                      "Resolve the conflict in that worktree (git add + git commit), then continue with `mf try`.")


def tree_blobs(repo, commit: str, globs) -> dict:
    out = {}
    for line in git(["ls-tree", "-r", commit], repo).splitlines():
        meta, path = line.split("\t", 1)
        if match(path, globs):
            out[path] = meta.split()[2]
    return out


def protect_problems(wt, frozen: dict, globs) -> list:
    """Compare protected files in a worktree with the blob ids frozen at `mf freeze`."""
    problems = []
    present = [p for p in frozen if (Path(wt) / p).is_file()]
    cur = dict(zip(present, git(["hash-object", "--", *present], wt).splitlines())) if present else {}
    for p, sha in frozen.items():
        if p not in cur:
            problems.append(f"{p}: deleted")
        elif cur[p] != sha:
            problems.append(f"{p}: modified")
    listed = git(["ls-files", "--cached", "--others", "--exclude-standard"], wt).splitlines()
    problems.extend(f"{p}: new file matching scope.protect" for p in listed if match(p, globs) and p not in frozen)
    return problems


def diffstat(wt, base: str, head: str = "HEAD") -> dict:
    s = git(["diff", "--shortstat", base, head], wt)
    nums = {k: 0 for k in ("files", "ins", "del")}
    for n, word in re.findall(r"(\d+) (file|insertion|deletion)", s):
        nums[{"file": "files", "insertion": "ins", "deletion": "del"}[word]] = int(n)
    nums["names"] = [x for x in git(["diff", "--name-only", base, head], wt).splitlines() if x][:30]
    return nums


def write_patch(wt, base: str, head: str, out: Path) -> None:
    out.write_text(git(["diff", base, head], wt) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- milestones


def ref_sha(repo, ref: str):
    return git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False) or None


def is_ancestor(repo, a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=str(repo), capture_output=True).returncode == 0


def ls_tree(repo, treeish: str) -> list:
    out = []
    for line in git(["ls-tree", "-r", "--full-tree", treeish], repo).splitlines():
        meta, path = line.split("\t", 1)
        out.append((path, meta.split()[2]))
    return out


def runtime_tree_hash(repo, treeish: str, excluded) -> str:
    """Hash of everything that can change what runs: the tree minus doc/test files (`excluded(path) -> bool`)."""
    return sha16("\n".join(f"{blob} {path}" for path, blob in ls_tree(repo, treeish) if not excluded(path)))


def stage_worktree(wt, ignore) -> None:
    """Index := HEAD + current (non-ignored) worktree changes, so reverted edits really disappear."""
    git(["read-tree", "HEAD"], wt)
    files = [f for f in uncommitted_files(wt) if not match(f, ignore)]
    if files:
        git(["add", "-A", "--", *files], wt)


def write_tree(wt) -> str:
    return git(["write-tree"], wt)


def tree_diff_names(repo, a: str, b: str) -> list:
    return [x for x in git(["diff-tree", "-r", "--name-only", "--no-renames", a, b], repo).splitlines() if x]


def numstat(repo, a: str, b: str) -> list:
    out = []
    for line in git(["diff", "--numstat", "--no-renames", a, b], repo).splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out.append((int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 0, parts[2]))
    return out


def commit_tree(wt, tree: str, parent: str, message: str, cfg=(), also=()) -> str:
    """A commit object for `tree` (nothing moves); `also` adds merge parents."""
    parents = [x for p in ([parent] if parent else []) + list(also) for x in ("-p", p)]
    return git([*_identity(wt), *cfg, "commit-tree", tree, *parents, "-m", message], wt)


def update_branch(repo, branch: str, new: str, old=None) -> None:
    git(["update-ref", f"refs/heads/{branch}", new, *([old] if old else [])], repo)


def make_tag(wt, tag: str, commit: str, message: str, cfg=()) -> None:
    git([*_identity(wt), *cfg, "tag", "-a", "-m", message, tag, commit], wt)


def delete_tag(repo, tag: str) -> None:
    git(["tag", "-d", tag], repo, check=False)


def valid_branch(name: str) -> bool:
    return bool(name) and not name.startswith("-") and subprocess.run(
        ["git", "check-ref-format", "--branch", name], capture_output=True).returncode == 0


def remote_head(repo, url: str, branch: str):
    out = git(["ls-remote", url, f"refs/heads/{branch}"], repo)
    return out.split()[0] if out.strip() else None


def remote_tag(repo, url: str, tag: str):
    """The remote tag's own object id (an annotated tag object, not the commit it points to), or None."""
    out = git(["ls-remote", url, f"refs/tags/{tag}"], repo)
    return out.split()[0] if out.strip() else None


def push_atomic(repo, url: str, refspecs: list, leases=()) -> tuple:
    """leases: (ref, expected object id): that remote ref may be overwritten only while it still holds exactly that."""
    lease = [f"--force-with-lease={ref}:{old}" for ref, old in leases]
    r = subprocess.run(["git", "push", "--atomic", "--porcelain", *lease, url, *refspecs], cwd=str(repo),
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def github_web(url: str):
    m = re.match(r"(?:https?://(?:[^@/]+@)?|git@|ssh://git@)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None
