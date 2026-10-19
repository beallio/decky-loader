#!/usr/bin/env python3
"""Check that the fork's runtime Python packages equal upstream's at the pinned SDK tag.

The fork locks backend/uv.lock itself; upstream locks backend/poetry.lock. The
runtime set (the project's dependencies and their dependencies, on every
platform) must have the same names and versions, except packages listed with a
reason in build-config.json "python_divergence". backend/pyproject.toml's version
must equal the SDK tag. Prints the differences and exits non-zero on any.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG = json.loads((REPO / 'backend-rust/build-config.json').read_text())


def _name(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def upstream_runtime(poetry_lock: str) -> dict[str, str]:
    """Packages that upstream's lock installs for its main (runtime) group."""
    return {_name(package['name']): package['version'] for package in tomllib.loads(poetry_lock)['package']
            if 'main' in package.get('groups', ['main'])}


def fork_runtime(uv_lock: str) -> dict[str, str]:
    """Packages reachable from the project's own dependencies in uv.lock, for every marker.

    A package locked in two versions (split by markers) shows both, so it never equals upstream.
    """
    packages = tomllib.loads(uv_lock)['package']
    project = next(package for package in packages if 'editable' in package.get('source', {}))
    found: dict[str, set[str]] = {}
    seen: set[tuple[str, str, str]] = set()
    pending = list(project.get('dependencies', []))
    while pending:
        dependency = pending.pop()
        name = _name(dependency['name'])
        for package in packages:
            if _name(package['name']) != name or dependency.get('version', package['version']) != package['version']:
                continue
            found.setdefault(name, set()).add(package['version'])
            for extra in ('', *dependency.get('extra', [])):
                if (name, package['version'], extra) in seen:
                    continue
                seen.add((name, package['version'], extra))
                pending += (package.get('optional-dependencies', {}).get(extra, []) if extra
                            else package.get('dependencies', []))
    return {name: ' | '.join(sorted(versions)) for name, versions in found.items()}


def differences(poetry_lock: str, uv_lock: str, allowed: dict[str, str]) -> list[str]:
    upstream, fork = upstream_runtime(poetry_lock), fork_runtime(uv_lock)
    problems = []
    for name in sorted(upstream.keys() | fork.keys()):
        if upstream.get(name) == fork.get(name):
            if name in allowed:
                problems.append(f'{name}: listed in python_divergence but equal to upstream ({fork[name]}); remove the entry')
            continue
        if name in allowed:
            continue
        problems.append(f'{name}: upstream {upstream.get(name, "absent")}, fork {fork.get(name, "absent")}')
    return problems


def check(tag: str) -> list[str]:
    poetry_lock = subprocess.run(['git', 'show', f'{tag}:backend/poetry.lock'], cwd=REPO, check=True,
                                 capture_output=True, text=True).stdout
    problems = differences(poetry_lock, (REPO / 'backend/uv.lock').read_text(), CONFIG.get('python_divergence', {}))
    version = tomllib.loads((REPO / 'backend/pyproject.toml').read_text())['project']['version']
    if tag == CONFIG['sdk']['tag'] and version != tag.removeprefix('v'):
        problems.append(f'backend/pyproject.toml version {version} differs from the SDK tag {tag}')
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tag', default=CONFIG['sdk']['tag'], help='upstream tag to compare with (default: the pinned SDK tag)')
    tag = parser.parse_args().tag
    problems = check(tag)
    for problem in problems:
        print(f'python_parity: {problem}', file=sys.stderr)
    if not problems:
        print(f'python_parity: runtime packages equal upstream {tag}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
