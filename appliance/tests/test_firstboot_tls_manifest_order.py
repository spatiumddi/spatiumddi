"""The self-signed TLS Secret manifest is staged, not written live (#994).

k3s's addon deployer applied ``spatium-appliance-tls.yaml`` before anything
had created the ``spatium`` namespace, so every fresh install logged

    ApplyManifestFailed  addon/spatium-appliance-tls  ...
    namespaces "spatium" not found

The Secret landed on the retry a minute later, so nothing broke — but a
Warning event on a healthy first boot is how operators learn to skim past
the events feed, which is the one place a real failure would show up.

The fix is ordering, and the ordering has to be *causal* rather than a
guess about filenames: k3s creates one Addon per manifest file and a
separate controller reconciles each Addon's contents, so lexical file order
does not guarantee one addon's objects exist before another's are applied.
firstboot therefore stages the manifest as ``.deferred`` and renames it in
only once ``kubectl get namespace spatium`` succeeds.

The fix NOT taken is worth a test of its own (below): prepending a
``kind: Namespace`` to this manifest, as the issue originally proposed. The
namespace already lives in ``spatium-bootstrap.yaml``, so that would put one
object in two k3s Addon object sets — wrangler prunes objects an addon
previously owned and no longer lists, so the two would fight over the owner
annotation on every resync and removing either manifest would delete the
namespace and everything in it.

Structural assertions, deliberately: these writers are inline in a boot
script that also configures swap, docker and k3s, so exercising them for
real means booting an appliance.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatiumddi-firstboot"
)
BODY = SCRIPT.read_text()


def test_the_cert_is_written_to_the_staged_path() -> None:
    """Nothing writes the live path except the two placement sites."""
    assert 'TLS_CERT_MANIFEST_DEFERRED="${TLS_CERT_MANIFEST}.deferred"' in BODY
    assert 'tmp_manifest="${TLS_CERT_MANIFEST_DEFERRED}.new"' in BODY
    assert 'mv "$tmp_manifest" "$TLS_CERT_MANIFEST_DEFERRED"' in BODY
    # The generator must not reach the live path at all.
    assert 'mv "$tmp_manifest" "$TLS_CERT_MANIFEST"' not in BODY


def test_the_existence_guard_covers_both_paths() -> None:
    """Otherwise a boot interrupted between staging and placement mints a
    SECOND self-signed cert on the next boot — and on an install where the
    operator has since uploaded their own, that is the #590 failure: a
    throwaway cert silently replacing a real one."""
    assert (
        'elif [ ! -f "$TLS_CERT_MANIFEST" ] && [ ! -f "$TLS_CERT_MANIFEST_DEFERRED" ]; then'
    ) in BODY


def test_placement_waits_for_the_namespace() -> None:
    """The wait is the whole point — placing on a timer would be the same
    guess about ordering that the retry already makes for us."""
    fn = BODY[BODY.index("place_deferred_tls_manifest() {") :]
    fn = fn[: fn.index("\nplace_deferred_control_manifest() {")]
    assert "kubectl get namespace spatium" in fn
    assert 'mv -f "$TLS_CERT_MANIFEST_DEFERRED" "$TLS_CERT_MANIFEST"' in fn


def test_placement_is_best_effort_not_blocking() -> None:
    """A namespace that never appears must still get the manifest placed:
    k3s's own retry is then exactly the pre-#994 behaviour, so the worst
    case is the Warning we started with — never a node with no Web UI cert.
    """
    fn = BODY[BODY.index("place_deferred_tls_manifest() {") :]
    fn = fn[: fn.index("\nplace_deferred_control_manifest() {")]
    # The rename is NOT inside the success branch of the wait.
    after_warn = fn[fn.index('if [ "$up" != 1 ]; then') :]
    assert 'mv -f "$TLS_CERT_MANIFEST_DEFERRED" "$TLS_CERT_MANIFEST"' in after_warn
    assert "exit" not in fn


def test_the_k3s_never_ready_path_still_places_it() -> None:
    """The other end of the same argument: firstboot exits 1 when k3s never
    answers /readyz, and must not strand a staged cert behind that exit."""
    tail = BODY[BODY.index("WARN: k3s /readyz did not respond") :]
    assert 'mv -f "$TLS_CERT_MANIFEST_DEFERRED" "$TLS_CERT_MANIFEST"' in tail
    assert tail.index('mv -f "$TLS_CERT_MANIFEST_DEFERRED"') < tail.index("exit 1")


def test_placement_runs_before_the_control_chart() -> None:
    """The frontend pod mounts this Secret. The control chart is deferred
    behind the CNPG webhook, which is later — but only if we place first."""
    ready = BODY[BODY.index('if [ "$ready" = 1 ]; then') :]
    assert ready.index("place_deferred_tls_manifest") < ready.index(
        "place_deferred_control_manifest"
    )


def test_the_namespace_is_owned_by_exactly_one_manifest() -> None:
    """The fix NOT taken. Two k3s Addons owning one cluster-scoped object is
    the #992 failure shape one layer down: wrangler prunes what an addon
    used to own, so removing either manifest would take the namespace — and
    everything in it — with it."""
    tls_block = BODY[BODY.index("TLS_CERT_MANIFEST=") : BODY.index("_env_get() {")]
    assert "kind: Namespace" not in tls_block
    # …and it IS rendered by the bootstrap manifest, which is the one owner.
    assert "_render_namespace_yaml" in BODY
    assert "kind: Namespace" in BODY
