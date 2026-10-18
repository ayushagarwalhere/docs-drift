"""Discovery checks use plain files and local Git indexes, never executable fixture programs."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from docs_drift.config import Config
from docs_drift.docs.discover import MAX_DOC_BYTES, DiscoveryWarning, discover_docs, read_doc
from docs_drift.models import DocFormat


def write(root: Path, name: str, content: bytes = b"# Guide\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_walk_configured_globs_sorted_paths_and_excludes(tmp_path: Path) -> None:
    for name in ("README.md", "docs/nested/a.md", "docs/b.md", "other/no.md", "CHANGELOG.md", "docs/CHANGELOG.md"):
        write(tmp_path, name)
    docs = discover_docs(tmp_path, Config())
    assert [doc.path for doc in docs] == ["README.md", "docs/b.md", "docs/nested/a.md"]
    assert docs[0].content_hash == hashlib.sha256(b"# Guide\n").hexdigest()


def test_non_git_ignores_and_negations_and_vendor_pruning(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", b"ignored.md\n*.private.md\n!keep.private.md\nblocked/\n")
    write(tmp_path, "docs/.gitignore", b"hidden.md\n!keep.private.md\n")
    for name in (
        "ignored.md",
        "secret.private.md",
        "keep.private.md",
        "docs/hidden.md",
        "docs/keep.private.md",
        "docs/ok.md",
        "blocked/README.md",
        "node_modules/README.md",
        ".venv/README.md",
        "vendor/README.md",
    ):
        write(tmp_path, name)
    docs = discover_docs(tmp_path, Config(docs=["**/*.md"], exclude=[]))
    assert [doc.path for doc in docs] == ["docs/keep.private.md", "docs/ok.md", "keep.private.md"]


def test_git_mode_only_tracked_and_respects_ignore_even_if_tracked(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    write(tmp_path, "README.md")
    write(tmp_path, "tracked ignored.md")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", "README.md", "tracked ignored.md"], check=True)
    write(tmp_path, "untracked.md")
    write(tmp_path, ".gitignore", b"tracked ignored.md\n")
    assert [doc.path for doc in discover_docs(tmp_path, Config())] == ["README.md"]


def test_nested_ignore_can_override_a_parent_file_pattern(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", b"*.md\n!README.md\n")
    write(tmp_path, "docs/.gitignore", b"!kept.md\n")
    for name in ("README.md", "docs/kept.md", "docs/dropped.md"):
        write(tmp_path, name)
    assert [doc.path for doc in discover_docs(tmp_path, Config())] == ["README.md", "docs/kept.md"]


def test_configured_formats_and_rst_opt_in(tmp_path: Path) -> None:
    for name in ("README.md", "guide.markdown", "guide.mdx", "guide.rst", "notes.txt", "binary.png"):
        write(tmp_path, name)
    assert [doc.path for doc in discover_docs(tmp_path, Config())] == ["README.md"]
    docs = discover_docs(tmp_path, Config(docs=["*"], exclude=[]))
    assert {doc.path: doc.format for doc in docs} == {
        "README.md": DocFormat.markdown,
        "guide.markdown": DocFormat.markdown,
        "guide.mdx": DocFormat.mdx,
        "guide.rst": DocFormat.rst,
        "notes.txt": DocFormat.text,
    }


def test_size_boundary_and_invalid_utf8(tmp_path: Path) -> None:
    write(tmp_path, "large.md", b"x" * (MAX_DOC_BYTES + 1))
    write(tmp_path, "limit.md", b"x" * MAX_DOC_BYTES)
    write(tmp_path, "README.md", b"# Guide\nBad byte: \xff\n")
    with pytest.warns(DiscoveryWarning, match="exceeds 2 MB"):
        docs = discover_docs(tmp_path, Config())
    assert [doc.path for doc in docs] == ["README.md", "limit.md"]
    assert "\ufffd" in read_doc(tmp_path, docs[0])
    assert docs[0].content_hash == hashlib.sha256(b"# Guide\nBad byte: \xff\n").hexdigest()


def test_read_detects_changed_snapshot(tmp_path: Path) -> None:
    path = write(tmp_path, "README.md")
    doc = discover_docs(tmp_path, Config())[0]
    path.write_bytes(b"# Changed\n")
    with pytest.raises(ValueError, match="changed since discovery"):
        read_doc(tmp_path, doc)


def test_broken_git_metadata_falls_back_with_warning(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    write(tmp_path, "README.md")
    with pytest.warns(DiscoveryWarning, match="Cannot list tracked"):
        assert len(discover_docs(tmp_path, Config())) == 1


def test_invalid_root_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing repository directory"):
        discover_docs(tmp_path / "missing", Config())


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    write(tmp_path, "README.md")
    try:
        (tmp_path / "linked.md").symlink_to(tmp_path / "README.md")
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or elevated privileges.")
    assert [doc.path for doc in discover_docs(tmp_path, Config())] == ["README.md"]
