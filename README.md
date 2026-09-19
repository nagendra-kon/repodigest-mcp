# repodigest-mcp

**Token-efficient code context for AI coding assistants: Python signatures, call graphs and budget-packed context, served locally over MCP.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MCP 2.x](https://img.shields.io/badge/MCP-2.x-8A2BE2.svg)](https://modelcontextprotocol.io)
[![Tests: 61 passing](https://img.shields.io/badge/tests-61%20passing-brightgreen.svg)](#testing)

`repodigest-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server that wraps
[RepoDigest](https://github.com/nagendra-kon/repodigest)'s static analysis (AST parsing, call graph, BM25 search,
token-budgeted packing) so that Claude Code, Cursor, or any MCP client can ask for *exactly the slice of a
codebase it needs* instead of reading whole files.

---

## The problem

Coding assistants explore a repository by reading files. To learn what one function accepts and returns, the
assistant pulls in the entire file: every other function, every body, every import. On a real module that is
routinely 90%+ noise for the question being asked, and it compounds over a session:

- **Context bloat.** The window fills with code that doesn't matter, leaving less room for the code that does.
- **Cost and latency.** You pay for every token in, on every subsequent turn.
- **Worse answers.** Relevant details get buried in long contexts.

Most of what an assistant needs is *structural*, and structure can be extracted deterministically. A function's
interface is its signature and docstring. Its blast radius is its callers and callees. The code relevant to a task
is a neighbourhood in the call graph around the best-matching symbol. None of that needs a model; it needs an
AST.

`repodigest-mcp` exposes those three operations as MCP tools. It runs as a local stdio subprocess: no daemon, no
cloud service, no telemetry, and your source is parsed on your machine. (The only network access is `tiktoken`
fetching its tokenizer vocabulary once on first use, after which it is cached.)

## Tools

All three tools are read-only. Failures come back as MCP tool errors (`is_error=True`) with a message the model
can act on, never as a crash or an empty result.

| Tool | Purpose |
| --- | --- |
| [`get_symbol_signature`](#get_symbol_signaturesymbol_name-path) | A function/class/method's signature and docstring, without the body |
| [`get_symbol_dependencies`](#get_symbol_dependenciessymbol_name-path) | Direct callers and callees, across files |
| [`pack_task_context`](#pack_task_contextquery-budget-signatures_only-path) | Best-matching code for a task, packed into a token budget |

`symbol_name` accepts a fully-qualified name (`pkg.mod.Class.method`) or any dotted suffix (`Class.method`,
`method`). `path` is the repository root and defaults to `.`, the server's working directory.

### `get_symbol_signature(symbol_name, path)`

Returns the definition header and docstring only. Classes come back with every method stubbed to its signature.
For a method in a large file this is typically 90%+ fewer tokens than reading the file
([measured below](#token-efficiency)).

```text
# repodigest.search.ranker.SymbolRanker.score (repodigest/search/ranker.py)
def score(self, query: str, symbol: str) -> float:
    ...
```

The first line names the symbol the request resolved to and where it lives, which matters when you passed a
short suffix.

### `get_symbol_dependencies(symbol_name, path)`

Direct callers and callees from the repository call graph, across files. Returned as structured content:

```json
{
  "symbol": "repodigest.search.ranker.SymbolRanker.rank",
  "kind": "method",
  "file": "repodigest/search/ranker.py",
  "callers": ["repodigest.search.ranker.SymbolRanker.top"],
  "callees": ["repodigest.search.ranker.SymbolRanker.score"]
}
```

> Edges are matched by simple name, with no import or type resolution. Common names (`get`, `run`) can produce
> false positives, and `Foo()` links to the class `Foo`, not to `Foo.__init__`.

### `pack_task_context(query, budget, signatures_only, path)`

Given a natural-language `query`, the tool:

1. ranks every symbol in the repo with **BM25** and takes the best match as the *root*;
2. expands **breadth-first** through the call graph, alternating callees and callers, nearest first;
3. packs symbols until the **token budget** is spent, skipping (never truncating) anything that no longer fits;
4. returns compact XML.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `query` | required | What you're working on, e.g. `"score symbols with BM25"` |
| `budget` | `2000` | Max tokens (`cl100k_base`) of packed *source* |
| `signatures_only` | `false` | Pack everything except the root as signature + docstring |
| `path` | `.` | Repository root |

A real call, `query="score symbols with BM25"`, `budget=1500`, `signatures_only=true` (abbreviated with `…`):

```xml
<context>
  <file path="…/repodigest/search/ranker.py">
    <symbol name="repodigest.search.ranker.SymbolRanker" kind="class" tokens="525">
<![CDATA[
class SymbolRanker:
    """BM25 ranking over a corpus of `{symbol: text}` documents."""
    …
]]>
    </symbol>
  </file>
  <file path="…/repodigest/cli.py">
    <symbol name="repodigest.cli.pack_command" kind="function" tokens="247">
      …
    </symbol>
  </file>
  <usage total_tokens="772" budget="1500" symbols_packed="2" symbols_skipped="0" />
</context>
```

The root is always included in full. Here it is a class, followed by one of its callers.

`budget` counts the packed source only. The XML tags around it are not counted, so leave roughly 10% headroom.

If the best match cannot fit, the tool says so instead of returning something misleading. This is a real
response from the same repository at `budget=500`:

```text
Error executing tool pack_task_context: Best match 'repodigest.search.ranker.SymbolRanker' needs 525 tokens but the budget is 500; raise budget to at least 525.
```

### Errors the tools handle

| Situation | Behaviour |
| --- | --- |
| Symbol not found | Error, with "did you mean" suggestions for near misses |
| Ambiguous suffix (`run` matches 2 methods) | Error listing the candidates (first 5, then `+N more`) |
| `path` missing, not a directory, or empty | Error naming the path |
| Directory with no Python symbols | Error, rather than an empty result |
| Empty `symbol_name` / `query`, `budget < 1` | Error explaining the constraint |
| Root symbol larger than `budget` | Error stating the budget needed |
| Unparsable or non-UTF-8 `.py` file | Skipped with a warning on stderr; the rest is indexed |

## Architecture

```text
MCP client (Claude Code, Cursor, ...)
        │  JSON-RPC over stdio
        ▼
server.py    three read-only tools; translates failures into tool errors
        │
        ▼
index.py     cached per-repo index: symbol registry · call graph · BM25 ranker
        │    file discovery, error-tolerant parsing, symbol resolution
        ▼
RepoDigest   py_parser · CallGraph · SymbolRanker · ContextPacker
```

RepoDigest does the analysis. `index.py` decides *which files* it sees and remembers the result.

### Optimizations

**mtime/size-cached indexer.** Each repo root gets one index (registry, call graph, ranker), stamped with a
fingerprint of `(path, mtime, size)` for every Python file. A repeated call against an unchanged tree reuses the
index. Editing, adding or deleting a file changes the fingerprint and triggers a rebuild on the next call. A
warm lookup on the RepoDigest repo took under 5 ms.

**Virtual environments and vendored code are excluded automatically.** Discovery prunes:

- hidden directories (`.venv`, `.git`, `.tox`, `.cache`, ...);
- **any directory containing a `pyvenv.cfg`**, so virtualenvs are caught whatever they are named (`venv`,
  `myenv`), while an ordinary package that merely happens to be called `env` is kept;
- `site-packages`, `node_modules` and `__pycache__`.

This matters because RepoDigest's own directory walker globs every `*.py` under the root. Pointed at this
project's directory, it collected 1,825 files (nearly all of them from the virtualenv) and took about 5 s.
`repodigest-mcp` indexed the same directory in 0.03 s, and none of the virtualenv's symbols leaked into search
results. If you deliberately pass a virtualenv as the root, it is indexed.

**Fault-tolerant parsing.** One file with a syntax error or a stray non-UTF-8 byte does not take down the index.
That file is skipped and logged.

**Protocol-safe logging.** On stdio, stdout belongs to the protocol. All logging goes to stderr; use
`repodigest-mcp --log-level DEBUG` to see more.

### Token efficiency

Measured on RepoDigest's own source with `cl100k_base`. "Signature" is the exact string
`get_symbol_signature` returns, header line included. "Saved" compares it to reading the file the symbol lives in.

| Symbol | Signature | Symbol source | Whole file | Saved vs. file |
| --- | ---: | ---: | ---: | ---: |
| `ContextPacker.pack` | 40 | 200 | 1,221 | **96.7%** |
| `SymbolRanker.score` | 40 | 146 | 766 | **94.8%** |
| `CallGraph.from_files` | 48 | 346 | 877 | **94.5%** |
| `parse_source` | 110 | 196 | 1,803 | **93.9%** |
| `to_xml` | 37 | 189 | 450 | **91.8%** |
| `ContextPacker` (class) | 166 | 677 | 1,221 | **86.4%** |

Across these six symbols the saving versus reading the whole file is **86% to 97%**. The gap narrows against the
symbol's own body (44% to 86% here) because the body of a short function is not much larger than its signature:
the big win comes from not reading the *rest of the file*. Your numbers will vary with file size and docstring
density.

## Installation

Requires **Python 3.10+**. `repodigest` is not published to PyPI, so install it from GitHub first, then install
this package in editable mode:

```bash
git clone https://github.com/nagendra-kon/repodigest-mcp.git
cd repodigest-mcp

python3 -m venv venv
source venv/bin/activate

pip install "repodigest @ git+https://github.com/nagendra-kon/repodigest.git"   # the analysis engine
pip install -e ".[dev]"                                                           # this server + pytest

repodigest-mcp --version
```

If you already have a local RepoDigest checkout, `pip install -e ../repodigest` works in place of the GitHub line.

### Claude Code

Register the server with the absolute path to the venv's executable, because the client launches it as a
subprocess and it must use the interpreter that has the dependencies installed. From the `repodigest-mcp`
directory:

```bash
claude mcp add repodigest -- "$(pwd)/venv/bin/repodigest-mcp"
```

Add `--scope user` to make it available in every project, or `--scope project` to share it via `.mcp.json`. Check
it with `claude mcp list`.

### Cursor

Add the server to `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` for all projects):

```json
{
  "mcpServers": {
    "repodigest": {
      "command": "/absolute/path/to/repodigest-mcp/venv/bin/repodigest-mcp",
      "args": []
    }
  }
}
```

### Which repository does it read?

Tools default to `path="."`, the server's working directory. If your client starts the server somewhere other
than the project you're working on, pass `path` explicitly (for example, tell the assistant which directory to
use) or start the server from the right directory. The server is read-only, but `path` is not sandboxed: it will
read `.py` files under any directory the client names.

### Try it

Once registered, ask your assistant things like:

- "Show me the signature of `AuthService.login`."
- "What calls `hash_password`, and what does it call?"
- "Pack context for adding rate limiting to the login handler, in 1,500 tokens."

## Testing

```bash
pytest tests/ -v          # 61 tests, ~2 s
```

The suite runs against a synthetic multi-file project built in a temp directory. That project includes a
fake virtualenv, a hidden directory, `node_modules`, a file with a syntax error and a non-UTF-8 file, so the
exclusion and fault-tolerance paths are exercised for real.

| File | Tests | Covers |
| --- | ---: | --- |
| `tests/test_server.py` | 38 | **MCP client integration**: every tool called through a real in-process MCP client session (schema, structured output, `is_error` results), plus **a subprocess transport test** that launches the installed `repodigest-mcp` console script over stdio and calls a tool. Also token-budget guarantees, `signatures_only`, ambiguous and unknown symbols, and invalid paths. |
| `tests/test_index.py` | 23 | **Indexing edge cases**: virtualenv, hidden-dir and vendored-dir exclusion (venv detected by `pyvenv.cfg`, not by name), root validation, skipped unparsable and undecodable files, cache reuse, invalidation on edit / add / delete, symbol resolution and BM25 matching. |

The budget tests assert that reported `total_tokens` never exceeds `budget`, that the per-symbol token counts sum
to the total, that smaller budgets pack fewer symbols and report what was skipped, and that a root symbol that
cannot fit is an error rather than a silent empty result. Integration tests use the official MCP Python client.

## Limitations

- **Python only.** Symbols are top-level functions, classes and methods; nested functions are not indexed
  separately.
- **Name-based call graph.** See the note under `get_symbol_dependencies`.
- **Token counts are a proxy.** They use `tiktoken`'s `cl100k_base`, not Claude's own tokenizer, so treat budgets
  as close approximations. The budget also excludes the XML markup.
- **`mcp>=2.0` only.** MCP SDK 2.x renamed `FastMCP` to `MCPServer`; this package targets the 2.x API.

## License

[MIT](LICENSE). Built on [RepoDigest](https://github.com/nagendra-kon/repodigest).
