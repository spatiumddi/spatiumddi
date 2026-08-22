"""The document must not say "nullable" in a way generators drop (#907).

FastAPI emits OpenAPI 3.1's ``anyOf: [X, {"type": "null"}]``. Strict
generators that cannot model the ``null`` arm skip the member — and skipping a
member drops the WHOLE property from the generated type, with a warning rather
than an error. Measured on this document before the fix: 3,291 schema
properties and 297 query parameters missing from a generated client, including
``limit`` on list endpoints, so the client could not paginate at all.

Two halves are tested, because either alone is a bug:

* the union collapses to the plain schema, and
* the property leaves ``required`` — 971 of them were nullable AND required
  (``str | None`` with no default), and collapsing without that would promise
  a client a value that is routinely null.

No database and no client — the document is generated from the route table.
"""

from __future__ import annotations

from typing import Any

from app.core.openapi_compat import collapse_nullable_unions
from app.main import app


def _schema() -> dict[str, Any]:
    return app.openapi()


# ── the rewrite, shape by shape ───────────────────────────────────────────


def test_two_arm_union_collapses_and_keeps_the_annotations() -> None:
    doc = {
        "properties": {"feed_url": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "F"}},
        "required": ["feed_url"],
    }
    collapse_nullable_unions(doc)
    assert doc["properties"]["feed_url"] == {"type": "string", "title": "F"}
    assert "required" not in doc


def test_constraints_on_the_surviving_arm_are_preserved() -> None:
    """The arm carries the type AND its constraints; the parent carries the
    annotations. Losing ``maximum`` here would drop a bound the server still
    enforces, so a generated client would build requests that 422."""
    doc = {
        "properties": {
            "max_packets": {
                "anyOf": [{"type": "integer", "maximum": 1000000, "minimum": 1}, {"type": "null"}],
                "title": "Max Packets",
                "default": 10000,
            }
        },
        "required": ["max_packets"],
    }
    collapse_nullable_unions(doc)
    assert doc["properties"]["max_packets"] == {
        "type": "integer",
        "maximum": 1000000,
        "minimum": 1,
        "title": "Max Packets",
        "default": 10000,
    }


def test_a_nullable_ref_collapses_to_a_bare_ref() -> None:
    """``$ref`` beside sibling keywords is legal in 3.1 and read
    inconsistently by generators. What sits beside it is FastAPI's generated
    wrapper title, which carries nothing a client needs."""
    doc = {
        "schema": {
            "anyOf": [{"$ref": "#/components/schemas/ACMEAccountSummary"}, {"type": "null"}],
            "title": "Response Get Account Api V1 Appliance Acme Account Get",
        }
    }
    collapse_nullable_unions(doc)
    assert doc["schema"] == {"$ref": "#/components/schemas/ACMEAccountSummary"}


def test_a_multi_arm_union_stays_a_union_minus_the_null() -> None:
    """``Decimal | None`` renders as [number, string-pattern, null]. Dropping
    the null arm leaves a union that is no longer a NULLABLE one — collapsing
    it to a single arm would throw away a form the server accepts."""
    doc = {
        "monthly_cost": {
            "anyOf": [
                {"type": "number"},
                {"type": "string", "pattern": r"^\d+$"},
                {"type": "null"},
            ],
            "title": "Monthly Cost",
        }
    }
    collapse_nullable_unions(doc)
    assert doc["monthly_cost"] == {
        "anyOf": [{"type": "number"}, {"type": "string", "pattern": r"^\d+$"}],
        "title": "Monthly Cost",
    }


def test_only_the_nullable_properties_leave_required() -> None:
    doc = {
        "properties": {
            "id": {"type": "string"},
            "site_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["id", "site_id"],
    }
    collapse_nullable_unions(doc)
    assert doc["required"] == ["id"]


def test_a_type_array_is_normalised_too() -> None:
    """Not a shape FastAPI emits, but ``json_schema_extra`` can hand-write it
    and it breaks the same generators."""
    doc = {"properties": {"note": {"type": ["string", "null"]}}, "required": ["note"]}
    collapse_nullable_unions(doc)
    assert doc["properties"]["note"] == {"type": "string"}
    assert "required" not in doc


def test_a_union_of_nothing_but_null_is_left_alone() -> None:
    """A field annotated ``None`` has no plain schema to collapse to. Emitting
    an empty schema would say "anything goes", which is the opposite."""
    doc = {"properties": {"never": {"anyOf": [{"type": "null"}]}}, "required": ["never"]}
    collapse_nullable_unions(doc)
    assert doc["properties"]["never"] == {"anyOf": [{"type": "null"}]}
    assert "required" not in doc


def test_instance_data_is_not_rewritten() -> None:
    """``default``/``example``/``enum`` hold somebody's literal payload. A
    JSON-Schema-shaped example is data, not a union to collapse."""
    doc = {
        "properties": {
            "tool_input_schema": {
                "type": "object",
                "example": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "default": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            }
        }
    }
    collapse_nullable_unions(doc)
    prop = doc["properties"]["tool_input_schema"]
    assert prop["example"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert prop["default"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


def test_name_keyed_maps_do_not_shadow_keywords() -> None:
    """``default`` is a value keyword under a schema and a RESPONSE under
    ``responses`` — and a property may simply be called ``default``. Suppressing
    the wrong one silently leaves a whole default-response body unrewritten."""
    doc = {
        "responses": {
            "default": {
                "content": {
                    "application/json": {
                        "schema": {
                            "properties": {
                                "default": {"anyOf": [{"type": "string"}, {"type": "null"}]}
                            },
                            "required": ["default"],
                        }
                    }
                }
            }
        }
    }
    collapse_nullable_unions(doc)
    schema = doc["responses"]["default"]["content"]["application/json"]["schema"]
    assert schema["properties"]["default"] == {"type": "string"}
    assert "required" not in schema


def test_a_request_body_schema_keeps_its_required_list() -> None:
    """``required`` is a description on the way out and an ENFORCEMENT on the
    way in. A field annotated ``X | None`` with no default is a key pydantic
    demands, so publishing it as optional would have a generated client omit
    it and take a 422 — trading a silent gap for a runtime failure."""
    doc = {
        "paths": {
            "/thing": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ThingIn"}}
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "ThingIn": {
                    "properties": {"soa": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                    "required": ["soa"],
                },
                "ThingOut": {
                    "properties": {"soa": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                    "required": ["soa"],
                },
            }
        },
    }
    collapse_nullable_unions(doc)
    schemas = doc["components"]["schemas"]
    # The union collapses either way — a dropped property is unusable in both
    # directions — but only the response side loses `required`.
    assert schemas["ThingIn"]["properties"]["soa"] == {"type": "string"}
    assert schemas["ThingIn"]["required"] == ["soa"]
    assert schemas["ThingOut"]["properties"]["soa"] == {"type": "string"}
    assert "required" not in schemas["ThingOut"]


def test_request_reachability_is_transitive() -> None:
    """A nested model is as required as the top-level one that refers to it."""
    doc = {
        "paths": {
            "/thing": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/c/s/Outer"}}}
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Outer": {"properties": {"inner": {"$ref": "#/components/schemas/Inner"}}},
                "Inner": {
                    "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                    "required": ["x"],
                },
            }
        },
    }
    # The requestBody ref is written the long way so the seed is found by the
    # same prefix the closure walks.
    body = doc["paths"]["/thing"]["post"]["requestBody"]
    body["content"]["application/json"]["schema"] = {"$ref": "#/components/schemas/Outer"}
    collapse_nullable_unions(doc)
    assert doc["components"]["schemas"]["Inner"]["required"] == ["x"]


def test_an_inline_request_body_keeps_its_required_list() -> None:
    """No ``$ref`` for the reachability scan to find — the flag has to ride
    the walk itself."""
    doc = {
        "paths": {
            "/thing": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "properties": {
                                        "x": {"anyOf": [{"type": "string"}, {"type": "null"}]}
                                    },
                                    "required": ["x"],
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    collapse_nullable_unions(doc)
    schema = doc["paths"]["/thing"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["x"] == {"type": "string"}
    assert schema["required"] == ["x"]


def test_a_ref_collapse_keeps_prose_and_drops_generated_noise() -> None:
    """The title is FastAPI's; the description is somebody's documentation."""
    doc = {
        "capabilities": {
            "anyOf": [{"$ref": "#/components/schemas/SupervisorCapabilities"}, {"type": "null"}],
            "title": "Capabilities",
            "description": "Supervisor-advertised facts.",
        }
    }
    collapse_nullable_unions(doc)
    assert doc["capabilities"] == {
        "$ref": "#/components/schemas/SupervisorCapabilities",
        "description": "Supervisor-advertised facts.",
    }


def test_the_rewrite_is_idempotent() -> None:
    doc: dict[str, Any] = {
        "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": ["a"],
    }
    once = collapse_nullable_unions(doc)
    twice = collapse_nullable_unions(once)
    assert twice == {"properties": {"a": {"type": "string"}}}


# ── the served document ───────────────────────────────────────────────────


def test_the_served_document_has_no_null_arms_left() -> None:
    """One walk over the real document: any ``{"type": "null"}`` that survives
    is a property a generator will drop from a model."""
    offenders: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "null":
                offenders.append(path)
            if isinstance(node.get("type"), list) and "null" in node["type"]:
                offenders.append(path)
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}")

    walk(_schema(), "")
    assert not offenders, f"{len(offenders)} null arms remain, e.g. {offenders[:5]}"


def test_list_endpoints_still_declare_a_limit_a_client_can_send() -> None:
    """The reported symptom: ``limit`` was nullable, so the generated client
    had no way to pass it and could not paginate at all."""
    checked = 0
    for path, item in _schema()["paths"].items():
        for method, operation in item.items():
            if not isinstance(operation, dict):
                continue
            for param in operation.get("parameters") or []:
                if param.get("name") != "limit" or param.get("in") != "query":
                    continue
                schema = param.get("schema") or {}
                assert "anyOf" not in schema, f"{method.upper()} {path}: {schema}"
                assert schema.get("type"), f"{method.upper()} {path}: {schema}"
                checked += 1
    assert checked, "no limit query parameters found — the assertion proved nothing"


def test_a_response_model_keeps_every_property_a_client_needs() -> None:
    """Spot-check from the issue: ``AddressSetResponse`` lost four fields to
    the generator, all of them nullable."""
    schema = _schema()["components"]["schemas"]["AddressSetResponse"]
    for name in ("customer_id", "site_id", "start_address", "end_address"):
        prop = schema["properties"][name]
        assert "anyOf" not in prop, prop
        assert prop.get("type") or "$ref" in prop, prop
        # Nullability now lives here, which is what a generator reads to make
        # the property optional.
        assert name not in schema.get("required", []), name


def test_no_shared_schema_is_required_and_nullable_at_once() -> None:
    """The invariant the collapse cannot satisfy on its own.

    A schema reachable from BOTH a request body and a response with a property
    that is required AND nullable has no honest publication: say optional and a
    client omits a key the server demands (422); say required and a client
    cannot decode — or send — the null the server routinely uses. The rewrite
    resolves it toward the write side, so the MODEL has to close the gap by
    giving the field a default, as ``ImportedZoneOut.soa`` now does. This test
    is what makes the next one loud instead of silent.

    Nullability is only visible BEFORE the rewrite, so the raw FastAPI document
    is generated here as an oracle — the one place ``get_openapi`` is the right
    call rather than the wrong one, since it is deliberately not what we ship.
    """
    from fastapi.openapi.utils import get_openapi  # noqa: PLC0415 — see docstring

    raw = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )
    schemas = raw["components"]["schemas"]
    shared = _reachable_from(raw, "requestBody") & _reachable_from(raw, "responses")
    assert shared, "reachability found nothing — the assertion proved nothing"

    offenders = [
        f"{name}.{prop}"
        for name in sorted(shared)
        for prop in (schemas[name].get("required") or [])
        if _is_nullable(schemas[name].get("properties", {}).get(prop))
    ]
    assert not offenders, (
        "required AND nullable in a schema used both ways — give the field a "
        f"default so it is honestly optional: {offenders}"
    )


def _is_nullable(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    arms = schema.get("anyOf") or schema.get("oneOf") or []
    return any(isinstance(arm, dict) and arm.get("type") == "null" for arm in arms)


def _reachable_from(document: dict[str, Any], section: str) -> set[str]:
    """Component-schema names reachable from every ``section`` in the paths."""
    prefix = "#/components/schemas/"

    def refs(node: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(prefix):
                found.add(ref[len(prefix) :])
            for value in node.values():
                found |= refs(value)
        elif isinstance(node, list):
            for value in node:
                found |= refs(value)
        return found

    schemas = document["components"]["schemas"]
    seen: set[str] = set()
    pending: list[str] = []
    for item in document["paths"].values():
        for operation in item.values():
            if isinstance(operation, dict) and operation.get(section):
                for name in refs(operation[section]):
                    if name not in seen:
                        seen.add(name)
                        pending.append(name)
    while pending:
        for name in refs(schemas.get(pending.pop(), {})):
            if name not in seen:
                seen.add(name)
                pending.append(name)
    return seen
