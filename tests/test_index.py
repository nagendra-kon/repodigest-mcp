"""Unit tests for the indexing layer: discovery, caching, symbol resolution."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from repodigest_mcp.index import ProjectError, iter_python_files, load_index, resolve_root


def rel(root: Path, files: list[Path]) -> set[str]:
    return {f.relative_to(root).as_posix() for f in files}


class TestDiscovery:
    def test_skips_virtualenvs_hidden_and_vendored_dirs(self, project: Path) -> None:
        found = rel(project, iter_python_files(project))

        assert "app/hashing.py" in found
        assert not any(p.startswith(("venv/", ".cache/", "node_modules/")) for p in found)

    def test_venv_is_detected_by_pyvenv_cfg_not_by_name(self, tmp_path: Path) -> None:
        (tmp_path / "myenv").mkdir()
        (tmp_path / "myenv/pyvenv.cfg").write_text("home = /usr/bin\n")
        (tmp_path / "myenv/lib.py").write_text("def x(): ...\n")
        (tmp_path / "env").mkdir()  # a plain package that merely has a venv-ish name
        (tmp_path / "env/settings.py").write_text("def load(): ...\n")

        assert rel(tmp_path, iter_python_files(tmp_path)) == {"env/settings.py"}

    def test_a_venv_passed_as_the_root_is_still_indexed(self, project: Path) -> None:
        assert "lib/vendored.py" in rel(project / "venv", iter_python_files(project / "venv"))


class TestResolveRoot:
    def test_returns_resolved_directory(self, project: Path) -> None:
        assert resolve_root(str(project / "app" / "..")) == project.resolve()

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_rejects_empty(self, bad: str) -> None:
        with pytest.raises(ProjectError, match="must not be empty"):
            resolve_root(bad)

    def test_rejects_missing(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectError, match="does not exist"):
            resolve_root(str(tmp_path / "nope"))

    def test_rejects_file(self, project: Path) -> None:
        with pytest.raises(ProjectError, match="not a directory"):
            resolve_root(str(project / "app/text.py"))


class TestLoadIndex:
    def test_unparsable_file_is_skipped_with_a_warning_and_the_rest_is_indexed(
        self, project: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="repodigest_mcp.index"):
            index = load_index(str(project))

        assert [f.name for f in index.skipped_files] == ["broken.py"]
        assert "broken.py" in caplog.text
        assert "app.hashing.hash_password" in index.registry

    def test_undecodable_file_is_skipped(self, project: Path) -> None:
        (project / "app/latin1.py").write_bytes(b"# caf\xe9\ndef ok(): ...\n")

        index = load_index(str(project))

        assert "latin1.py" in {f.name for f in index.skipped_files}

    def test_unchanged_repo_reuses_the_cached_index(self, project: Path) -> None:
        assert load_index(str(project)) is load_index(str(project))

    def test_editing_a_file_rebuilds_the_index(self, project: Path) -> None:
        first = load_index(str(project))
        with (project / "app/text.py").open("a") as f:
            f.write("\n\ndef shout(s: str) -> str:\n    return s.upper()\n")

        second = load_index(str(project))

        assert second is not first
        assert "app.text.shout" in second.registry and "app.text.shout" not in first.registry

    def test_adding_and_deleting_files_rebuilds_the_index(self, project: Path) -> None:
        first = load_index(str(project))
        (project / "app/extra.py").write_text("def extra(): ...\n")
        added = load_index(str(project))
        (project / "app/text.py").unlink()
        removed = load_index(str(project))

        assert "app.extra.extra" in added.registry and added is not first
        assert "app.text.slugify" not in removed.registry and removed is not added

    def test_empty_project_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectError, match="No Python symbols"):
            load_index(str(tmp_path))

    def test_call_graph_and_registry_agree(self, project: Path) -> None:
        index = load_index(str(project))

        assert index.graph.nodes <= set(index.registry)
        assert index.graph.get_callees("app.auth.AuthService.login") == {
            "app.hashing.hash_password",
            "app.store.UserStore.lookup_hash",
        }


class TestResolve:
    def test_exact_qualname(self, project: Path) -> None:
        assert load_index(str(project)).resolve("app.text.slugify") == "app.text.slugify"

    def test_unique_suffix(self, project: Path) -> None:
        assert load_index(str(project)).resolve("Worker.run") == "app.jobs.Worker.run"

    def test_suffix_must_align_with_a_dot_boundary(self, project: Path) -> None:
        with pytest.raises(ProjectError, match="not found"):
            load_index(str(project)).resolve("ugify")  # tail of "slugify", not a whole name part

    def test_ambiguous_suffix_lists_candidates(self, project: Path) -> None:
        with pytest.raises(ProjectError, match=r"ambiguous \(2 matches\).*Scheduler\.run.*Worker\.run"):
            load_index(str(project)).resolve("run")

    def test_long_candidate_lists_are_truncated(self, tmp_path: Path) -> None:
        for i in range(8):
            (tmp_path / f"m{i}.py").write_text("def dup(): ...\n")

        with pytest.raises(ProjectError, match=r"\(\+3 more\)"):
            load_index(str(tmp_path)).resolve("dup")

    def test_unknown_name_without_close_matches_has_no_suggestion(self, project: Path) -> None:
        with pytest.raises(ProjectError) as exc:
            load_index(str(project)).resolve("qqqqqqqq")

        assert "not found" in str(exc.value) and "Did you mean" not in str(exc.value)


class TestBestMatch:
    def test_returns_top_ranked_symbol(self, project: Path) -> None:
        assert load_index(str(project)).best_match("slugify title URL slug") == "app.text.slugify"

    def test_no_match(self, project: Path) -> None:
        with pytest.raises(ProjectError, match="No symbols matched"):
            load_index(str(project)).best_match("zzzz qqqq")
