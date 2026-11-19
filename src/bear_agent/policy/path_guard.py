"""Workspace path containment and sensitive-file protection.

This is an execution-layer control, not a prompt request. It resolves symlinks
and non-existent tails, rejects escapes, and refuses credential material before
any file tool touches disk.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from bear_agent.core.errors import PolicyError


class PathAccessError(PolicyError):
    pass


# Patterns are matched against a normalized POSIX-style path. They are
# deliberately narrow enough not to hit normal source files such as
# ``tokenizer.py`` or ``secretary.ts``.
_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        ".env",
        re.compile(r"(^|/)\.env(\.(local|production|prod|staging|development|dev|test|ci))*$"),
    ),
    (
        "credentials",
        re.compile(r"(^|/)credentials(\.(json|ya?ml|xml|ini|conf|config))?$"),
    ),
    (
        "ssh private key",
        re.compile(r"(^|/)id_(rsa|ed25519|ecdsa|dsa)$"),
    ),
    ("private key", re.compile(r"\.(pem|key)$")),
    ("package credentials", re.compile(r"(^|/)\.npmrc$")),
    ("netrc", re.compile(r"(^|/)(\.netrc|\.git-credentials)$")),
    (
        "secrets/tokens",
        re.compile(
            r"(^|/)(secrets?|tokens?|auth[-_]?tokens?)"
            r"\.(json|ya?ml|ini|env|txt)$"
        ),
    ),
)
_WHITELIST = re.compile(r"\.env\.(example|template|sample)$")


def normalize_for_match(path: str) -> str:
    value = path.replace("\\", "/").lower()
    # Preserve a Windows drive prefix; strip NTFS alternate data streams
    # (``.env::$DATA`` opens ``.env``) from the remainder of the path.
    drive = value[:2] if len(value) >= 2 and value[1] == ":" and value[0].isalpha() else ""
    rest = value[len(drive) :]
    rest = rest.split(":", 1)[0]
    rest = rest.rstrip(" .")
    return drive + rest


def is_sensitive_path(path: str) -> tuple[bool, str | None]:
    normalized = normalize_for_match(path)
    name = PurePosixPath(normalized).name
    if _WHITELIST.search(name):
        return False, None
    for label, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(normalized):
            return True, label
    return False, None


class PathGuard:
    def __init__(
        self,
        cwd: str | Path,
        *,
        additional_read_roots: tuple[str | Path, ...] = (),
        additional_write_roots: tuple[str | Path, ...] = (),
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.read_roots = (self.cwd, *(Path(p).resolve() for p in additional_read_roots))
        self.write_roots = (self.cwd, *(Path(p).resolve() for p in additional_write_roots))

    def resolve(self, raw_path: str, *, mode: str = "read") -> Path:
        if not raw_path or not raw_path.strip():
            raise PathAccessError("path must not be empty")
        candidate = Path(os.path.expanduser(raw_path))
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        resolved = candidate.resolve(strict=False)
        canonical = _canonicalize(resolved)

        sensitive, label = is_sensitive_path(raw_path)
        if not sensitive:
            sensitive, label = is_sensitive_path(str(resolved))
        if not sensitive:
            sensitive, label = is_sensitive_path(str(canonical))
        if sensitive:
            raise PathAccessError(
                f"sensitive file blocked ({label}): {raw_path}. "
                "Reading or writing credentials and private keys is not permitted."
            )

        roots = self.write_roots if mode == "write" else self.read_roots
        if not any(_is_within(canonical, root) for root in roots):
            raise PathAccessError(
                f"path outside workspace: {raw_path} (workspace: {self.cwd}). "
                "Use a path under the workspace or add an explicit root in config."
            )
        if mode == "write" and _inside_git_dir(canonical, self.cwd):
            raise PathAccessError(
                "writing inside .git is blocked; use git commands through the harness"
            )
        return canonical

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.cwd))
        except ValueError:
            return str(path)


def _canonicalize(path: Path) -> Path:
    """Resolve symlinks, including the nearest existing ancestor for new files."""
    current = path
    tail: list[str] = []
    while True:
        try:
            real = current.resolve(strict=True)
            return real.joinpath(*reversed(tail)) if tail else real
        except FileNotFoundError:
            if current == current.parent:
                return path
            tail.append(current.name)
            current = current.parent
        except OSError:
            return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _inside_git_dir(path: Path, cwd: Path) -> bool:
    try:
        relative = path.relative_to(cwd)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0].lower() == ".git"
