"""Per-repository index over RepoDigest's parser, call graph and BM25 ranker.

RepoDigest's own entry points (`CallGraph.from_directory`, `build_symbol_registry`)
glob every `*.py` under the root and abort on the first unparsable file. That is
wrong for an MCP server pointed at a live checkout, where the root usually holds
virtualenvs and the odd broken file. This module does its own file discovery and
error handling, then feeds RepoDigest's public building blocks.

Indexes are cached per root and invalidated when any file's mtime or size changes,
so repeated tool calls against an unchanged repo skip re-parsing.
"""

from __future__ import annotations

import difflib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from repodigest.graph.call_graph import CallGraph, module_qualname
from repodigest.packer.budget import SymbolRecord
from repodigest.parsers.py_parser import parse_file
from repodigest.search.ranker import SymbolRanker

log = logging.getLogger(__name__)

_SKIP_DIR_NAMES = frozenset({"node_modules", "__pycache__", "site-packages"})
_PARSE_ERRORS = (SyntaxError, ValueError, OSError, RecursionError)
_MAX_LISTED = 5


class ProjectError(Exception):
    """A failure the caller can act on: bad path, unknown or ambiguous symbol, no matches."""


@dataclass(frozen=True)
class ProjectIndex:
    root: Path
    registry: dict[str, SymbolRecord]
    graph: CallGraph
    ranker: SymbolRanker
    skipped_files: tuple[Path, ...] = ()

    def relpath(self, file: Path) -> str:
        return file.relative_to(self.root).as_posix()

    def resolve(self, name: str) -> str:
        """Map a user-supplied symbol name to its fully-qualified registry key.

        Accepts a full qualname (`pkg.mod.Class.method`) or any dotted suffix of one
        (`Class.method`, `method`). Raises `ProjectError` if nothing matches or the
        suffix is ambiguous.
        """
        name = name.strip()
        if not name:
            raise ProjectError("symbol_name must not be empty.")
        if name in self.registry:
            return name

        suffix = f".{name}"
        matches = sorted(qual for qual in self.registry if qual.endswith(suffix))
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ProjectError(
                f"Symbol {name!r} is ambiguous ({len(matches)} matches); use a longer name: "
                + _format_list(matches)
            )
        raise ProjectError(f"Symbol {name!r} not found under {self.root}.{self._suggest(name)}")

    def best_match(self, query: str) -> str:
        """Top BM25 hit for a natural-language `query`; raises `ProjectError` if nothing scores."""
        if not query.strip():
            raise ProjectError("query must not be empty.")
        top = self.ranker.top(query)
        if top is None:
            raise ProjectError(f"No symbols matched query {query!r} under {self.root}.")
        return top

    def _suggest(self, name: str) -> str:
        short_names = {qual.rsplit(".", 1)[-1] for qual in self.registry}
        close = difflib.get_close_matches(name.rsplit(".", 1)[-1], short_names, n=3)
        if not close:
            return ""
        hits = sorted(qual for qual in self.registry if qual.rsplit(".", 1)[-1] in close)
        return " Did you mean: " + _format_list(hits)


def _format_list(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_LISTED])
    extra = len(items) - _MAX_LISTED
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def resolve_root(path: str) -> Path:
    """Validate `path` as an existing directory and return its resolved absolute form."""
    if not path or not path.strip():
        raise ProjectError("path must not be empty.")
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise ProjectError(f"Path does not exist: {root}")
    if not root.is_dir():
        raise ProjectError(f"Path is not a directory: {root}")
    return root


def iter_python_files(root: Path) -> list[Path]:
    """`*.py` files under `root`, skipping hidden dirs, virtualenvs and vendored/cache dirs."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not _is_skipped_dir(here / d))
        files.extend(here / name for name in sorted(filenames) if name.endswith(".py"))
    return files


def _is_skipped_dir(directory: Path) -> bool:
    name = directory.name
    return name.startswith(".") or name in _SKIP_DIR_NAMES or (directory / "pyvenv.cfg").is_file()


_Fingerprint = tuple[tuple[str, int, int], ...]
_CACHE: dict[Path, tuple[_Fingerprint, ProjectIndex]] = {}


def load_index(path: str) -> ProjectIndex:
    """Return the (cached) index for the repository at `path`.

    Raises `ProjectError` if `path` is not a directory or holds no parsable Python symbols.
    """
    root = resolve_root(path)
    files, fingerprint = _stat_files(iter_python_files(root))

    cached = _CACHE.get(root)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    index = _build_index(root, files)
    if not index.registry:
        raise ProjectError(f"No Python symbols found under {root}.")
    _CACHE[root] = (fingerprint, index)
    return index


def _stat_files(files: list[Path]) -> tuple[list[Path], _Fingerprint]:
    kept: list[Path] = []
    stamps: list[tuple[str, int, int]] = []
    for file in files:
        try:
            stat = file.stat()
        except OSError:  # vanished (or dangling symlink) between the walk and now
            continue
        kept.append(file)
        stamps.append((str(file), stat.st_mtime_ns, stat.st_size))
    return kept, tuple(stamps)


def _build_index(root: Path, files: list[Path]) -> ProjectIndex:
    # Mirrors repodigest.packer.budget.build_symbol_registry, over an explicit file list
    # and tolerant of files that fail to parse.
    registry: dict[str, SymbolRecord] = {}
    parsed: list[Path] = []
    skipped: list[Path] = []

    for file in files:
        try:
            full = parse_file(file, mode="full")
            sig = parse_file(file, mode="signature")
        except _PARSE_ERRORS as exc:
            log.warning("Skipping %s: %s: %s", file, type(exc).__name__, exc)
            skipped.append(file)
            continue
        parsed.append(file)

        mod_name = module_qualname(file, root)
        for func_full, func_sig in zip(full.functions, sig.functions):
            qual = f"{mod_name}.{func_full.name}"
            registry[qual] = SymbolRecord(qual, "function", file, func_full.source, func_sig.source)
        for cls_full, cls_sig in zip(full.classes, sig.classes):
            class_qual = f"{mod_name}.{cls_full.name}"
            registry[class_qual] = SymbolRecord(class_qual, "class", file, cls_full.source, cls_sig.source)
            for m_full, m_sig in zip(cls_full.methods, cls_sig.methods):
                method_qual = f"{class_qual}.{m_full.name}"
                registry[method_qual] = SymbolRecord(method_qual, "method", file, m_full.source, m_sig.source)

    corpus = {qual: f"{qual} {record.signature_text}" for qual, record in registry.items()}
    return ProjectIndex(
        root=root,
        registry=registry,
        graph=CallGraph.from_files(parsed, root),
        ranker=SymbolRanker(corpus),
        skipped_files=tuple(skipped),
    )
