"""No source reference to a column/attribute a model doesn't have (#923).

``select(DHCPScope.id).where(DHCPScope.server_group_id == group_id)`` is
valid Python, passes ruff, passes ``mypy`` — a declarative model is not
typed strictly enough for an unknown attribute to be an error — and raises
``AttributeError`` the first time the line executes. Every such reference is
therefore a guaranteed 500 on whatever path reaches it, invisible until
someone exercises exactly that branch.

That is not theoretical. This check was written after #923 reported
accepted-write / broken-read 500s, and on its first run over ``backend/app``
it found six live instances:

* ``DHCPScope.server_group_id`` in the phone-profile scope validator — so
  assigning a phone profile to any scope answered 500, which means the
  validation that function exists to perform had never once run.
* ``DHCPServer.group_id`` (twice) and ``NetworkDevice.space_id`` in Operator
  Copilot tools — each the documented filter argument of its own tool.
* ``DHCPScope.subnet`` (twice) in ``list_dhcp_scopes``, one of them the
  unconditional ``order_by``, so that tool had never returned a scope.

The walk is deliberately conservative: it only looks at ``Name.attribute``
where ``Name`` is the bare class name of a mapped model, and only flags an
attribute absent from ``dir(cls)``. That misses references through an alias
or a variable, and it cannot see a dynamically-built string — but it has no
false positives, so it needs no baseline file and any finding is a real bug.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.base import Base

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _mapped_attributes() -> dict[str, set[str]]:
    """``{ClassName: everything addressable on it}`` for every mapped model."""
    return {mapper.class_.__name__: set(dir(mapper.class_)) for mapper in Base.registry.mappers}


def test_no_references_to_nonexistent_model_attributes() -> None:
    models = _mapped_attributes()
    assert models, "no mapped models discovered — the import graph changed"

    findings: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            attributes = models.get(node.value.id)
            if attributes is None:
                continue
            # Dunders are resolved on the metaclass and never appear in a
            # query expression; skipping them keeps the walk to real usage.
            if node.attr.startswith("__") or node.attr in attributes:
                continue
            findings.append(
                f"{path.relative_to(APP_ROOT.parent)}:{node.lineno}: "
                f"{node.value.id}.{node.attr} does not exist"
            )

    assert not findings, "references to attributes no model has:\n  " + "\n  ".join(findings)
