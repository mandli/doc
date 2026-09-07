#!/usr/bin/env python
# encoding: utf-8
r"""
Preflight the environment a Clawpack doc build needs, before Sphinx runs.

autodoc failures are reported as ordinary warnings buried in a long build log,
so an environment that cannot import Clawpack does not *fail* the build -- it
quietly produces a site whose API pages are empty.  That is exactly what
happened to the first docs-publish runs: CI had no sibling source tree, fell
back to a pip-installed clawpack with no dependencies, and emitted 36
``autodoc: failed to import`` warnings that nothing was watching.

This script front-loads that check.  It collects every autodoc target the
documentation actually references, imports each one, and asserts the module
resolved to the pinned source tree rather than to something on ``sys.path`` by
accident.  One legible error instead of 36 opaque warnings.

Usage
-----
    check_doc_env.py [--check-pin] [--verbose]

``--check-pin`` additionally requires the tree to match
``tools/clawpack-ref.txt``.  CI passes it, and so does
``make checkwarnings-update``, since a baseline written against an unpinned
tree is not reproducible.  A plain ``make checkenv`` does not, so a developer
with a slightly different checkout can still use it.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
import traceback


# Also makes fetch_clawpack_src importable for --check-pin.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clawroot import CLAW_ROOT_HELP, SRC_DIR, claw_root  # noqa: E402

CLAW_ROOT = claw_root()

# The directive matters: `automodule:: X` means X is a module, while
# `autoclass:: X` means X is an attribute of the module X[:-1].  Conflating
# them hides real failures -- stripping the last component of a *module* name
# until something imports would happily "resolve" a broken
# clawpack.geoclaw.topotools to a perfectly healthy clawpack.geoclaw.
_AUTODOC_RE = re.compile(
    r'^\s*\.\.\s+auto(module|class|function|exception|data)::\s+(\S+)',
    re.MULTILINE)
_MODULE_DIRECTIVES = {'module'}


def _mocked_prefixes() -> list[str]:
    """The ``autodoc_mock_imports`` list from conf.py, read without importing it.

    conf.py has import-time side effects (it patches logging.config), so it is
    parsed rather than executed.
    """
    conf = os.path.join(SRC_DIR, 'conf.py')
    with open(conf, encoding='utf-8') as fh:
        text = fh.read()
    m = re.search(r'^autodoc_mock_imports\s*=\s*\[(.*?)\]', text,
                  re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return re.findall(r'''['"]([^'"]+)['"]''', m.group(1))


def collect_targets() -> set[tuple[str, str]]:
    """Every ``(directive, dotted-name)`` pair the docs point autodoc at."""
    targets: set[tuple[str, str]] = set()
    for dirpath, dirnames, filenames in os.walk(SRC_DIR):
        dirnames[:] = [d for d in dirnames
                       if d not in ('_build', '_build1', '_static', '_templates')]
        for name in filenames:
            if not name.endswith('.rst'):
                continue
            with open(os.path.join(dirpath, name), encoding='utf-8',
                      errors='replace') as fh:
                targets.update(_AUTODOC_RE.findall(fh.read()))
    return targets


def _resolve(directive: str, target: str) -> tuple[str | None, BaseException | None]:
    """Import what *target* needs and return ``(module_name, error)``.

    For ``automodule`` the target is the module.  For everything else it is an
    attribute of its parent module, so the parent is imported and the attribute
    is required to exist -- which also catches a class that has been renamed
    out from under the docs.
    """
    if directive in _MODULE_DIRECTIVES:
        module, attr = target, None
    elif '.' not in target:
        return None, ImportError(f'{target!r} has no module part')
    else:
        module, attr = target.rsplit('.', 1)

    try:
        obj = importlib.import_module(module)
    except Exception as exc:
        return None, exc

    if attr is not None and not hasattr(obj, attr):
        return None, AttributeError(
            f'module {module!r} has no attribute {attr!r}')
    return module, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check-pin', action='store_true',
                        help='also require the tree to match tools/clawpack-ref.txt')
    parser.add_argument('--verbose', action='store_true',
                        help='print every resolved module path')
    args = parser.parse_args(argv)

    print(f'$CLAW = {CLAW_ROOT}')

    shim = os.path.join(CLAW_ROOT, 'clawpack', '__init__.py')
    if not os.path.exists(shim):
        print(f'\nFAIL: {shim} is missing -- there is no Clawpack source tree '
              f'at\n{CLAW_ROOT}.\n\n' + CLAW_ROOT_HELP, file=sys.stderr)
        return 1

    sys.path.insert(0, CLAW_ROOT)

    if args.check_pin:
        import fetch_clawpack_src  # noqa: E402  (TOOLS_DIR is on sys.path)
        if fetch_clawpack_src.check(CLAW_ROOT) != 0:
            return 1

    # An editable/pip-installed clawpack registers a meta-path finder that wins
    # over sys.path, so getting this far does not guarantee we are documenting
    # the tree we chose.  Check the shim itself before anything else imports.
    import clawpack  # noqa: E402
    if os.path.commonpath([os.path.abspath(clawpack.__file__), CLAW_ROOT]) != CLAW_ROOT:
        print(f'\nFAIL: `import clawpack` resolved to\n  {clawpack.__file__}\n'
              f'which is outside {CLAW_ROOT}.\n\n' + CLAW_ROOT_HELP,
              file=sys.stderr)
        return 1

    mocked = _mocked_prefixes()
    targets = collect_targets()
    if not targets:
        print('FAIL: no autodoc directives found under '
              f'{SRC_DIR} -- has the doc layout changed?', file=sys.stderr)
        return 1

    failures: list[tuple[str, BaseException]] = []
    outside: list[tuple[str, str]] = []
    checked = 0

    for directive, target in sorted(targets, key=lambda t: t[1]):
        if any(target == p or target.startswith(p + '.') for p in mocked):
            continue
        module, error = _resolve(directive, target)
        if module is None:
            failures.append((target, error))
            continue
        checked += 1
        path = getattr(sys.modules.get(module), '__file__', None)
        if args.verbose:
            print(f'  ok  {target:<50} {path}')
        if path and target.startswith('clawpack.'):
            if os.path.commonpath([os.path.abspath(path), CLAW_ROOT]) != CLAW_ROOT:
                outside.append((target, path))

    if failures:
        print(f'\nFAIL: {len(failures)} autodoc target(s) cannot be imported.\n'
              'Their API pages would be silently empty in the built site.\n',
              file=sys.stderr)
        for target, error in failures:
            print(f'  {target}', file=sys.stderr)
            print('      ' + ''.join(
                traceback.format_exception_only(type(error), error)).strip(),
                file=sys.stderr)
        print('\nEither pin the missing dependency in '
              'tools/requirements-docs.txt or\nadd it to autodoc_mock_imports '
              'in conf.py -- a module must be one or\nthe other, never '
              'neither, or local and CI builds will disagree.',
              file=sys.stderr)
        return 1

    if outside:
        print('\nFAIL: clawpack modules resolved outside the source tree.\n'
              'autodoc would document an installed clawpack instead of the\n'
              'pinned checkout, so the docs and the code would drift apart.\n',
              file=sys.stderr)
        for target, path in outside:
            print(f'  {target}\n      {path}', file=sys.stderr)
        return 1

    print(f'ok   {checked} autodoc target(s) import from {CLAW_ROOT}')
    if mocked:
        print(f'ok   {len(mocked)} mocked prefix(es): {", ".join(mocked)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
