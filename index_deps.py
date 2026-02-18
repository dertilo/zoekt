#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Index Python dependencies into Zoekt with custom repo names.

Reads dependencies from pyproject.toml automatically and resolves
PyPI package names to actual directories in site-packages.

Usage:
    uv run index_deps.py --venv .venv --project myproject
    uv run index_deps.py --venv .venv --project myproject --packages pydantic fastapi
    uv run index_deps.py --venv .venv --project myproject --packages-file packages.txt
    uv run index_deps.py --venv .venv --project myproject --pyproject pyproject.toml
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def find_site_packages(venv: Path) -> Path:
    lib = venv / "lib"
    if not lib.exists():
        sys.exit(f"Error: {lib} does not exist")
    python_dirs = [d for d in lib.iterdir() if d.is_dir() and d.name.startswith("python")]
    if not python_dirs:
        sys.exit(f"Error: no python directory found in {lib}")
    site_packages = python_dirs[0] / "site-packages"
    if not site_packages.exists():
        sys.exit(f"Error: {site_packages} does not exist")
    return site_packages


def parse_pyproject_deps(pyproject_path: Path) -> list[str]:
    """Extract dependency names from pyproject.toml (strips version specifiers)."""
    data = tomllib.loads(pyproject_path.read_text())
    deps = data.get("project", {}).get("dependencies", [])
    names = []
    for dep in deps:
        name = re.split(r"[>=<!~\[;]", dep)[0].strip()
        if name:
            names.append(name)
    return names


def resolve_package_dirs(dist_name: str, site_packages: Path) -> list[str]:
    """Given a PyPI distribution name, find its top-level package directories in site-packages.

    Tries: 1) match dist-info RECORD, 2) normalized name as directory.
    """
    # PyPI normalizes - _ . to the same thing; escape first, then replace escaped separators
    pattern = re.sub(r"\\?[-_.]", "[-_.]", re.escape(dist_name))
    for d in site_packages.iterdir():
        if not d.name.endswith(".dist-info"):
            continue
        if not re.match(rf"^{pattern}-[\d]", d.name, re.IGNORECASE):
            continue
        record = d / "RECORD"
        if not record.exists():
            continue
        dirs = set()
        for line in record.read_text().splitlines():
            file_path = line.split(",")[0]
            if "/" in file_path:
                top = file_path.split("/")[0]
                if (
                    not top.endswith(".dist-info")
                    and not top.startswith("_")
                    and top != ".."
                    and (site_packages / top).is_dir()
                ):
                    dirs.add(top)
        if dirs:
            return sorted(dirs)
    # Fallback: try normalized name directly
    normalized = dist_name.lower().replace("-", "_")
    if (site_packages / normalized).is_dir():
        return [normalized]
    return []


def index_directory(
    pkg_dir: str, site_packages: Path, project: str, index_dir: Path
) -> bool:
    pkg_path = site_packages / pkg_dir
    if not pkg_path.is_dir():
        print(f"  SKIP {pkg_dir} (not a directory)")
        return False

    repo_name = f"{project}/deps/{pkg_dir}"
    meta = {"Name": repo_name}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".meta.json", delete=False) as f:
        json.dump(meta, f)
        meta_path = f.name

    cmd = [
        "zoekt-index",
        "-index", str(index_dir),
        "-meta", meta_path,
        str(pkg_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    Path(meta_path).unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"  FAIL {pkg_dir}: {result.stderr.strip()}")
        return False

    print(f"  OK   {pkg_dir} -> r:{repo_name}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Index Python deps into Zoekt")
    parser.add_argument("--venv", required=True, help="Path to .venv directory")
    parser.add_argument("--project", required=True, help="Project name prefix (e.g. myproject)")
    parser.add_argument("--pyproject", default="pyproject.toml", help="Path to pyproject.toml (default: pyproject.toml)")
    parser.add_argument("--packages", nargs="*", help="Explicit package names (overrides pyproject.toml)")
    parser.add_argument("--packages-file", help="File with one package name per line")
    parser.add_argument("--index-dir", default=Path.home() / ".zoekt", help="Zoekt index directory")
    args = parser.parse_args()

    venv = Path(args.venv).resolve()
    index_dir = Path(args.index_dir)
    site_packages = find_site_packages(venv)

    # Collect package names
    packages: list[str] = []
    if args.packages:
        packages = list(args.packages)
    elif args.packages_file:
        pf = Path(args.packages_file)
        packages = [
            line.strip() for line in pf.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        pyproject = Path(args.pyproject)
        if pyproject.exists():
            packages = parse_pyproject_deps(pyproject)
            print(f"Read {len(packages)} dependencies from {pyproject}")
        else:
            sys.exit("Error: no pyproject.toml found and no --packages given")

    if not packages:
        sys.exit("Error: no packages to index")

    # Resolve PyPI names to site-packages directories
    to_index: list[str] = []
    for pkg in packages:
        dirs = resolve_package_dirs(pkg, site_packages)
        if dirs:
            to_index.extend(dirs)
        else:
            print(f"  SKIP {pkg} (not found in site-packages)")

    # Deduplicate while preserving order
    seen = set()
    to_index = [d for d in to_index if not (d in seen or seen.add(d))]

    print(f"Indexing {len(to_index)} package directories from {site_packages}")
    print(f"Project: {args.project}, Index: {index_dir}\n")

    ok, failed = 0, 0
    for pkg_dir in to_index:
        if index_directory(pkg_dir, site_packages, args.project, index_dir):
            ok += 1
        else:
            failed += 1

    print(f"\nDone: {ok} indexed, {failed} failed")


if __name__ == "__main__":
    main()
