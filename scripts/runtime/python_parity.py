#!/usr/bin/env python3
"""Check that the fork's runtime Python packages equal upstream's at the pinned SDK tag.

The fork locks backend/uv.lock itself; upstream locks backend/poetry.lock. The
runtime set (the project's dependencies and their dependencies, with markers
evaluated for the shipped target: CPython from build-config.json on Linux x86-64)
must have the same names and versions, except packages listed with a reason in
build-config.json "python_divergence". backend/pyproject.toml's version must
equal the SDK tag. Prints the differences and exits non-zero on any.
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
# The environment the frozen worker runs in.
TARGET = {
    'python_full_version': CONFIG['python']['version'],
    'python_version': '.'.join(CONFIG['python']['version'].split('.')[:2]),
    'sys_platform': 'linux', 'platform_system': 'Linux', 'platform_machine': 'x86_64', 'os_name': 'posix',
    'implementation_name': 'cpython', 'platform_python_implementation': 'CPython', 'extra': '',
}
_VERSION_VARIABLES = {'python_version', 'python_full_version'}
_TOKEN = re.compile(r"""\s*(?:(?P<op>==|!=|<=|>=|<|>|\(|\))|(?P<word>not\s+in\b|and\b|or\b|in\b)"""
                    r"""|'(?P<s1>[^']*)'|"(?P<s2>[^"]*)"|(?P<var>[a-z_]+))""")


def _name(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def _version(text: str) -> tuple[int, ...]:
    if not re.fullmatch(r'\d+(\.\d+)*', text):
        raise ValueError(f'unsupported version in marker: {text!r}')
    parts = tuple(int(part) for part in text.split('.'))
    return parts + (0,) * (3 - len(parts))


def marker_applies(marker: str | None, target: dict[str, str] = TARGET) -> bool:
    """Evaluate a PEP 508 environment marker for `target`; unknown syntax fails closed."""
    if not marker:
        return True
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(marker.rstrip()):
        match = _TOKEN.match(marker, position)
        if not match or match.end() == position:
            raise ValueError(f'unsupported marker: {marker!r}')
        kind = match.lastgroup or ''
        tokens.append(('str' if kind in ('s1', 's2') else kind, ' '.join(match.group(kind).split())))
        position = match.end()
    index = 0

    def take(kind: str, value: str | None = None) -> str | None:
        nonlocal index
        if index < len(tokens) and tokens[index][0] == kind and value in (None, tokens[index][1]):
            index += 1
            return tokens[index - 1][1]
        return None

    def operand() -> tuple[str, bool]:
        variable = take('var')
        if variable is not None:
            if variable not in target:
                raise ValueError(f'unsupported marker variable {variable!r} in {marker!r}')
            return target[variable], variable in _VERSION_VARIABLES
        literal = take('str')
        if literal is None:
            raise ValueError(f'unsupported marker: {marker!r}')
        return literal, False

    def comparison() -> bool:
        if take('op', '('):
            result = either()
            if take('op', ')') is None:
                raise ValueError(f'unbalanced marker: {marker!r}')
            return result
        left, left_version = operand()
        operator = take('op') or take('word')
        right, right_version = operand()
        if operator in ('in', 'not in'):
            return (left in right) == (operator == 'in')
        if operator not in ('==', '!=', '<', '<=', '>', '>='):
            raise ValueError(f'unsupported marker operator {operator!r} in {marker!r}')
        if left_version or right_version:
            a, b = _version(left), _version(right)
            return {'==': a == b, '!=': a != b, '<': a < b, '<=': a <= b, '>': a > b, '>=': a >= b}[operator]
        if operator not in ('==', '!='):
            raise ValueError(f'unsupported marker comparison in {marker!r}')
        return (left == right) == (operator == '==')

    def both() -> bool:
        result = comparison()
        while take('word', 'and'):
            result = comparison() and result
        return result

    def either() -> bool:
        result = both()
        while take('word', 'or'):
            result = both() or result
        return result

    result = either()
    if index != len(tokens):
        raise ValueError(f'unsupported marker: {marker!r}')
    return result


def upstream_runtime(poetry_lock: str, target: dict[str, str] = TARGET) -> dict[str, str]:
    """Packages that upstream's lock installs for its main (runtime) group on `target`."""
    found = {}
    for package in tomllib.loads(poetry_lock)['package']:
        if 'main' not in package.get('groups', ['main']):
            continue
        markers = package.get('markers')
        if marker_applies(markers.get('main') if isinstance(markers, dict) else markers, target):
            found[_name(package['name'])] = package['version']
    return found


def fork_runtime(uv_lock: str, target: dict[str, str] = TARGET) -> dict[str, str]:
    """Packages reachable from the project's own dependencies in uv.lock on `target`.

    A package locked in two versions for the target shows both, so it never equals upstream.
    """
    packages = tomllib.loads(uv_lock)['package']
    project = next(package for package in packages if 'editable' in package.get('source', {}))
    found: dict[str, set[str]] = {}
    seen: set[tuple[str, str, str]] = set()
    pending = list(project.get('dependencies', []))
    while pending:
        dependency = pending.pop()
        if not marker_applies(dependency.get('marker'), target):
            continue
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
