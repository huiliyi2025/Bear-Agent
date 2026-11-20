"""Shell command policy.

This is an execution-layer allow/ask/deny decision, not a prompt suggestion.
It is intentionally conservative: command strings are tokenized, wrappers and
inline interpreters are unwrapped where possible, and anything we cannot
analyze is escalated rather than silently allowed.

It is not a shell parser. The sandbox and approvals remain the primary
containment; the policy prevents obvious destructive and indirect-execution
patterns from reaching them without a decision.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from bear_agent.config.models import ShellConfig
from bear_agent.policy.path_guard import is_sensitive_path


class CommandAction(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class CommandDecision:
    action: CommandAction
    reason: str
    risk: str = "low"
    matched_rules: tuple[str, ...] = ()
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class _Analysis:
    reasons: list[str] = field(default_factory=list)
    hard_denies: list[str] = field(default_factory=list)
    safe: bool = True
    risk: str = "low"


_HARD_DENY: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "recursive-delete-root",
        re.compile(
            r"(?i)\brm\s+(?:-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b[^;|&]*"
            r"(?:^|\s)(?:/\*?|~|\$home|\*|[a-z]:[\\/])(?:\s|$)"
        ),
    ),
    ("mkfs", re.compile(r"(?i)\bmkfs(?:\.[a-z0-9]+)?\b")),
    ("raw-disk-write", re.compile(r"(?i)\bdd\b[^;\n]*\bof\s*=\s*/dev/")),
    ("system-power", re.compile(r"(?i)\b(?:shutdown|reboot|halt|poweroff)\b")),
    ("windows-format", re.compile(r"(?i)\bformat\s+[a-z]:")),
    ("diskpart", re.compile(r"(?i)\bdiskpart\b")),
    ("bcdedit", re.compile(r"(?i)\bbcdedit\b")),
    ("registry-delete", re.compile(r"(?i)\breg\s+delete\b")),
    (
        "git-force-push",
        re.compile(r"(?i)\bgit\s+push\b[^;\n]*(?:--force(?:-with-lease)?|-f)\b"),
    ),
    ("git-hard-reset", re.compile(r"(?i)\bgit\s+reset\s+--hard\b")),
    (
        "git-clean-force",
        re.compile(r"(?i)\bgit\s+clean\s+-[a-z]*[fx][a-z]*\b"),
    ),
    ("sql-destructive", re.compile(r"(?i)\b(?:drop|truncate)\s+table\b")),
    (
        "pipe-to-interpreter",
        re.compile(
            r"(?i)\b(?:curl|wget)\b[^|;\n]*\|\s*"
            r"(?:(?:ba|z|k|fi)?sh|powershell|pwsh|cmd|python[0-9]*|node|perl|ruby|php|iex)\b"
        ),
    ),
    (
        "powershell-iex",
        re.compile(r"(?i)\b(?:invoke-expression|iex)\b"),
    ),
    (
        "powershell-recursive-delete",
        re.compile(r"(?i)\b(?:remove-item|ri)\b(?=[^;\n]*-recurse)(?=[^;\n]*-force)"),
    ),
    (
        "chmod-root",
        re.compile(r"(?i)\bchmod\s+(?:-r\s+)?777\s+/(?:\s|$)"),
    ),
    (
        "chown-root",
        re.compile(r"(?i)\bchown\s+-r\s+\S+\s+/(?:\s|$)"),
    ),
    ("fork-bomb", re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:")),
    (
        "windows-del-root",
        re.compile(r"(?i)\b(?:del|erase|rd|rmdir)\s+(?:/[a-z]+\s+)*[a-z]:[\\/]"),
    ),
)

_SAFE_EXECUTABLES = {
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "which",
    "whereis",
    "whoami",
    "date",
    "uname",
    "echo",
    "printf",
    "true",
    "false",
    "test",
    "[",
    "git",
}

_GIT_SAFE_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse"}

_SHELLS = {"bash", "sh", "zsh", "ksh", "dash", "fish"}
_HARD_DENY_EXECUTABLES = {
    "mkfs",
    "diskpart",
    "bcdedit",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "format",
    "ri",
}
_INTERPRETERS = {"python", "python3", "pypy3", "perl", "ruby", "node", "php"}
_WRAPPERS = {"env", "nohup", "nice", "time", "timeout", "command", "builtin"}
_REDIRECT_RE = re.compile(r"(?:^|\s)(>>?|2>>?|&>)\s*([^\s;|&]+)")


def split_segments(command: str) -> list[str]:
    """Split on shell control operators while respecting quotes."""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            index += 1
            continue
        if char in {";", "\n"}:
            segments.append("".join(current).strip())
            current = []
            index += 1
            continue
        if char in {"&", "|"}:
            # Consume && / || as one separator.
            if index + 1 < len(command) and command[index + 1] == char:
                index += 1
            segments.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    segments.append("".join(current).strip())
    return [segment for segment in segments if segment]


class CommandPolicy:
    def __init__(
        self,
        config: ShellConfig,
        *,
        platform: str | None = None,
        workspace: str | Path = ".",
    ) -> None:
        self.config = config
        self.platform = platform
        self.workspace = Path(workspace).resolve()
        self._extra_denies = [
            (f"config-deny-{index}", re.compile(pattern, re.IGNORECASE))
            for index, pattern in enumerate(config.deny_patterns)
        ]

    def evaluate(self, command: str, *, sandbox_filesystem: bool) -> CommandDecision:
        command = command.strip()
        if not command:
            return CommandDecision(
                CommandAction.DENY, "empty command", risk="low", matched_rules=("empty",)
            )
        analysis = self._analyze(command, sandbox_filesystem, depth=0)
        if analysis.hard_denies:
            hard_denies = tuple(dict.fromkeys(analysis.hard_denies))
            return CommandDecision(
                CommandAction.DENY,
                "blocked by command policy: " + "; ".join(hard_denies),
                risk="critical",
                matched_rules=hard_denies,
            )
        if self._allowlisted(command):
            return CommandDecision(
                CommandAction.ALLOW,
                "command is in the configured allowlist",
                risk=analysis.risk,
                matched_rules=("allowlist",),
            )
        if analysis.safe and not analysis.reasons:
            return CommandDecision(
                CommandAction.ALLOW,
                "read-only command inside the workspace",
                risk="low",
                matched_rules=("safe-read-only",),
            )
        reasons = analysis.reasons or ["command requires approval"]
        return CommandDecision(
            CommandAction.ASK,
            "; ".join(reasons),
            risk=analysis.risk,
            matched_rules=tuple(reasons),
        )

    def _allowlisted(self, command: str) -> bool:
        normalized = " ".join(command.strip().lower().split())
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return False
        if not tokens:
            return False
        executable = Path(tokens[0]).name.lower()
        if executable in {name.lower() for name in self.config.allow_commands}:
            return True
        return any(
            normalized.startswith(prefix.strip().lower())
            for prefix in self.config.allow_prefixes
            if prefix.strip()
        )

    def _analyze(self, command: str, sandbox_filesystem: bool, *, depth: int) -> _Analysis:
        analysis = _Analysis()
        if depth > 4:
            analysis.reasons.append("nested command is too deep to analyze safely")
            analysis.safe = False
            analysis.risk = "high"
            return analysis
        normalized = " ".join(command.lower().split())
        for name, pattern in (*_HARD_DENY, *self._extra_denies):
            if pattern.search(normalized):
                analysis.hard_denies.append(name)
                analysis.risk = "critical"

        if "$(" in command or "`" in command:
            analysis.reasons.append("command substitution is indirect execution")
            analysis.safe = False
            analysis.risk = "high"
        if _REDIRECT_RE.search(command):
            analysis.safe = False
            analysis.reasons.append("output redirection requires approval")
            for match in _REDIRECT_RE.finditer(command):
                target = match.group(2).strip("'\"")
                sensitive, label = is_sensitive_path(target)
                if sensitive:
                    analysis.hard_denies.append(f"sensitive-path:{label}")
                    analysis.risk = "critical"
                    continue
                if _outside_workspace(target, self.workspace):
                    if sandbox_filesystem:
                        analysis.reasons.append(
                            f"redirection target {target!r} is outside the workspace"
                        )
                    else:
                        analysis.hard_denies.append("redirect-outside-workspace")
                        analysis.risk = "critical"

        segments = split_segments(command)
        if len(segments) > 1:
            analysis.safe = False
            analysis.reasons.append("command chaining or piping requires approval")
            self._check_pipe_to_interpreter(segments, analysis)
        for segment in segments:
            self._analyze_segment(segment, sandbox_filesystem, analysis, depth)
        if analysis.reasons:
            analysis.risk = "high" if analysis.risk == "low" else analysis.risk
        return analysis

    def _analyze_segment(
        self,
        segment: str,
        sandbox_filesystem: bool,
        analysis: _Analysis,
        depth: int,
    ) -> None:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            analysis.safe = False
            analysis.reasons.append("command could not be tokenized")
            analysis.risk = "high"
            return
        if not tokens:
            return
        self._analyze_tokens(tokens, sandbox_filesystem, analysis, depth)

    def _analyze_tokens(
        self,
        tokens: list[str],
        sandbox_filesystem: bool,
        analysis: _Analysis,
        depth: int,
    ) -> None:
        while tokens and _is_assignment(tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return
        executable = Path(tokens[0]).name.lower()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        args = tokens[1:]
        self._check_sensitive_args(args, analysis)
        self._check_semantic_dangers(executable, args, analysis)

        if executable in {"sudo", "doas", "su"}:
            analysis.safe = False
            analysis.reasons.append(f"{executable} privilege escalation requires approval")
            analysis.risk = "high"
            self._recurse(args, sandbox_filesystem, analysis, depth)
            return
        if executable in {"eval", "exec", "source"} or executable == ".":
            analysis.safe = False
            analysis.reasons.append(f"{executable} is indirect execution")
            analysis.risk = "high"
            return
        if executable in _WRAPPERS or executable in {"busybox", "toybox"}:
            inner = _strip_wrapper_options(executable, args) if executable in _WRAPPERS else args
            if inner:
                self._recurse(inner, sandbox_filesystem, analysis, depth)
            return
        if executable in _SHELLS:
            if "-c" in args:
                index = args.index("-c")
                if index + 1 < len(args):
                    self._recurse(args[index + 1 :], sandbox_filesystem, analysis, depth + 1)
                    return
            analysis.safe = False
            analysis.reasons.append("interactive shell requires approval")
            analysis.risk = "high"
            return
        if executable in _INTERPRETERS:
            if "--version" in args or "-V" in args:
                return
            analysis.safe = False
            analysis.reasons.append(f"{executable} can execute arbitrary code; approval required")
            analysis.risk = "high"
            return
        if executable in {"powershell", "pwsh"}:
            self._analyze_powershell(args, sandbox_filesystem, analysis, depth)
            return
        if executable in {"cmd", "cmd.exe"}:
            if any(arg.lower() in {"/c", "/k"} for arg in args):
                index = next(i for i, arg in enumerate(args) if arg.lower() in {"/c", "/k"})
                if index + 1 < len(args):
                    self._recurse(args[index + 1 :], sandbox_filesystem, analysis, depth + 1)
                    return
            analysis.safe = False
            analysis.reasons.append("cmd shell requires approval")
            analysis.risk = "high"
            return
        if executable == "xargs":
            inner = _strip_xargs_options(args)
            if inner:
                self._recurse(inner, sandbox_filesystem, analysis, depth + 1)
            return
        if executable == "git":
            if "-c" in args or "--exec-path" in args:
                analysis.safe = False
                analysis.reasons.append("git config/exec overrides require approval")
                analysis.risk = "high"
                return
            subcommand = next((arg for arg in args if not arg.startswith("-")), "")
            if subcommand not in _GIT_SAFE_SUBCOMMANDS:
                analysis.safe = False
                analysis.reasons.append(f"git {subcommand or '(unknown)'} requires approval")
                analysis.risk = "moderate"
                return
            if subcommand == "branch" and any(
                arg in {"-d", "-D", "--delete", "-m", "-M", "--move"} or arg.startswith("--delete")
                for arg in args
            ):
                analysis.safe = False
                analysis.reasons.append("git branch mutation requires approval")
                analysis.risk = "high"
                return
            self._check_paths(args, analysis)
            return

        if executable not in _SAFE_EXECUTABLES:
            analysis.safe = False
            analysis.reasons.append(f"{executable or 'command'} requires approval")
            analysis.risk = "moderate"
            return
        if executable == "find" and any(
            arg in {"-exec", "-execdir", "-delete", "-fprint", "-fls"} for arg in args
        ):
            analysis.safe = False
            analysis.reasons.append("find with side effects requires approval")
            analysis.risk = "high"
            return
        if not sandbox_filesystem and executable in {"grep", "cat", "head", "tail", "wc"}:
            recursive = executable == "grep" and any(
                arg == "--recursive" or (arg.startswith("-") and "r" in arg.lower()) for arg in args
            )
            globbed = any(any(ch in arg for ch in "*?[") for arg in args)
            if recursive or globbed:
                analysis.safe = False
                analysis.reasons.append(
                    "broad workspace read requires approval when no filesystem sandbox is active"
                )
                analysis.risk = "high"
                return
        self._check_paths(args, analysis)

    def _analyze_powershell(
        self,
        args: list[str],
        sandbox_filesystem: bool,
        analysis: _Analysis,
        depth: int,
    ) -> None:
        lowered = [arg.lower() for arg in args]
        for flag in ("-encodedcommand", "-enc"):
            if flag in lowered:
                index = lowered.index(flag)
                if index + 1 >= len(args):
                    analysis.hard_denies.append("powershell-encoded-empty")
                    return
                try:
                    decoded = base64.b64decode(args[index + 1]).decode(
                        "utf-16-le", errors="replace"
                    )
                except (binascii.Error, ValueError):
                    analysis.hard_denies.append("powershell-encoded-unreadable")
                    return
                self._recurse([decoded], sandbox_filesystem, analysis, depth + 1)
                return
        for flag in ("-command", "-c"):
            if flag in lowered:
                index = lowered.index(flag)
                if index + 1 < len(args):
                    self._recurse(args[index + 1 :], sandbox_filesystem, analysis, depth + 1)
                    return
        analysis.safe = False
        analysis.reasons.append("PowerShell execution requires approval")
        analysis.risk = "high"

    def _recurse(
        self,
        tokens: list[str],
        sandbox_filesystem: bool,
        analysis: _Analysis,
        depth: int,
    ) -> None:
        inner = " ".join(tokens)
        nested = self._analyze(inner, sandbox_filesystem, depth=depth)
        analysis.hard_denies.extend(nested.hard_denies)
        analysis.reasons.extend(nested.reasons)
        analysis.safe = analysis.safe and nested.safe and not nested.hard_denies
        if nested.risk in {"high", "critical"}:
            analysis.risk = nested.risk

    def _check_pipe_to_interpreter(self, segments: list[str], analysis: _Analysis) -> None:
        tokenized: list[tuple[list[str], str]] = []
        for segment in segments:
            try:
                tokens = shlex.split(segment, posix=True)
            except ValueError:
                return
            tokenized.append((tokens, _first_executable(tokens)))
        interpreters = (
            _SHELLS
            | _INTERPRETERS
            | {
                "powershell",
                "pwsh",
                "cmd",
                "iex",
                "invoke-expression",
            }
        )
        downloaders = {"curl", "wget", "iwr", "invoke-webrequest"}
        downloader_seen = False
        for tokens, current in tokenized:
            if current in downloaders or (
                current in {"sudo", "doas"} and any(token in downloaders for token in tokens[1:])
            ):
                downloader_seen = True
                continue
            if not downloader_seen:
                continue
            wrapper_prefix = current in {"sudo", "doas", "env", "command", "exec"}
            suspicious = current in interpreters or (
                wrapper_prefix
                and any(token in interpreters for token in tokens[1:] if not token.startswith("-"))
            )
            if suspicious:
                analysis.hard_denies.append("pipe-to-interpreter")
                analysis.risk = "critical"
                break

    def _check_semantic_dangers(
        self, executable: str, args: list[str], analysis: _Analysis
    ) -> None:
        """Catch dangerous forms that survive quoting/escaping.

        The raw-string rules are backed by token-level checks: ``r""m -rf "/"``
        tokenizes to ``rm -rf /`` and must still be a hard deny.
        """
        if executable in _HARD_DENY_EXECUTABLES or executable.startswith("mkfs."):
            analysis.hard_denies.append(f"dangerous-command:{executable}")
            analysis.risk = "critical"
            return
        if executable in {"rm", "del", "erase", "rd", "rmdir"}:
            is_windows = executable in {"del", "erase", "rd", "rmdir"}
            recursive = any(
                (
                    arg.lower() == "/s"
                    if is_windows
                    else arg.lower() == "--recursive"
                    or (arg.startswith("-") and "r" in arg[1:].lower())
                )
                for arg in args
            )
            targets = [
                arg
                for arg in args
                if not (arg.startswith("-") or (is_windows and arg.startswith("/")))
            ]
            if recursive and any(
                _is_root_like_target(target, self.workspace) for target in targets
            ):
                analysis.hard_denies.append("destructive-root-delete")
                analysis.risk = "critical"
        if executable == "find" and "-delete" in args:
            paths = [arg for arg in args if not arg.startswith("-")]
            if any(_is_root_like_target(path, self.workspace) for path in paths[:1]):
                analysis.hard_denies.append("find-root-delete")
                analysis.risk = "critical"
        if executable == "dd":
            for arg in args:
                if arg.lower().startswith("of=") and arg[3:].strip("'").strip('"').startswith(
                    "/dev/"
                ):
                    analysis.hard_denies.append("raw-disk-write")
                    analysis.risk = "critical"
        if executable in {"chmod", "chown"}:
            targets = [arg for arg in args if not arg.startswith("-")]
            root_target = any(_is_root_like_target(arg, self.workspace) for arg in targets)
            recursive = any(
                arg in {"-R", "--recursive"} or (arg.startswith("-") and "r" in arg.lower())
                for arg in args
            )
            if root_target and (recursive or "777" in args):
                analysis.hard_denies.append(f"{executable}-root")
                analysis.risk = "critical"
        if executable == "git":
            lowered = [arg.lower() for arg in args]
            if "push" in lowered and any(
                arg in {"-f", "--force", "--force-with-lease"} for arg in lowered
            ):
                analysis.hard_denies.append("git-force-push")
                analysis.risk = "critical"
            if "reset" in lowered and "--hard" in lowered:
                analysis.hard_denies.append("git-hard-reset")
                analysis.risk = "critical"
            if "clean" in lowered and any(
                arg.startswith("-") and ("f" in arg or "x" in arg) for arg in lowered
            ):
                analysis.hard_denies.append("git-clean-force")
                analysis.risk = "critical"
        if executable == "remove-item":
            lowered = [arg.lower() for arg in args]
            if "-recurse" in lowered and "-force" in lowered:
                analysis.hard_denies.append("powershell-recursive-delete")
                analysis.risk = "critical"
        if executable == "reg" and "delete" in [arg.lower() for arg in args]:
            analysis.hard_denies.append("registry-delete")
            analysis.risk = "critical"

    def _check_sensitive_args(self, args: list[str], analysis: _Analysis) -> None:
        for arg in args:
            candidate = arg.split("=", 1)[-1] if arg.startswith("-") and "=" in arg else arg
            if not candidate or candidate in {"-", "/dev/null", "$null"}:
                continue
            sensitive, label = is_sensitive_path(candidate)
            if not sensitive and not candidate.startswith("-"):
                resolved = (self.workspace / candidate).resolve(strict=False)
                sensitive, label = is_sensitive_path(str(resolved))
            if sensitive:
                analysis.hard_denies.append(f"sensitive-path:{label}")
                analysis.risk = "critical"

    def _check_paths(self, args: list[str], analysis: _Analysis) -> None:
        for arg in args:
            if arg.startswith("-"):
                continue
            if _outside_workspace(arg, self.workspace):
                analysis.safe = False
                analysis.reasons.append(f"path argument {arg!r} is outside the workspace")
                analysis.risk = "high"


def _is_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def _first_executable(tokens: list[str]) -> str:
    while tokens and _is_assignment(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return ""
    executable = Path(tokens[0]).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in _WRAPPERS:
        return _first_executable(_strip_wrapper_options(executable, tokens[1:]))
    return executable


def _is_root_like_target(value: str, workspace: Path) -> bool:
    text = value.strip("'\"")
    lowered = text.lower()
    if not text:
        return False
    if lowered in {
        "/",
        "//",
        "/*",
        "~",
        "~/",
        "$home",
        "${home}",
        "%userprofile%",
    }:
        return True
    if lowered.startswith(("~/", "$home/", "${home}/", "%userprofile%/")):
        return True
    if re.fullmatch(r"[a-z]:[\\/]\*?", lowered):
        return True
    if text.startswith("/"):
        # Normalize POSIX traversal lexically before touching the filesystem.
        # On macOS ``/tmp`` is a symlink to ``/private/tmp``, so resolving
        # ``/tmp/..`` yields ``/private`` even though the command clearly
        # targets the filesystem root. The lexical form must stay a hard deny.
        normalized = posixpath.normpath(text)
        if normalized in {"/", "//"}:
            return True
        if normalized.startswith("//"):
            normalized = "/" + normalized.lstrip("/")
        parent, _, name = normalized.rpartition("/")
        if parent == "" and name in {"*", ".*"}:
            return True
    try:
        path = Path(text)
        if not path.is_absolute():
            path = workspace / path
        resolved = path.resolve(strict=False)
        anchor = Path(resolved.anchor)
        if resolved == anchor:
            return True
        return resolved.parent == anchor and resolved.name in {"*", ".*"}
    except OSError:
        return False


def _strip_wrapper_options(executable: str, args: list[str]) -> list[str]:
    if executable == "timeout":
        # timeout [options] DURATION COMMAND...
        index = 0
        while index < len(args) and args[index].startswith("-"):
            index += 1
        index += 1  # duration
        return args[index:]
    if executable in {"nice", "nohup", "time", "command", "builtin"}:
        index = 0
        while index < len(args) and args[index].startswith("-"):
            index += 1
        return args[index:]
    if executable == "env":
        index = 0
        while index < len(args) and (args[index].startswith("-") or _is_assignment(args[index])):
            index += 1
        return args[index:]
    return args


def _strip_xargs_options(args: list[str]) -> list[str]:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 1
    return args[index:]


def _outside_workspace(token: str, workspace: Path) -> bool:
    value = token.strip("'\"")
    if not value or value in {"-", "/dev/null", "nul", "$null"}:
        return False
    if value.startswith("~"):
        return True
    if value.startswith(("/", "\\")) and workspace != Path("/"):
        return True
    path = Path(value)
    try:
        if path.is_absolute():
            resolved = path.resolve(strict=False)
            try:
                resolved.relative_to(workspace)
                return False
            except ValueError:
                return True
        if ".." in path.parts:
            resolved = (workspace / path).resolve(strict=False)
            try:
                resolved.relative_to(workspace)
                return False
            except ValueError:
                return True
    except OSError:
        return True
    return False
