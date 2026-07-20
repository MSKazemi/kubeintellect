"""
Unit tests for kube_q/session.py.

Covers: _resolve_attachments (bare path, quoted path, missing file,
        oversized file, multiple attachments), _load_or_create_user_id
        (explicit arg, from existing file, auto-generate).
"""

from pathlib import Path

import pytest

from kube_q.core.session import load_or_create_user_id as _load_or_create_user_id
from kube_q.core.session import resolve_attachments as _resolve_attachments

# ── _resolve_attachments ──────────────────────────────────────────────────────


def test_resolve_attachments_no_at(tmp_path: Path) -> None:
    text = "just a plain message"
    expanded, attached, errors = _resolve_attachments(text)
    assert expanded == text
    assert attached == []
    assert errors == []


def test_resolve_attachments_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "pod.yaml"
    f.write_text("apiVersion: v1\nkind: Pod\n")
    msg = f"what is wrong? @{f}"
    expanded, attached, errors = _resolve_attachments(msg)
    assert errors == []
    assert len(attached) == 1
    assert "pod.yaml" in attached[0]
    assert "```yaml" in expanded
    assert "apiVersion: v1" in expanded


def test_resolve_attachments_missing_file(tmp_path: Path) -> None:
    msg = f"@{tmp_path}/nonexistent.yaml"
    expanded, attached, errors = _resolve_attachments(msg)
    assert len(errors) == 1
    assert "not found" in errors[0]
    assert attached == []
    # Original token preserved in output
    assert "nonexistent.yaml" in expanded


def test_resolve_attachments_file_too_large(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (101 * 1024))  # 101 KB > 100 KB limit
    expanded, attached, errors = _resolve_attachments(f"@{big}")
    assert len(errors) == 1
    assert "too large" in errors[0]
    assert attached == []


def test_resolve_attachments_quoted_path(tmp_path: Path) -> None:
    f = tmp_path / "my file.json"
    f.write_text('{"key": "value"}')
    msg = f'@"{f}"'
    expanded, attached, errors = _resolve_attachments(msg)
    assert errors == []
    assert len(attached) == 1
    assert "```json" in expanded


def test_resolve_attachments_multiple_files(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("a: 1")
    b.write_text("b: 2")
    expanded, attached, errors = _resolve_attachments(f"compare @{a} and @{b}")
    assert errors == []
    assert len(attached) == 2
    assert "a: 1" in expanded
    assert "b: 2" in expanded


def test_resolve_attachments_unknown_extension(tmp_path: Path) -> None:
    f = tmp_path / "notes.xyz"
    f.write_text("some content")
    expanded, attached, errors = _resolve_attachments(f"@{f}")
    assert errors == []
    # Unknown extension → fence with no language specifier
    assert "```\n" in expanded or "```" in expanded


def test_resolve_attachments_not_a_file(tmp_path: Path) -> None:
    msg = f"@{tmp_path}"  # directory, not a file
    expanded, attached, errors = _resolve_attachments(msg)
    assert len(errors) == 1
    assert "not a regular file" in errors[0]


# ── _load_or_create_user_id ───────────────────────────────────────────────────


def test_load_or_create_user_id_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    id_file = tmp_path / "kube_q_id"
    monkeypatch.setattr("kube_q.core.session._USER_ID_FILE", str(id_file))
    uid = _load_or_create_user_id("my-explicit-id")
    assert uid == "my-explicit-id"
    assert id_file.read_text() == "my-explicit-id"
    assert oct(id_file.stat().st_mode)[-3:] == "600"


def test_load_or_create_user_id_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    id_file = tmp_path / "kube_q_id"
    id_file.write_text("saved-user-id")
    monkeypatch.setattr("kube_q.core.session._USER_ID_FILE", str(id_file))
    uid = _load_or_create_user_id()
    assert uid == "saved-user-id"


def test_load_or_create_user_id_generates_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    id_file = tmp_path / "kube_q_id"
    monkeypatch.setattr("kube_q.core.session._USER_ID_FILE", str(id_file))
    uid = _load_or_create_user_id()
    assert uid.startswith("cli-user-")
    assert id_file.exists()
    assert id_file.read_text() == uid


def test_load_or_create_user_id_empty_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    id_file = tmp_path / "kube_q_id"
    id_file.write_text("   ")  # whitespace only — treated as empty
    monkeypatch.setattr("kube_q.core.session._USER_ID_FILE", str(id_file))
    uid = _load_or_create_user_id()
    # Should generate a new ID rather than returning whitespace
    assert uid.startswith("cli-user-")
