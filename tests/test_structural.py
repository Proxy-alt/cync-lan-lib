"""Tests built from the source rather than from a list of cases.

Both bugs the end-to-end suite found in 0.9.0 were instances of a class, not
one-offs:

  * `await`ing a task you just cancelled inside `except Exception`.
    CancelledError has inherited from BaseException since Python 3.8, so it
    escapes and the rest of the teardown never runs. `stop_proxy()` did this
    twice in one function.
  * dereferencing `self.node` on a path that runs before the device has
    identified itself. That bit three separate times - `_setup_mitm_logger`,
    `existing_init`, `start_proxy` - each fixed individually, each time
    without asking where else it might be true.

Fixing an instance and moving on is how a class survives. These walk the
package's own AST and check the whole of it, including code nobody has
written yet, which is the only version of this that stays true.

Each test names what to do about a hit, because a structural test that only
says "no" is a structural test people disable.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import cync_lan

PACKAGE_ROOT = pathlib.Path(cync_lan.__file__).parent
SOURCE_FILES = sorted(PACKAGE_ROOT.rglob("*.py"))


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _relative(path: pathlib.Path) -> str:
    return str(path.relative_to(PACKAGE_ROOT.parent))


def test_the_package_is_actually_being_scanned():
    """A structural test that silently scans nothing passes forever."""
    assert len(SOURCE_FILES) >= 8, SOURCE_FILES
    assert any(p.name == "devices.py" for p in SOURCE_FILES)


# ---------------------------------------------------------------------------
# CancelledError must not be swallowed by `except Exception`
# ---------------------------------------------------------------------------


def _handler_catches_only_ordinary_exceptions(handler: ast.ExceptHandler) -> bool:
    """True when this `except` clause would NOT catch CancelledError."""
    if handler.type is None:
        return False  # bare except: catches BaseException, so it is fine here
    names = []
    targets = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for node in targets:
        names.append(ast.unparse(node))
    if any("BaseException" in n or "CancelledError" in n for n in names):
        return False
    return any(n.endswith("Exception") for n in names)


def _cancelled_expressions(function: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "cancel"
        ):
            found.add(ast.unparse(node.func.value))
    return found


def _find_swallowed_cancellations(tree: ast.AST, path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        cancelled = _cancelled_expressions(function)
        if not cancelled:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Try):
                continue
            awaited = {
                ast.unparse(sub.value)
                for sub in ast.walk(node)
                if isinstance(sub, ast.Await)
            }
            if not (awaited & cancelled):
                continue
            for handler in node.handlers:
                if _handler_catches_only_ordinary_exceptions(handler):
                    problems.append(
                        f"{_relative(path)}:{handler.lineno} in "
                        f"{function.name}(): awaits {sorted(awaited & cancelled)} "
                        f"under `except {ast.unparse(handler.type)}`"
                    )
    return problems


def test_no_cancelled_await_is_swallowed_by_except_exception():
    """The exact shape of the 0.9.1 stop_proxy() bug, checked package-wide.

    If this fires: use
    `contextlib.suppress(asyncio.CancelledError, Exception)`, or catch
    CancelledError explicitly and re-raise it if the task was not the one you
    cancelled. Do not widen the handler to a bare `except:` - that hides real
    cancellation of the *calling* task, which is a different bug.
    """
    problems: list[str] = []
    for path in SOURCE_FILES:
        problems.extend(_find_swallowed_cancellations(_parse(path), path))
    assert not problems, (
        "CancelledError would escape these handlers:\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------
# self.node is None until the device says who it is
# ---------------------------------------------------------------------------

# Where a CyncTCPSession begins life. Anything reachable from these runs
# while the connection is being accepted, which is before any packet has been
# read and therefore before self.node exists.
ACCEPT_TIME_ROOTS = {
    "__init__",
    "existing_init",
    "start_tasks",
    "enable_passthrough",
    "start_proxy",
    "_setup_mitm_logger",
}


def _session_class() -> ast.ClassDef:
    tree = _parse(PACKAGE_ROOT / "devices.py")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CyncTCPSession":
            return node
    raise AssertionError("CyncTCPSession not found - has it been renamed?")


def _methods(cls: ast.ClassDef) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def _reachable_from(roots: set[str], methods: dict[str, ast.AST]) -> set[str]:
    """Close over `self.x()` calls, so adding a helper to the accept path
    brings it under this test automatically rather than when someone
    remembers to list it."""
    seen: set[str] = set()
    queue = [name for name in roots if name in methods]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(methods[name]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr in methods
            ):
                queue.append(node.func.attr)
    return seen


def _unguarded_node_reads(method: ast.AST) -> list[tuple[int, str]]:
    """`self.node.<attr>` not sitting behind a check on self.node.

    getattr(self.node, ...) is the accepted way to write this and is not
    reported; neither is a read inside `if self.node:` or `if self.node is
    not None:`.
    """
    guarded_lines: set[int] = set()
    for node in ast.walk(method):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "self.node" in test:
                for sub in ast.walk(node):
                    if hasattr(sub, "lineno"):
                        guarded_lines.add(sub.lineno)

    hits: list[tuple[int, str]] = []
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "node"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.lineno not in guarded_lines
        ):
            hits.append((node.lineno, f"self.node.{node.attr}"))
    return hits


def test_accept_time_paths_do_not_dereference_a_node_that_does_not_exist():
    """The bug that bit three times, asked of every method on the path.

    If this fires: `getattr(self.node, "id", None)`, or put the read behind
    an explicit `if self.node:`. The failure mode is nastier than it looks -
    in start_proxy() the dereference was only building a task *name*, and the
    AttributeError surfaced as "failed to start MITM" while the session
    quietly fell back to local-only.
    """
    methods = _methods(_session_class())
    reachable = _reachable_from(ACCEPT_TIME_ROOTS, methods)
    assert "start_proxy" in reachable, "the reachability walk found nothing"

    problems = []
    for name in sorted(reachable):
        for lineno, expression in _unguarded_node_reads(methods[name]):
            problems.append(f"devices.py:{lineno} in {name}(): {expression}")
    assert not problems, (
        "self.node is None while a connection is being accepted:\n  "
        + "\n  ".join(problems)
    )


@pytest.mark.parametrize("root", sorted(ACCEPT_TIME_ROOTS))
def test_the_accept_time_roots_still_exist(root: str):
    """A renamed method would silently shrink the set above to nothing."""
    assert root in _methods(_session_class()), (
        f"{root}() is gone - update ACCEPT_TIME_ROOTS to match"
    )
