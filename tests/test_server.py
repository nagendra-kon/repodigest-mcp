"""End-to-end tests: every tool is called through a real MCP client session."""

from __future__ import annotations

import asyncio
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.client import CallToolResult
from repodigest.packer.tokenizer import count_tokens

from repodigest_mcp.index import load_index
from repodigest_mcp.server import server

LOGIN_QUERY = "handle HTTP login requests"  # root: app.api.handle_login_request
LOGIN_ROOT = "app.api.handle_login_request"


def call(tool: str, **arguments: object) -> CallToolResult:
    async def _run() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(_run())


def text(result: CallToolResult) -> str:
    return result.content[0].text


def parse_pack(xml: str) -> tuple[list[ET.Element], dict[str, str]]:
    root = ET.fromstring(xml)
    return list(root.iter("symbol")), root.find("usage").attrib


def root_tokens(project: Path, qualname: str) -> int:
    return count_tokens(load_index(str(project)).registry[qualname].full_text)


def test_server_registers_the_three_read_only_tools() -> None:
    async def _run():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in asyncio.run(_run())}
    assert set(tools) == {"get_symbol_signature", "get_symbol_dependencies", "pack_task_context"}
    assert all(tool.annotations.read_only_hint for tool in tools.values())


class TestGetSymbolSignature:
    def test_returns_signature_and_docstring_without_body(self, project: Path) -> None:
        result = call("get_symbol_signature", symbol_name="hash_password", path=str(project))

        assert not result.is_error
        out = text(result)
        assert out.startswith("# app.hashing.hash_password (app/hashing.py)")
        assert "def hash_password(password: str" in out
        assert "Return the hex SHA-256 digest" in out
        assert "hexdigest" not in out  # body omitted

    def test_is_cheaper_than_reading_the_file(self, project: Path) -> None:
        out = text(call("get_symbol_signature", symbol_name="hash_password", path=str(project)))

        assert count_tokens(out) < count_tokens((project / "app/hashing.py").read_text())

    def test_class_lists_method_signatures_only(self, project: Path) -> None:
        out = text(call("get_symbol_signature", symbol_name="AuthService", path=str(project)))

        assert "class AuthService" in out
        assert "def login(" in out and "def register(" in out
        assert "lookup_hash" not in out  # method bodies stubbed

    @pytest.mark.parametrize(
        "name", ["app.store.UserStore.lookup_hash", "UserStore.lookup_hash", "lookup_hash"]
    )
    def test_resolves_qualified_names_and_suffixes(self, project: Path, name: str) -> None:
        result = call("get_symbol_signature", symbol_name=name, path=str(project))

        assert not result.is_error
        assert text(result).startswith("# app.store.UserStore.lookup_hash (app/store.py)")

    def test_ambiguous_name_lists_candidates(self, project: Path) -> None:
        result = call("get_symbol_signature", symbol_name="run", path=str(project))

        assert result.is_error
        assert "ambiguous" in text(result)
        assert "app.jobs.Worker.run" in text(result) and "app.jobs.Scheduler.run" in text(result)

    def test_unknown_symbol_suggests_close_matches(self, project: Path) -> None:
        result = call("get_symbol_signature", symbol_name="hash_pasword", path=str(project))

        assert result.is_error
        assert "not found" in text(result)
        assert "app.hashing.hash_password" in text(result)

    @pytest.mark.parametrize("name", ["vendored_helper", "cached_junk", "node_tool", "oops"])
    def test_ignores_venvs_hidden_dirs_vendored_dirs_and_unparsable_files(
        self, project: Path, name: str
    ) -> None:
        result = call("get_symbol_signature", symbol_name=name, path=str(project))

        assert result.is_error and "not found" in text(result)

    def test_empty_symbol_name_is_an_error(self, project: Path) -> None:
        result = call("get_symbol_signature", symbol_name="  ", path=str(project))

        assert result.is_error and "must not be empty" in text(result)

    def test_missing_path_is_an_error(self, tmp_path: Path) -> None:
        result = call("get_symbol_signature", symbol_name="x", path=str(tmp_path / "nope"))

        assert result.is_error and "does not exist" in text(result)

    def test_file_path_is_an_error(self, project: Path) -> None:
        result = call("get_symbol_signature", symbol_name="x", path=str(project / "app/text.py"))

        assert result.is_error and "not a directory" in text(result)

    def test_directory_without_python_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hi")

        result = call("get_symbol_signature", symbol_name="x", path=str(tmp_path))

        assert result.is_error and "No Python symbols" in text(result)

    def test_default_path_is_the_working_directory(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project)

        result = call("get_symbol_signature", symbol_name="slugify")

        assert not result.is_error and "def slugify(" in text(result)


class TestGetSymbolDependencies:
    def test_returns_direct_callers_and_callees(self, project: Path) -> None:
        result = call("get_symbol_dependencies", symbol_name="AuthService.login", path=str(project))

        assert not result.is_error
        assert result.structured_content == {
            "symbol": "app.auth.AuthService.login",
            "kind": "method",
            "file": "app/auth.py",
            "callers": ["app.api.handle_login_request"],
            "callees": ["app.hashing.hash_password", "app.store.UserStore.lookup_hash"],
        }

    def test_callers_span_files(self, project: Path) -> None:
        result = call("get_symbol_dependencies", symbol_name="hash_password", path=str(project))

        assert result.structured_content["callers"] == [
            "app.auth.AuthService.login",
            "app.auth.AuthService.register",
        ]
        assert result.structured_content["callees"] == []

    def test_isolated_symbol_has_no_edges(self, project: Path) -> None:
        result = call("get_symbol_dependencies", symbol_name="slugify", path=str(project))

        assert result.structured_content["callers"] == [] and result.structured_content["callees"] == []

    def test_unknown_symbol_is_an_error(self, project: Path) -> None:
        result = call("get_symbol_dependencies", symbol_name="does_not_exist", path=str(project))

        assert result.is_error and "not found" in text(result)

    def test_invalid_path_is_an_error(self, tmp_path: Path) -> None:
        result = call("get_symbol_dependencies", symbol_name="x", path=str(tmp_path / "nope"))

        assert result.is_error and "does not exist" in text(result)


class TestPackTaskContext:
    def test_packs_root_first_then_its_call_graph_neighbourhood(self, project: Path) -> None:
        result = call("pack_task_context", query=LOGIN_QUERY, budget=10_000, path=str(project))

        assert not result.is_error
        symbols, usage = parse_pack(text(result))
        names = [s.get("name") for s in symbols]
        assert names[0] == LOGIN_ROOT
        assert "app.auth.AuthService.login" in names  # callee
        assert "app.hashing.hash_password" in names  # two hops away
        assert "app.text.slugify" not in names  # unrelated
        assert usage["symbols_packed"] == str(len(symbols)) and usage["symbols_skipped"] == "0"

    @pytest.mark.parametrize("budget", [50, 100, 150, 10_000])
    def test_never_exceeds_the_token_budget(self, project: Path, budget: int) -> None:
        result = call("pack_task_context", query=LOGIN_QUERY, budget=budget, path=str(project))

        assert not result.is_error
        symbols, usage = parse_pack(text(result))
        assert int(usage["total_tokens"]) <= budget
        assert int(usage["budget"]) == budget
        assert sum(int(s.get("tokens")) for s in symbols) == int(usage["total_tokens"])

    def test_smaller_budget_packs_fewer_symbols_and_reports_skips(self, project: Path) -> None:
        big, _ = parse_pack(
            text(call("pack_task_context", query=LOGIN_QUERY, budget=10_000, path=str(project)))
        )
        tight = root_tokens(project, LOGIN_ROOT)  # root only

        symbols, usage = parse_pack(
            text(call("pack_task_context", query=LOGIN_QUERY, budget=tight, path=str(project)))
        )

        assert [s.get("name") for s in symbols] == [LOGIN_ROOT]
        assert len(symbols) < len(big)
        assert int(usage["symbols_skipped"]) == len(big) - 1

    def test_signatures_only_stubs_dependencies_but_keeps_the_root_whole(self, project: Path) -> None:
        full = text(call("pack_task_context", query=LOGIN_QUERY, budget=10_000, path=str(project)))
        sigs = text(
            call(
                "pack_task_context", query=LOGIN_QUERY, budget=10_000, signatures_only=True, path=str(project)
            )
        )

        body = "stored == hash_password(password)"  # AuthService.login's body, a dependency
        assert body in full and body not in sigs
        assert 'service.login(payload["username"]' in sigs  # root stays full
        assert int(parse_pack(sigs)[1]["total_tokens"]) < int(parse_pack(full)[1]["total_tokens"])

    def test_root_larger_than_budget_is_an_error_naming_the_needed_budget(self, project: Path) -> None:
        needed = root_tokens(project, LOGIN_ROOT)

        result = call("pack_task_context", query=LOGIN_QUERY, budget=needed - 1, path=str(project))

        assert result.is_error
        assert LOGIN_ROOT in text(result) and f"at least {needed}" in text(result)

    @pytest.mark.parametrize("budget", [0, -5])
    def test_non_positive_budget_is_an_error(self, project: Path, budget: int) -> None:
        result = call("pack_task_context", query=LOGIN_QUERY, budget=budget, path=str(project))

        assert result.is_error and "positive" in text(result)

    def test_query_with_no_matches_is_an_error(self, project: Path) -> None:
        result = call("pack_task_context", query="zzzz qqqq", path=str(project))

        assert result.is_error and "No symbols matched" in text(result)

    def test_empty_query_is_an_error(self, project: Path) -> None:
        result = call("pack_task_context", query=" ", path=str(project))

        assert result.is_error and "must not be empty" in text(result)

    def test_invalid_path_is_an_error(self, tmp_path: Path) -> None:
        result = call("pack_task_context", query="anything", path=str(tmp_path / "nope"))

        assert result.is_error and "does not exist" in text(result)

    def test_default_budget_and_path(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)

        result = call("pack_task_context", query="slugify title URL slug")

        assert not result.is_error
        symbols, usage = parse_pack(text(result))
        assert symbols[0].get("name") == "app.text.slugify"
        assert usage["budget"] == "2000"


def test_console_script_serves_tools_over_stdio(project: Path) -> None:
    """The installed `repodigest-mcp` entry point speaks MCP on stdout with nothing else mixed in."""
    script = shutil.which("repodigest-mcp", path=str(Path(sys.executable).parent))
    assert script, "repodigest-mcp console script is not installed next to the interpreter"

    async def _run() -> CallToolResult:
        # `path` is omitted on purpose: the server's cwd is the project.
        async with Client(StdioServerParameters(command=script, cwd=project)) as client:
            return await client.call_tool("get_symbol_signature", {"symbol_name": "slugify"})

    result = asyncio.run(_run())

    assert not result.is_error
    assert "def slugify(" in text(result)
