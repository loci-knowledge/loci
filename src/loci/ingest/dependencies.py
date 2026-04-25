"""Code dependency extraction — build `actual` edges between raw nodes.

Parses imports/requires from source files and creates `actual` edges
between the raw nodes that represent those files. This gives the graph
explicit structural relationships (A imports B → A actual→ B) rather than
relying on embedding similarity alone.

Supported languages:
  - Python: `import X`, `from X import Y`
  - JavaScript/TypeScript: `import … from '…'`, `require('…')`, `export … from '…'`

Relative imports are resolved to absolute paths. Stdlib / third-party imports
are ignored (we only create edges between files that exist in the project's
raw nodes).

Safe to run repeatedly — uses INSERT OR IGNORE on the UNIQUE(src, dst, type)
constraint, so re-running after a new file is added only adds new edges.
"""

from __future__ import annotations

import ast
import logging
import re
import sqlite3
from pathlib import Path

from loci.graph.models import new_id

log = logging.getLogger(__name__)

# JS/TS import patterns. We extract the module specifier string.
_JS_IMPORT_RE = re.compile(
    r"""(?:import|export)\s.*?from\s+['"]([^'"]+)['"]"""
    r"""|require\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.DOTALL,
)

_JS_DYNAMIC_IMPORT_RE = re.compile(
    r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)"""
)


def extract_and_write(
    conn: sqlite3.Connection,
    project_id: str,
) -> dict:
    """Extract code dependencies for all raw code nodes in `project_id` and
    write `actual` edges. Returns summary dict."""
    rows = conn.execute(
        """
        SELECT n.id AS node_id, rn.canonical_path AS path, n.subkind AS subkind
        FROM nodes n
        JOIN raw_nodes rn ON rn.node_id = n.id
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
          AND n.kind = 'raw'
          AND n.subkind = 'code'
          AND rn.source_of_truth = 1
        """,
        (project_id,),
    ).fetchall()

    if not rows:
        return {"code_files": 0, "edges_added": 0}

    # Build path → node_id index for fast lookup.
    path_to_id: dict[str, str] = {r["path"]: r["node_id"] for r in rows}

    added = 0
    for r in rows:
        src_path = Path(r["path"])
        src_id = r["node_id"]
        try:
            deps = _extract_imports(src_path)
        except Exception as exc:  # noqa: BLE001
            log.debug("dependencies: failed to parse %s: %s", src_path, exc)
            continue

        for dep_str in deps:
            dst_path = _resolve(src_path, dep_str, path_to_id)
            if dst_path is None:
                continue
            dst_id = path_to_id.get(dst_path)
            if dst_id is None or dst_id == src_id:
                continue
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO edges(id, src, dst, type, weight, created_by)
                    VALUES (?, ?, ?, 'actual', 1.0, 'system')
                    """,
                    (new_id(), src_id, dst_id),
                )
                added += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("dependencies: edge insert failed: %s", exc)

    if added:
        conn.commit()
    return {"code_files": len(rows), "edges_added": added}


def _extract_imports(path: Path) -> list[str]:
    """Return a list of raw import specifiers from `path`."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _extract_python(path)
    if suffix in {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        return _extract_js(path)
    return []


def _extract_python(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Relative: level > 0 means `.module` or `..module`
                prefix = "." * (node.level or 0)
                imports.append(prefix + node.module)
    return imports


def _extract_js(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    specifiers: list[str] = []
    for m in _JS_IMPORT_RE.finditer(source):
        s = m.group(1) or m.group(2)
        if s:
            specifiers.append(s)
    for m in _JS_DYNAMIC_IMPORT_RE.finditer(source):
        if m.group(1):
            specifiers.append(m.group(1))
    return specifiers


def _resolve(src_path: Path, specifier: str, path_to_id: dict[str, str]) -> str | None:
    """Resolve an import specifier to an absolute path string in path_to_id.

    Returns None if we can't map to a known file (e.g., stdlib or npm package).
    """
    # Relative imports: starts with ./ or ../ or Python `.` prefix
    is_relative = specifier.startswith(".") or specifier.startswith("/")
    if is_relative:
        base = src_path.parent
        # Python relative: `.models` → `./models.py`
        # Strip leading dots for Python relative level
        clean = specifier.lstrip(".")
        candidate_dir = base
        # Count leading dots for Python (one `.` = same dir, `..` = parent, etc.)
        dots = len(specifier) - len(clean)
        for _ in range(max(0, dots - 1)):
            candidate_dir = candidate_dir.parent

        # Try several extensions + __init__.py
        candidates = [
            candidate_dir / clean.replace(".", "/"),
            candidate_dir / (clean.replace(".", "/") + ".py"),
            candidate_dir / (clean.replace(".", "/") + ".ts"),
            candidate_dir / (clean.replace(".", "/") + ".tsx"),
            candidate_dir / (clean.replace(".", "/") + ".js"),
            candidate_dir / (clean.replace(".", "/") + ".jsx"),
            candidate_dir / clean.replace(".", "/") / "__init__.py",
            candidate_dir / clean.replace(".", "/") / "index.ts",
            candidate_dir / clean.replace(".", "/") / "index.js",
        ]
        for c in candidates:
            key = str(c.resolve())
            if key in path_to_id:
                return key
        return None
    else:
        # Absolute / package import — only match if the full path exists in
        # path_to_id. We do a prefix scan: `loci.graph.models` → any path
        # ending in `loci/graph/models.py`.
        candidate_tail = specifier.replace(".", "/")
        for abs_path in path_to_id:
            p = Path(abs_path)
            # Match `a/b/c` anywhere in the path (without extension)
            parts = candidate_tail.split("/")
            p_parts = list(p.parts)
            # Walk p_parts looking for parts as a contiguous subsequence
            for i in range(len(p_parts) - len(parts) + 1):
                if p_parts[i : i + len(parts)] == parts:
                    return abs_path
                # Also try with stem (no extension) at the last segment
                stem_parts = p_parts[i : i + len(parts)]
                if stem_parts and i + len(parts) == len(p_parts):
                    stem_parts[-1] = Path(stem_parts[-1]).stem
                if stem_parts == parts:
                    return abs_path
        return None
