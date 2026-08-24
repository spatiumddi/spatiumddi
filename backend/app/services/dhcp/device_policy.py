"""Compile fingerbank device classes into Kea client-class expressions (#700).

The operator picks device *categories*; this module turns them into
something a DHCP server can actually evaluate.

**Why this is a compiler and not a lookup.** Fingerbank classifies a
device by querying their corpus with the DHCP signature it emitted.
Kea cannot do that mid-packet, and there is no ``device-class ==
IoT`` predicate to render. What Kea *can* test is the signature
itself — option 55 (parameter request list) and option 60 (vendor
class identifier) — which is precisely the input that produced the
classification. So the compiler collects every signature we have
observed and had classified into the selected classes, and emits an
OR over those signatures.

The gap between "the category" and "signatures we have observed in
the category" is the honest limit of the feature, and every surface
states it rather than implying instant enforcement: a device whose
signature is new to us matches nothing until it has been seen and
classified once, which is why v1 is *classify on first lease, apply
on renewal*.

Three properties this module is responsible for, each of which is the
difference between a working policy and a quiet misfire:

**Ambiguous signatures are excluded by default.** DHCP signatures are
not unique to a device class — a minimal parameter-request-list like
``1,3,6,15`` is emitted by embedded Linux in a doorbell and by a rack
server alike. If a signature appears on devices both inside and
outside the selected classes, matching it applies the policy to
devices the operator did not choose. For a feature whose headline use
is "put unknown devices in a quarantine pool with a restricted
resolver", silently including such a signature is how the CEO's
laptop ends up quarantined. They are excluded, counted, and listed;
``include_ambiguous`` is the explicit opt-in.

**Nothing device-controlled is interpolated as a string.** Option 60
is a value the *device* chooses, and it lands inside a config file
whose syntax has quoting. Both halves of every term are emitted as
hex literals (``option[60].hex == 0x4D5346...``), so a vendor-class
of ``' or 1--`` becomes inert bytes rather than expression syntax.
Verified against kea-dhcp4 3.0.3: the hex form parses and the same
payload as a quoted string does not survive as syntax.

**The output is deterministic.** Signatures are sorted before
rendering, because the expression rides the config bundle's ETag —
an unstable ordering would shift the ETag on every rebuild and make
every agent in the group re-fetch and re-render identical config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp_device_policy import DHCPDevicePolicy
from app.models.dhcp_fingerprint import DHCPFingerprint

logger = structlog.get_logger(__name__)

# Cap on signature terms in one compiled expression.
#
# Measured, not guessed: kea-dhcp4 3.0.3 parses a 1024-term / 32 KB
# expression without complaint, so the parser is not the constraint.
# The constraints are per-packet evaluation cost (every term is tested
# against every DISCOVER until one matches) and an operator's ability to
# read what we generated — the issue's own requirement that this not be a
# black box. A policy that hits the cap is reported, never silently
# truncated: ``CompiledPolicy.truncated`` carries the count and the API
# surfaces it as a warning.
MAX_SIGNATURE_TERMS = 128


@dataclass(frozen=True, order=True)
class Signature:
    """One observed DHCP signature.

    ``option_55`` is the comma-separated decimal parameter-request-list
    exactly as ``DHCPFingerprint`` stores it; ``option_60`` is the decoded
    vendor-class string. ``None`` means the device did not send that
    option, which is itself matchable and is *not* the same as an empty
    one — see ``_term``.
    """

    option_55: str | None
    option_60: str | None


@dataclass
class CompiledPolicy:
    """The result of compiling one policy — everything the UI must show."""

    class_name: str
    expression: str
    source: str  # "compiled" | "override" | "empty"
    signatures: list[Signature] = field(default_factory=list)
    ambiguous: list[Signature] = field(default_factory=list)
    # Signatures dropped by MAX_SIGNATURE_TERMS. Never silent.
    truncated: int = 0
    # MACs whose stored fingerprint matches a signature that made it into
    # the expression. This is the "which existing leases land in which
    # class" preview the issue asks for.
    matched_macs: list[str] = field(default_factory=list)
    # Devices fingerbank has NOT classified that share a matched
    # signature. They will receive the policy — same signature, so almost
    # certainly the same kind of device — but the operator did not pick
    # them by name, so the count is surfaced rather than buried.
    unclassified_matches: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def compiled_expression(self) -> str:
        """The expression the compiler produced, override or not.

        When an override is set the rendered ``expression`` is the
        operator's, but the UI still shows what we would have generated so
        the two can be compared — that comparison is the whole point of
        making the override visible.
        """
        return self.expression


def option55_to_hex(csv: str | None) -> str | None:
    """``"1,3,6,15"`` → ``"0103060F"``. ``None`` when unusable.

    Rejects rather than coerces: a byte outside 0-255 or a non-numeric
    field means the stored fingerprint is not a parameter-request-list,
    and guessing at it would emit an expression that matches the wrong
    packets. Returns ``None`` so the caller drops the term.
    """
    if not csv:
        return None
    out: list[str] = []
    for part in csv.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part, 10)
        except ValueError:
            return None
        if not 0 <= value <= 255:
            return None
        out.append(f"{value:02X}")
    return "".join(out) or None


def text_to_hex(text: str | None) -> str | None:
    """Encode a vendor-class string as hex bytes. ``None`` when empty.

    Empty must return ``None``, not ``""``: ``option[60].hex == 0x`` is a
    parse error that fails the *entire* Kea config, taking every unrelated
    class down with it (verified against kea-dhcp4 3.0.3).
    """
    if not text:
        return None
    raw = text.encode("utf-8", errors="replace")
    return raw.hex().upper() or None


def _term(sig: Signature) -> str | None:
    """Render one signature as a Kea sub-expression, or ``None`` to skip.

    A signature with neither option is unmatchable, and must never be
    rendered: an empty term would collapse into an always-true expression
    and apply the policy to every device on the network.

    When the device sent no option 60 the term asserts that absence
    (``not option[60].exists``) rather than ignoring it. Ignoring it would
    silently widen the match to every device sharing the
    parameter-request-list *including* ones that do send a vendor class —
    the exact over-matching the ambiguity check exists to prevent.
    """
    hex55 = option55_to_hex(sig.option_55)
    hex60 = text_to_hex(sig.option_60)
    if hex55 and hex60:
        return f"(option[55].hex == 0x{hex55} and option[60].hex == 0x{hex60})"
    if hex55:
        return f"(option[55].hex == 0x{hex55} and not option[60].exists)"
    if hex60:
        return f"(option[60].hex == 0x{hex60} and not option[55].exists)"
    return None


def build_expression(signatures: list[Signature]) -> str:
    """OR the signature terms together. Empty input yields ``""``.

    An empty string is meaningful downstream and is *not* the same as an
    always-match: the renderers omit the ``test`` key entirely for a
    falsy expression, and a Kea class with no test matches everything.
    Callers must therefore skip rendering the class altogether rather than
    emitting it testless — see ``assemble_device_policy_classes``.
    """
    terms = [t for t in (_term(s) for s in signatures) if t]
    return " or ".join(terms)


def slugify_class_name(name: str) -> str:
    """Derive a Kea-safe, stable client-class name from a policy name.

    Kept conservative — alphanumerics, dash and underscore — because the
    result is both a Kea identifier and the value an operator types into a
    pool's ``class_restriction``. A name that round-trips badly through
    either would break the pool binding silently.
    """
    out = []
    for ch in name.strip():
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch in " \t/.":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug[:100] or "policy"
    return f"spatium-device-{slug}"


async def _load_signatures(
    db: AsyncSession, selected: set[str]
) -> tuple[dict[Signature, list[str]], set[Signature], dict[Signature, int]]:
    """Partition observed fingerprints by whether their class was selected.

    Returns ``(inside, outside, unclassified)`` where ``inside`` maps each
    in-class signature to the MACs that produced it and ``unclassified``
    counts unenriched devices per signature.

    The unclassified counts are returned per-signature rather than
    pre-summed so the caller can tally them against the signatures that
    actually survived ambiguity filtering and the term cap. Summing here
    would count devices behind an *excluded* signature as ones the policy
    will reach — an over-report in exactly the safety reporting an
    operator is relying on to decide whether a quarantine is safe to
    enable.

    Rows fingerbank has not classified are deliberately NOT treated as
    evidence of ambiguity. An unclassified device sharing a signature is
    not a device known to be something else — it is a device we have not
    asked about yet, and excluding an otherwise-clean signature because of
    one would make the feature unusable before the fingerbank key is set.
    They are counted instead, and reported: they will receive the policy.
    """
    res = await db.execute(
        select(
            DHCPFingerprint.mac_address,
            DHCPFingerprint.option_55,
            DHCPFingerprint.option_60,
            DHCPFingerprint.fingerbank_device_class,
        )
    )
    inside: dict[Signature, list[str]] = {}
    outside: set[Signature] = set()
    unclassified: dict[Signature, int] = {}
    for mac, opt55, opt60, device_class in res.all():
        sig = Signature(option_55=opt55, option_60=opt60)
        if device_class is None or device_class == "":
            unclassified[sig] = unclassified.get(sig, 0) + 1
        elif device_class in selected:
            inside.setdefault(sig, []).append(str(mac))
        else:
            outside.add(sig)
    return inside, outside, unclassified


async def compile_device_policy(db: AsyncSession, policy: DHCPDevicePolicy) -> CompiledPolicy:
    """Compile one policy into its Kea client-class expression.

    Always returns a result — a policy that currently matches nothing is a
    normal state (no devices seen yet in the selected classes), not an
    error. The caller decides whether to render it; ``source == "empty"``
    means there is nothing to render.
    """
    selected = {c for c in (policy.device_classes or []) if isinstance(c, str) and c}
    result = CompiledPolicy(class_name=policy.class_name, expression="", source="empty")

    if not selected:
        result.warnings.append(
            "No device classes selected — this policy matches nothing and is not rendered."
        )
    inside, outside, unclassified = (
        await _load_signatures(db, selected) if selected else ({}, set(), {})
    )

    usable: list[Signature] = []
    ambiguous: list[Signature] = []
    for sig in inside:
        # A signature with nothing to match on is dropped here rather than
        # in _term, so it never counts toward the cap or the preview.
        if _term(sig) is None:
            continue
        if sig in outside:
            ambiguous.append(sig)
            if not policy.include_ambiguous:
                continue
        usable.append(sig)

    # Deterministic ordering — the expression rides the bundle ETag.
    usable.sort()
    ambiguous.sort()

    if len(usable) > MAX_SIGNATURE_TERMS:
        result.truncated = len(usable) - MAX_SIGNATURE_TERMS
        usable = usable[:MAX_SIGNATURE_TERMS]
        result.warnings.append(
            f"{result.truncated} signature(s) beyond the {MAX_SIGNATURE_TERMS}-term "
            "cap were not compiled. Narrow the selected device classes, or split "
            "this policy, so the match is complete."
        )

    result.signatures = usable
    result.ambiguous = ambiguous
    # Tallied against the signatures that survived filtering and the cap —
    # not against every in-class signature — so this counts only devices the
    # rendered expression will actually reach.
    usable_set = set(usable)
    unclassified_hits = sum(n for sig, n in unclassified.items() if sig in usable_set)
    result.unclassified_matches = unclassified_hits
    result.matched_macs = sorted(mac for sig in usable for mac in inside.get(sig, []))

    if ambiguous and not policy.include_ambiguous:
        result.warnings.append(
            f"{len(ambiguous)} signature(s) were excluded because devices outside "
            "the selected classes emit them too. Including them would apply this "
            "policy to those devices as well."
        )
    if unclassified_hits:
        result.warnings.append(
            f"{unclassified_hits} device(s) fingerbank has not classified share a "
            "matched signature and will also receive this policy."
        )

    compiled = build_expression(usable)
    override = (policy.match_override or "").strip()
    if override:
        result.expression = override
        result.source = "override"
        result.warnings.append(
            "A manual match expression is in force; the compiled expression below "
            "is shown for comparison and is not rendered."
        )
    elif compiled:
        result.expression = compiled
        result.source = "compiled"
    else:
        result.expression = ""
        result.source = "empty"

    return result


__all__ = [
    "MAX_SIGNATURE_TERMS",
    "CompiledPolicy",
    "Signature",
    "build_expression",
    "compile_device_policy",
    "option55_to_hex",
    "slugify_class_name",
    "text_to_hex",
]
