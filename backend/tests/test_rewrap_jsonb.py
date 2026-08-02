"""#781 — Fernet ciphertext also lives in JSONB, as strings.

The first pass at #781 completed ``ENCRYPTED_COLUMNS`` and guarded it with a
metadata sweep. That guard is structurally blind to the schema's SECOND
storage convention: secrets kept as JSON strings inside a JSONB column. They
are not ``LargeBinary``, their keys do not end in ``_encrypted``, and no
amount of walking ``Base.metadata`` will find them — so SNMPv3 passphrases,
syslog TLS CA bundles, APT GPG keys and private-mirror passwords went on
surviving a cross-install restore still encrypted under the SOURCE key.

Same failure mode as the column half: the restore reports success, first use
throws ``InvalidToken``, and on an appliance the source key is gone the moment
the box is reimaged — which is every restore, since firstboot mints fresh
keys.

Two things are pinned here:

* the walker actually rewraps both document layouts, leaves plaintext alone,
  is idempotent, and survives a partly-populated document;
* the registry cannot drift. Since there is no metadata to diff against, the
  guard diffs against the SOURCE: every ``encrypt_str(...).decode(...)`` call
  in the app — the exact idiom for "embed Fernet ciphertext as a JSON-friendly
  string" — must be at a call site that has been reviewed against the
  registry. It walks the AST rather than matching text, because the argument
  is an arbitrary expression and a guard that quietly fails to see a site is
  worse than no guard.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.services.backup.rewrap import (
    JSONB_ENCRYPTED_FIELDS,
    JsonbSecret,
    _jsonb_secret_sites,
    _rewrap_value,
)

# Every source location that writes Fernet ciphertext into a JSON-friendly
# string, mapped to the registry entry that covers it. A new call site fails
# the drift test below until it is either registered in
# JSONB_ENCRYPTED_FIELDS or listed here with a reason — which is the review
# this file exists to force.
#
# Keyed per CALL SITE (``file::enclosing_function``), not per file: four of
# these live in one module, so a file-level key would let a fifth be added
# there without anyone noticing. Function names are stable across
# reformatting in a way line numbers are not.
_KNOWN_ENC_STR_SITES: dict[str, str] = {
    "app/api/v1/settings/router.py::_merge_syslog_targets": (
        "platform_settings.syslog_targets[].ca_cert_pem"
    ),
    "app/api/v1/settings/router.py::_merge_snmp_v3_users::_resolve": (
        "platform_settings.snmp_v3_users[].{auth,priv}_pass_enc"
    ),
    "app/api/v1/settings/router.py::_merge_apt_gpg_keys": (
        "platform_settings.apt_gpg_keys[].armoured_text_enc"
    ),
    "app/api/v1/settings/router.py::_merge_apt_auth": ("platform_settings.apt_auth[].password_enc"),
    "app/services/ai/operations_writes.py::_apply_update_syslog_settings": (
        "platform_settings.syslog_targets[].ca_cert_pem, via the Operator Copilot"
    ),
    "app/services/backup/targets/secrets_config.py::encrypt_config_secrets": (
        "backup_target.config, via the __enc__: prefix"
    ),
}


class _EncryptStrVisitor(ast.NodeVisitor):
    """Find ``encrypt_str(...).decode(...)`` and name its enclosing scope.

    An AST walk rather than a regex, because the argument is an arbitrary
    expression: ``encrypt_str((x or "").strip()).decode("ascii")`` has nested
    parentheses that no simple pattern matches, and a guard that silently
    fails to see a call site is worse than no guard.
    """

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.sites: list[str] = []

    def _scoped(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.scope.pop()

    # ``ast.NodeVisitor`` dispatches on these exact CamelCase names, so the
    # naming rule doesn't apply here.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._scoped(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._scoped(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "decode"
            and isinstance(func.value, ast.Call)
            and isinstance(func.value.func, ast.Name)
            and func.value.func.id == "encrypt_str"
        ):
            self.sites.append("::".join(self.scope) or "<module>")
        self.generic_visit(node)


def _app_root() -> Path:
    # tests/ sits beside app/ in the backend image and in the repo.
    return Path(__file__).resolve().parent.parent / "app"


def _inline_encrypt_sites() -> set[str]:
    root = _app_root()
    out: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a broken file fails elsewhere
            continue
        visitor = _EncryptStrVisitor()
        visitor.visit(tree)
        rel = path.relative_to(root.parent)
        out.update(f"{rel}::{site}" for site in visitor.sites)
    return out


def test_every_inline_encrypt_site_is_registered() -> None:
    """The drift guard.

    ``encrypt_str(...).decode(...)`` is not a general-purpose idiom — it means
    "put this ciphertext somewhere that needs a string", which in this schema
    means JSONB. So its call sites are a complete enumeration of the places
    this registry has to cover, and a new one appearing is exactly the moment
    a human should check whether the rewrap still spans everything.
    """
    found = _inline_encrypt_sites()

    unregistered = found - set(_KNOWN_ENC_STR_SITES)
    assert not unregistered, (
        "New inline-Fernet write site(s) found. A secret stored as a JSON "
        "string is invisible to the ENCRYPTED_COLUMNS metadata guard, so it "
        "will silently survive a cross-install restore under the SOURCE key "
        "unless it is added to JSONB_ENCRYPTED_FIELDS (#781). Add the field "
        "to the registry, then register the site here: "
        f"{sorted(unregistered)}"
    )

    # And the reverse: a site that goes away should not leave a stale entry
    # implying coverage that no longer has anything to cover.
    stale = set(_KNOWN_ENC_STR_SITES) - found
    assert not stale, f"_KNOWN_ENC_STR_SITES lists sites that no longer exist: {sorted(stale)}"


def test_the_guard_sees_through_nested_parentheses() -> None:
    """The regex this replaced could not, and would have gone quiet.

    A guard that misses a call site is worse than no guard, because it reads
    as a passing check.
    """
    tree = ast.parse('def f():\n    return encrypt_str((x or "").strip()).decode("ascii")\n')
    visitor = _EncryptStrVisitor()
    visitor.visit(tree)

    assert visitor.sites == ["f"]


def test_the_guard_distinguishes_sites_within_one_file() -> None:
    # Four of the six real sites share a module, so a file-level key would
    # let a fifth be added there unnoticed.
    tree = ast.parse(
        "def a():\n    return encrypt_str(v).decode()\n"
        "def b():\n    return encrypt_str(v).decode()\n"
    )
    visitor = _EncryptStrVisitor()
    visitor.visit(tree)

    assert sorted(visitor.sites) == ["a", "b"]


def test_registry_covers_the_known_platform_settings_secrets() -> None:
    covered = {(s.table, s.column) for s in JSONB_ENCRYPTED_FIELDS}
    assert ("platform_settings", "snmp_v3_users") in covered
    assert ("platform_settings", "syslog_targets") in covered
    assert ("platform_settings", "apt_gpg_keys") in covered
    assert ("platform_settings", "apt_auth") in covered
    assert ("backup_target", "config") in covered


def test_registry_entries_are_well_formed() -> None:
    for spec in JSONB_ENCRYPTED_FIELDS:
        assert spec.shape in ("list", "dict"), spec
        # A list-shaped column needs keys to look at; a prefix-marked dict
        # finds its own, so keys there would be misleading dead config.
        if spec.shape == "list":
            assert spec.keys, f"{spec.table}.{spec.column} lists no keys"
            assert not spec.prefix, f"{spec.table}.{spec.column} mixes keys with a prefix"
        else:
            assert spec.prefix, f"{spec.table}.{spec.column} is a dict with no marker prefix"


# ── the walker's discovery half ──────────────────────────────────────


_LIST_SPEC = JsonbSecret(
    "platform_settings", "id", "snmp_v3_users", shape="list", keys=("auth_pass_enc",)
)
_DICT_SPEC = JsonbSecret("backup_target", "id", "config", shape="dict", prefix="__enc__:")


def test_list_shape_finds_only_the_named_keys() -> None:
    doc = [
        {"username": "ops", "auth_pass_enc": "cipher-1", "priv_pass_enc": "cipher-2"},
        {"username": "ro", "auth_pass_enc": "cipher-3"},
    ]

    sites = _jsonb_secret_sites(_LIST_SPEC, doc)

    # ``priv_pass_enc`` is a real secret but not in THIS spec's keys — the
    # real registry lists both. Pinning the narrow behaviour keeps a typo in
    # a future entry from silently rewrapping a plaintext field.
    assert [c for _, _, c in sites] == [b"cipher-1", b"cipher-3"]


def test_list_shape_ignores_plaintext_siblings_and_nulls() -> None:
    doc = [
        {"username": "ops", "auth_pass_enc": None},
        {"username": "ro", "auth_pass_enc": ""},
        {"username": "plain"},
        "not-a-dict",
    ]

    assert _jsonb_secret_sites(_LIST_SPEC, doc) == []


def test_dict_shape_finds_only_prefixed_values() -> None:
    doc = {
        "bucket": "backups",
        "region": "us-east-1",
        "secret_access_key": "__enc__:cipher-1",
        "access_key_id": "AKIA-plaintext",
    }

    sites = _jsonb_secret_sites(_DICT_SPEC, doc)

    assert [(k, c) for _, k, c in sites] == [("secret_access_key", b"cipher-1")]


def test_malformed_documents_do_not_raise() -> None:
    # A hand-edited settings row, or a column whose shape changed under a
    # migration, must not take the whole restore down with it.
    for doc in (None, 42, "a string", {"a": 1}, [[]]):
        assert _jsonb_secret_sites(_LIST_SPEC, doc) == []


# ── the walker's rewrap half ─────────────────────────────────────────


@pytest.fixture
def keys() -> tuple[Fernet, Fernet]:
    return Fernet(Fernet.generate_key()), Fernet(Fernet.generate_key())


def test_value_rewrapped_from_source_to_dest(keys: tuple[Fernet, Fernet]) -> None:
    source, dest = keys
    ciphertext = source.encrypt(b"hunter2")

    new_value, status = _rewrap_value(source, dest, ciphertext)

    assert status == "rewrapped"
    assert new_value is not None
    assert dest.decrypt(new_value) == b"hunter2"


def test_already_destination_wrapped_is_idempotent(keys: tuple[Fernet, Fernet]) -> None:
    # A re-run of the rewrap, or a row written through the API after the
    # restore, must not be counted as a failure.
    source, dest = keys

    _new, status = _rewrap_value(source, dest, dest.encrypt(b"hunter2"))

    assert status == "idempotent"


def test_garbage_is_reported_not_swallowed(keys: tuple[Fernet, Fernet]) -> None:
    source, dest = keys

    _new, status = _rewrap_value(source, dest, b"not-fernet-at-all")

    assert status not in ("rewrapped", "idempotent")


# ── the exclude-secrets scrub ────────────────────────────────────────
#
# The scrub had no test coverage at all, in either direction, which is how
# it came to cover exactly one of the five JSONB secret locations.


def _copy_block(table: str, columns: list[str], rows: list[list[str]]) -> str:
    header = ", ".join(f'"{c}"' for c in columns)
    body = "\n".join("\t".join(r) for r in rows)
    return f'COPY "public"."{table}" ({header}) FROM stdin;\n{body}\n\\.\n'


def _scrubbed_doc(table: str, column: str, doc: object) -> object:
    from app.services.backup.archive import _scrub_dump_text

    dump = _copy_block(table, ["id", column], [["1", json.dumps(doc, separators=(",", ":"))]])
    out = _scrub_dump_text(dump)
    payload = out.splitlines()[1].split("\t")[1]
    return json.loads(payload)


def test_scrub_blanks_snmp_v3_passphrases() -> None:
    doc = [
        {
            "username": "ops",
            "auth_protocol": "SHA",
            "auth_pass_enc": "cipher-1",
            "priv_pass_enc": "cipher-2",
        }
    ]

    out = _scrubbed_doc("platform_settings", "snmp_v3_users", doc)

    assert out == [
        {
            "username": "ops",
            "auth_protocol": "SHA",
            "auth_pass_enc": None,
            "priv_pass_enc": None,
        }
    ]


def test_scrub_blanks_apt_mirror_passwords_and_gpg_keys() -> None:
    auth = _scrubbed_doc(
        "platform_settings",
        "apt_auth",
        [{"machine": "mirror.example", "login": "svc", "password_enc": "cipher"}],
    )
    keys = _scrubbed_doc(
        "platform_settings",
        "apt_gpg_keys",
        [{"key_id": "ABCD", "comment": "mirror", "armoured_text_enc": "cipher"}],
    )

    assert auth[0]["password_enc"] is None
    assert auth[0]["login"] == "svc", "non-secret fields must survive"
    assert keys[0]["armoured_text_enc"] is None
    assert keys[0]["key_id"] == "ABCD"


def test_scrub_blanks_syslog_ca_but_keeps_the_target() -> None:
    out = _scrubbed_doc(
        "platform_settings",
        "syslog_targets",
        [{"host": "logs.example", "port": 6514, "protocol": "tls", "ca_cert_pem": "cipher"}],
    )

    # The target itself is diagnostic information worth keeping; only the
    # CA bundle is a credential.
    assert out == [{"host": "logs.example", "port": 6514, "protocol": "tls", "ca_cert_pem": None}]


def test_scrub_keeps_backup_target_config_empty_string_convention() -> None:
    out = _scrubbed_doc(
        "backup_target",
        "config",
        {"bucket": "b", "secret_access_key": "__enc__:cipher"},
    )

    # Unchanged from before this registry existed — its reader expects "".
    assert out == {"bucket": "b", "secret_access_key": ""}


def test_scrub_leaves_unregistered_tables_alone() -> None:
    from app.services.backup.archive import _scrub_dump_text

    dump = _copy_block("dns_zone", ["id", "name"], [["1", "example.com."]])

    assert _scrub_dump_text(dump) == dump
