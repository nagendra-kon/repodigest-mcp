"""RepoDigest MCP server: symbol signatures, call-graph dependencies and budgeted context packing.

Runs over stdio, so stdout belongs to the protocol; all logging goes to stderr.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import click
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from repodigest.packer.budget import ContextPacker
from repodigest.packer.serializer import to_xml
from repodigest.packer.tokenizer import count_tokens

from repodigest_mcp.index import ProjectError, load_index

server = MCPServer(
    "repodigest",
    instructions=(
        "Token-efficient views of a Python repository. Prefer these tools over reading whole files: "
        "get_symbol_signature for an interface, get_symbol_dependencies for who-calls-what, "
        "pack_task_context for a budgeted bundle of code relevant to a task."
    ),
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Surface anticipated failures to the model as tool errors (`is_error=True`)."""
    try:
        yield
    except ProjectError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=_READ_ONLY)
def get_symbol_signature(symbol_name: str, path: str = ".") -> str:
    """Return a Python symbol's signature and docstring without its body.

    Far cheaper than reading the file. Works for functions, classes (methods listed as
    signatures) and methods.

    Args:
        symbol_name: Qualified name (`pkg.mod.Class.method`) or any dotted suffix of it
            (`Class.method`, `method`). Ambiguous suffixes return the candidates.
        path: Repository root to search (default: the server's working directory).
    """
    with _tool_errors():
        index = load_index(path)
        qualname = index.resolve(symbol_name)
        record = index.registry[qualname]
    return f"# {qualname} ({index.relpath(record.file)})\n{record.signature_text}"


@server.tool(annotations=_READ_ONLY)
def get_symbol_dependencies(symbol_name: str, path: str = ".") -> dict[str, Any]:
    """List a symbol's direct callers and callees across the repository.

    Edges are matched by simple name (no import or type resolution), so common names such
    as `get` or `run` can produce false positives. Class instantiation `Foo()` links to
    the class `Foo`, not to `Foo.__init__`.

    Args:
        symbol_name: Qualified name (`pkg.mod.Class.method`) or any dotted suffix of it.
        path: Repository root to search (default: the server's working directory).
    """
    with _tool_errors():
        index = load_index(path)
        qualname = index.resolve(symbol_name)
        record = index.registry[qualname]
    return {
        "symbol": qualname,
        "kind": record.kind,
        "file": index.relpath(record.file),
        "callers": sorted(index.graph.get_callers(qualname)),
        "callees": sorted(index.graph.get_callees(qualname)),
    }


@server.tool(annotations=_READ_ONLY)
def pack_task_context(query: str, budget: int = 2000, signatures_only: bool = False, path: str = ".") -> str:
    """Pack the code most relevant to a task into a token-bounded XML snippet.

    Ranks every symbol against `query` (BM25), takes the best match as the root, then
    breadth-first expands through its callees and callers, skipping (never truncating)
    any symbol that no longer fits.

    Args:
        query: Natural-language description of the task, e.g. "validate user login".
        budget: Max tokens (cl100k_base) of packed source. The XML markup around the source
            (file/symbol tags, usage summary) is not counted, so allow ~10% headroom.
        signatures_only: Pack everything except the root symbol as signature + docstring.
        path: Repository root to search (default: the server's working directory).
    """
    with _tool_errors():
        if budget < 1:
            raise ProjectError(f"budget must be a positive number of tokens, got {budget}.")
        index = load_index(path)
        root_symbol = index.best_match(query)
        packer = ContextPacker(
            index.root,
            budget=budget,
            signatures_only=signatures_only,
            call_graph=index.graph,
            registry=index.registry,
        )
        result = packer.pack(root_symbol)
        if all(packed.symbol != root_symbol for packed in result.packed):
            needed = count_tokens(index.registry[root_symbol].full_text)
            raise ProjectError(
                f"Best match {root_symbol!r} needs {needed} tokens but the budget is {budget}; "
                f"raise budget to at least {needed}."
            )
    return to_xml(result)


@click.command()
@click.version_option(package_name="repodigest-mcp")
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default="WARNING",
    show_default=True,
    help="Log verbosity (written to stderr).",
)
def main(log_level: str) -> None:
    """Run the RepoDigest MCP server over stdio."""
    logging.getLogger().setLevel(log_level.upper())
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
