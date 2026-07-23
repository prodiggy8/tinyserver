"""Guard test: src/ must implement HTTP from raw sockets, not via stdlib
HTTP helpers. See CLAUDE.md hard constraints."""
import ast
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

FORBIDDEN_MODULES = {
    "http",
    "http.server",
    "http.client",
    "socketserver",
    "wsgiref",
}


def _iter_src_files():
    return sorted(SRC_DIR.glob("*.py")) if SRC_DIR.is_dir() else []


def _imported_module_names(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def test_no_forbidden_http_imports():
    offenders = []
    for path in _iter_src_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for name in _imported_module_names(tree):
            if name in FORBIDDEN_MODULES or name.startswith("urllib"):
                offenders.append(f"{path.name}: import {name}")
    assert not offenders, "forbidden HTTP-library imports found: " + ", ".join(offenders)


def test_no_high_level_asyncio_http_server():
    offenders = []
    for path in _iter_src_files():
        if "asyncio.start_server" in path.read_text():
            offenders.append(path.name)
    assert not offenders, "forbidden asyncio.start_server usage found in: " + ", ".join(offenders)
