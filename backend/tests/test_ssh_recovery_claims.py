"""No surface may promise SSH as an unconditional recovery path (#1009).

Before #1009 the appliance opened `tcp dport 22` unconditionally and could
not stop doing so, and a dozen places said as much — in code comments, in
docs, and in two 422 details and three pieces of UI copy an operator reads at
the moment they are deciding whether to restrict something.

`ssh_lockdown` made that false in one specific case: it retires the port-22
floor, so an operator who turns it on with a scope that excludes them, AND has
also scoped the Web UI, is left with the console. The two settings are
independent and neither can see the other, so neither warns — which makes the
copy the only thing standing between the operator and a surprise.

Correcting those surfaces took three passes. The first swept for
``un-removable``; the second was Copilot catching two the first missed; the
third found three more phrased as "stays open regardless" and "never
restricted". Each pass grepped for the WORDING and so missed the next
paraphrase. This test asserts on the CLAIM instead: any sentence that puts an
SSH/22-is-open assertion next to an unconditional word, without naming the
exception, fails — however it is spelled.

Deliberately narrow: the CLAIM — port 22, stated with "always / never /
regardless" — must sit in one sentence. Saying SSH is open (it is, by default)
is fine; promising it *always* is what is no longer true.

The SSH reference and the exception are both looked for in the SURROUNDING
sentences, not the matched one. That is not laxity, it is what the misses
looked like: FleetTab said "…validated host-side via sshd -t before
activation. Port 22 always stays open in the host firewall as an escape
hatch." — a false promise whose own sentence never says SSH, and which
survived three human sweeps and the first cut of this guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Files an operator's eyes or an operator's API client actually reach.
#: Design docs are excluded — FLEET_FIREWALL.md records the historical design
#: and carries its own annotation about where that design was wrong.
_SEARCH: tuple[tuple[str, str], ...] = (
    ("backend/app", "*.py"),
    ("frontend/src", "*.tsx"),
    ("frontend/src", "*.ts"),
    ("docs/deployment", "*.md"),
    ("docs/features", "*.md"),
    ("appliance/mkosi.extra", "*"),
)

#: A sentence asserting something unconditional…
_ABSOLUTE = re.compile(r"\b(always|never|regardless|unconditional\w*)\b", re.I)
#: …about SSH on 22. ``sshd`` counts too: prose about the daemon and prose
#: about the protocol make the same promise, and much of the UI copy that
#: carries these claims says ``sshd``.
_SSH = re.compile(r"\bsshd?\b", re.I)
_PORT22 = re.compile(r"(?<![\d.])22\b|port[- ]22|dport 22|:22\b", re.I)
#: ...but "a NON-22 ssh port" asserts nothing about 22. Stripped before the
#: port test rather than excluded by a lookbehind, because the hyphen in
#: ``non-22`` is the same character as the one in the legitimate ``port-22``.
_NOT_22 = re.compile(r"\bnon-?22\b", re.I)
#: …that does NOT name the exception.
_EXCEPTION = re.compile(r"lockdown|ssh_lockdown|unless|except|console|retire", re.I)


def _sentences(text: str) -> list[str]:
    """Split on sentence ends, then join wrapped lines.

    Comments and JSX wrap mid-sentence, so a line-based scan would see
    "SSH on port 22 is never restricted, so a" and miss whether the
    exception is named two lines later.
    """
    flat = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.!?]) ", flat)


#: How far past the matched sentence to look for the exception.
#:
#: Sentence-level scanning alone is too strict for prose: a comment that opens
#: "port 22 is opened unconditionally by a management floor" and names the
#: retirement in its NEXT sentence is correct, and a reader meets both. Only
#: text that never qualifies the claim nearby is a real offender — which is
#: exactly the shape the UI strings had (one sentence, full stop, no caveat).
_CONTEXT_SENTENCES = 2


def _offenders() -> list[str]:
    out: list[str] = []
    for subdir, glob in _SEARCH:
        root = REPO / subdir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(glob)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.name == Path(__file__).name:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "ssh" not in text.lower():
                continue
            sentences = _sentences(text)
            for i, sentence in enumerate(sentences):
                # The CLAIM must be in one sentence: port 22, stated
                # unconditionally.
                if not _PORT22.search(_NOT_22.sub("", sentence)):
                    continue
                if not _ABSOLUTE.search(sentence):
                    continue
                window = " ".join(sentences[max(0, i - 1) : i + 1 + _CONTEXT_SENTENCES])
                # ...but the SSH reference may sit in a neighbouring sentence,
                # and routinely does. FleetTab said "…validated host-side via
                # sshd -t before activation. Port 22 always stays open in the
                # host firewall as an escape hatch." — a false promise whose
                # own sentence never says SSH, which is how it survived three
                # sweeps AND the first cut of this guard.
                if not _SSH.search(window):
                    continue
                if _EXCEPTION.search(window):
                    continue
                out.append(f"{path.relative_to(REPO)}: {sentence.strip()[:160]}")
    return out


@pytest.mark.skipif(
    not (REPO / "frontend" / "src").is_dir(),
    reason="full checkout not present (backend-only image)",
)
def test_no_surface_promises_ssh_as_an_unconditional_recovery_path() -> None:
    offenders = _offenders()
    assert not offenders, (
        "a surface asserts SSH/22 is unconditionally open, which "
        "``ssh_lockdown`` (#1009) made false — name the exception (lockdown / "
        "unless / console) or drop the absolute:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.skipif(
    not (REPO / "frontend" / "src").is_dir(),
    reason="full checkout not present (backend-only image)",
)
def test_the_guard_can_actually_fire() -> None:
    """A scanner whose regex never matches is a scanner that always passes.

    The three phrasings that reached production are the fixture: each was
    written by someone who had just read the previous one and paraphrased it.
    """
    bad = [
        "SSH on port 22 stays open regardless, so the appliance is recoverable.",
        "SSH on port 22 is never restricted, so a mistake here is fine.",
        "The un-removable SSH floor on :22 is always open.",
    ]
    for sentence in bad:
        assert _SSH.search(sentence) and _PORT22.search(sentence)
        assert _ABSOLUTE.search(sentence)
        assert not _EXCEPTION.search(sentence), sentence

    # The claim split across sentences — the FleetTab shape, and the reason
    # both the SSH test and the exception test run against the window.
    split = (
        "Rendered config is validated host-side via sshd -t before activation. "
        "Port 22 always stays open in the host firewall as an escape hatch."
    )
    claim = _sentences(split)[1]
    assert _PORT22.search(claim) and _ABSOLUTE.search(claim)
    assert not _SSH.search(claim), "the claim's own sentence never says SSH"
    assert _SSH.search(split), "...but the window does, which is what catches it"
    assert not _EXCEPTION.search(split)

    # A sentence about a NON-22 port asserts nothing about the floor.
    non22 = (
        "The nft fragment is the only thing that opens a NON-22 ssh port, and "
        "`nft -c -f` has always refused a malformed CIDR."
    )
    assert _PORT22.search(non22), "the raw regex does match, which is the trap"
    assert not _PORT22.search(_NOT_22.sub("", non22))

    # ...and the corrected forms must pass, or the guard just bans the topic.
    # Parenthesised: an implicitly-concatenated pair inside a list literal is
    # indistinguishable from a missing comma, which is what the linter says.
    good = [
        (
            "The console always recovers it; SSH does too unless the SSH "
            "source restriction is on and excludes you."
        ),
        ("SSH on port 22 is open by default — the floor is retired only " "under lockdown."),
    ]
    for sentence in good:
        assert _EXCEPTION.search(sentence), sentence


def test_the_guard_reads_the_surrounding_sentences() -> None:
    """Prose that qualifies the claim one sentence later must pass.

    Without the window this flagged four correct comments — each opens with
    the default behaviour and names the retirement immediately after, which is
    how a reader meets it. A guard that cannot express "nearby" would push
    authors to cram the caveat into every sentence, or to delete the guard.
    """
    from_a_real_comment = (
        "Port 22 is opened unconditionally by a management floor. "
        "Retiring that floor removes the recovery channel, so it happens only "
        "under lockdown."
    )
    sentences = _sentences(from_a_real_comment)
    hit = next(
        i
        for i, s in enumerate(sentences)
        if _SSH.search("ssh " + s) and _PORT22.search(s) and _ABSOLUTE.search(s)
    )
    assert not _EXCEPTION.search(sentences[hit])
    window = " ".join(sentences[hit : hit + 1 + _CONTEXT_SENTENCES])
    assert _EXCEPTION.search(window)
