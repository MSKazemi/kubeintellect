"""
config_cmd.py — `kq config` subcommand: show / set / reset values in ~/.kube-q/.env.

Subcommands:
  kq config show                  Print effective config with each value's source.
  kq config set KEY=VAL           Write KEY=VAL to ~/.kube-q/.env (creates file).
  kq config reset KEY             Remove KEY from ~/.kube-q/.env.
  kq config reset                 Wipe ~/.kube-q/.env entirely (asks for confirmation).

KEY may be the full env var (``KUBE_Q_URL``) or its config-field alias (``url``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.table import Table

from kube_q.cli.renderer import console
from kube_q.core.config import (
    _ENV_MAP,
    CONFIG_DIR,
    PROFILES_DIR,
    Config,
    load_config,
    validate_config,
)

ENV_FILE = CONFIG_DIR / ".env"

_PROFILE_TEMPLATE = """\
# kube-q profile — loaded after ~/.kube-q/.env, before ./.env.
# Uncomment and edit the keys you want to override for this profile.
#
# KUBE_Q_URL=http://prod-cluster.example.com:8000
# KUBE_Q_API_KEY=
# KUBE_Q_MODEL=kubeintellect-v2
# KUBE_Q_BACKEND=kube-q
# KUBE_Q_CONTEXT=prod
"""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _normalize_key(raw: str) -> str | None:
    """Return the canonical ``KUBE_Q_*`` env var for a user-supplied key.

    Accepts either the env var (``KUBE_Q_URL``) or the field name (``url``).
    Returns None if the key is not recognised.
    """
    raw = raw.strip()
    if raw in _ENV_MAP:
        return raw
    upper = raw.upper()
    if upper in _ENV_MAP:
        return upper
    # Field-name alias
    for env_key, (field_name, _typ) in _ENV_MAP.items():
        if field_name == raw.lower():
            return env_key
    return None


def _read_env_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _write_env_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _file_values(path: Path) -> dict[str, str]:
    """Parse the env file into {KEY: VALUE}, ignoring comments / blanks."""
    out: dict[str, str] = {}
    for line in _read_env_file(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        k = k.strip()
        v = v.split("#")[0].strip().strip("\"'")
        if k:
            out[k] = v
    return out


def _set_key(path: Path, env_key: str, value: str) -> None:
    lines = [ln for ln in _read_env_file(path)
             if not ln.strip().startswith(f"{env_key}=")]
    lines.append(f"{env_key}={value}")
    _write_env_file(path, lines)


def _remove_key(path: Path, env_key: str) -> bool:
    """Remove ``env_key`` from *path*. Returns True if something was removed."""
    original = _read_env_file(path)
    filtered = [ln for ln in original if not ln.strip().startswith(f"{env_key}=")]
    if len(filtered) == len(original):
        return False
    _write_env_file(path, filtered)
    return True


# ── `kq config show` ──────────────────────────────────────────────────────────


def _value_source(env_key: str, file_vals: dict[str, str]) -> str:
    """Report where the current value came from: shell env / file / default."""
    # Shell env wins — but _load_dotenv_file has already merged file values
    # into os.environ. Distinguish by comparing against what's in the file.
    env_val = os.environ.get(env_key)
    if env_val is None:
        return "default"
    if env_key in file_vals and file_vals[env_key] == env_val:
        return f"file ({ENV_FILE})"
    return "shell env"


def _mask(env_key: str, value: object) -> str:
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    if "API_KEY" in env_key or "TOKEN" in env_key:
        # Show only last 4 chars
        tail = s[-4:] if len(s) > 4 else ""
        return f"***{tail}" if tail else "***"
    return s


def cmd_show() -> int:
    file_vals = _file_values(ENV_FILE)
    cfg = load_config(strict=False)

    table = Table(
        "Key", "Value", "Source",
        title=f"[bold cyan]kube-q config[/bold cyan]  [dim]({ENV_FILE})[/dim]",
        border_style="dim cyan",
    )

    for env_key, (field_name, _typ) in _ENV_MAP.items():
        current = getattr(cfg, field_name, None)
        table.add_row(
            env_key,
            _mask(env_key, current),
            _value_source(env_key, file_vals),
        )

    console.print(table)

    errors = validate_config(cfg)
    if errors:
        console.print()
        console.print("[red bold]⚠ Invalid values detected:[/red bold]")
        for err in errors:
            console.print(f"  [red]•[/red] {err.splitlines()[0]}")
    return 0


# ── `kq config set KEY=VAL` ───────────────────────────────────────────────────


def cmd_set(assignment: str) -> int:
    if "=" not in assignment:
        console.print(
            "[red]Usage:[/red] kq config set KEY=VALUE  "
            "[dim](example: kq config set url=https://api.kubeintellect.com)[/dim]"
        )
        return 2
    raw_key, _, value = assignment.partition("=")
    env_key = _normalize_key(raw_key)
    if not env_key:
        console.print(
            f"[red]Unknown key:[/red] {raw_key!r}.  "
            "Run [yellow]kq config show[/yellow] for the list of valid keys."
        )
        return 2

    # Validate just the new value. We only block the write if the error
    # clearly relates to the field we're about to set — unrelated pre-existing
    # errors in the user's env shouldn't stop them from fixing things.
    os.environ[env_key] = value
    cfg = load_config(strict=False)
    field_name = _ENV_MAP[env_key][0]
    field_marker = f"Invalid {field_name}"
    url_marker = "Invalid URL"  # validate_config labels URL errors specially
    related = [
        e for e in validate_config(cfg)
        if field_marker in e or (field_name == "url" and url_marker in e)
    ]
    if related:
        console.print("[red]Refusing to write — value would be invalid:[/red]")
        for err in related:
            console.print(f"  [red]•[/red] {err.splitlines()[0]}")
        return 2

    _set_key(ENV_FILE, env_key, value)
    console.print(
        f"[green]✓[/green] Set [bold]{env_key}[/bold]={_mask(env_key, value)}  "
        f"[dim]→ {ENV_FILE}[/dim]"
    )
    return 0


# ── `kq config reset [KEY]` ───────────────────────────────────────────────────


def cmd_reset(raw_key: str | None) -> int:
    if raw_key:
        env_key = _normalize_key(raw_key)
        if not env_key:
            console.print(
                f"[red]Unknown key:[/red] {raw_key!r}.  "
                "Run [yellow]kq config show[/yellow] for the list of valid keys."
            )
            return 2
        if _remove_key(ENV_FILE, env_key):
            console.print(
                f"[green]✓[/green] Removed [bold]{env_key}[/bold] from {ENV_FILE}"
            )
        else:
            console.print(
                f"[dim]{env_key} was not present in {ENV_FILE} — nothing to do.[/dim]"
            )
        return 0

    # Full reset
    if not ENV_FILE.exists():
        console.print(f"[dim]{ENV_FILE} does not exist — nothing to reset.[/dim]")
        return 0

    try:
        ENV_FILE.unlink()
    except OSError as exc:
        console.print(f"[red]Failed to delete {ENV_FILE}: {exc}[/red]")
        return 1
    console.print(f"[green]✓[/green] Deleted {ENV_FILE} — all keys reset to defaults.")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────


_HELP = """\
Usage:
  kq config show                  Print effective config with each value's source.
  kq config set KEY=VALUE         Write KEY=VALUE to ~/.kube-q/.env.
  kq config reset KEY             Remove KEY from ~/.kube-q/.env.
  kq config reset                 Delete ~/.kube-q/.env entirely.

  kq config profile list          List profiles in ~/.kube-q/profiles/.
  kq config profile new NAME      Create a new profile (~/.kube-q/profiles/NAME.env).
  kq config profile delete NAME   Delete ~/.kube-q/profiles/NAME.env.
  kq config profile show NAME     Print the contents of a profile.

KEY may be the full env var (KUBE_Q_URL) or its alias (url).
"""


# ── `kq config profile` subcommand ────────────────────────────────────────────


def _profile_path(name: str) -> Path:
    return PROFILES_DIR / f"{name}.env"


def cmd_profile_list() -> int:
    if not PROFILES_DIR.exists():
        console.print(f"[dim]{PROFILES_DIR} does not exist yet.[/dim]")
        console.print(
            "[dim]Create a profile with [yellow]kq config profile new <name>[/yellow].[/dim]"
        )
        return 0
    profiles = sorted(p.stem for p in PROFILES_DIR.glob("*.env"))
    if not profiles:
        console.print(f"[dim]No profiles in {PROFILES_DIR}.[/dim]")
        return 0
    console.print(f"[bold cyan]Profiles[/bold cyan] [dim]({PROFILES_DIR})[/dim]")
    active = os.environ.get("KUBE_Q_PROFILE")
    for name in profiles:
        mark = "→ " if name == active else "  "
        console.print(f"  {mark}[bold]{name}[/bold]")
    if active:
        console.print(f"\n[dim]Active: KUBE_Q_PROFILE={active}[/dim]")
    return 0


def cmd_profile_new(name: str) -> int:
    if not name.replace("-", "").replace("_", "").isalnum():
        console.print(
            f"[red]Invalid profile name {name!r}[/red] — "
            "must contain only alphanumerics, '-', or '_'."
        )
        return 2
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = _profile_path(name)
    if path.exists():
        console.print(f"[yellow]Profile already exists:[/yellow] {path}")
        return 1
    path.write_text(_PROFILE_TEMPLATE, encoding="utf-8")
    console.print(f"[green]✓[/green] Created [bold]{path}[/bold]")
    console.print(
        f"[dim]Use it with:[/dim]\n  [yellow]kq --profile {name}[/yellow]\n"
        f"  [yellow]KUBE_Q_PROFILE={name} kq[/yellow]"
    )
    return 0


def cmd_profile_delete(name: str) -> int:
    path = _profile_path(name)
    if not path.exists():
        console.print(f"[dim]{path} does not exist — nothing to do.[/dim]")
        return 0
    try:
        path.unlink()
    except OSError as exc:
        console.print(f"[red]Failed to delete {path}: {exc}[/red]")
        return 1
    console.print(f"[green]✓[/green] Deleted {path}")
    return 0


def cmd_profile_show(name: str) -> int:
    path = _profile_path(name)
    if not path.exists():
        console.print(f"[red]No such profile:[/red] {path}")
        return 1
    console.print(f"[dim]# {path}[/dim]")
    console.print(path.read_text(encoding="utf-8").rstrip())
    return 0


def _cmd_profile(argv: list[str]) -> int:
    if not argv:
        print(_HELP, file=sys.stderr)
        return 2
    sub = argv[0]
    if sub == "list" and len(argv) == 1:
        return cmd_profile_list()
    if sub == "new" and len(argv) == 2:
        return cmd_profile_new(argv[1])
    if sub in ("delete", "rm") and len(argv) == 2:
        return cmd_profile_delete(argv[1])
    if sub == "show" and len(argv) == 2:
        return cmd_profile_show(argv[1])
    print(_HELP, file=sys.stderr)
    return 2


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0

    sub = argv[0]
    rest = argv[1:]

    if sub == "show":
        if rest:
            print("kq config show takes no arguments.", file=sys.stderr)
            return 2
        return cmd_show()

    if sub == "set":
        if len(rest) != 1:
            print(_HELP, file=sys.stderr)
            return 2
        return cmd_set(rest[0])

    if sub == "reset":
        if len(rest) > 1:
            print(_HELP, file=sys.stderr)
            return 2
        return cmd_reset(rest[0] if rest else None)

    if sub == "profile":
        return _cmd_profile(rest)

    print(f"kq config: unknown subcommand {sub!r}\n\n{_HELP}", file=sys.stderr)
    return 2


# Fallback when the module is exec'd directly (unused by the CLI, kept for devs).
if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))


__all__ = [
    "Config",  # re-exported for type hints in callers
    "run",
    "cmd_show",
    "cmd_set",
    "cmd_reset",
]
