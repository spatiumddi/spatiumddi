"""The k3s CIDR screen is one summary with the defaults pre-chosen.

It used to be two free-text inputboxes an operator tabbed through on EVERY
install, retyping nothing and getting two chances to fat-finger a value that
is almost always correct. Now it is one menu that SHOWS both ranges and
defaults to "Use these ranges" — one Enter on the common path.

The safety question that change raises is whether the fast path can walk past
the #995 item 22 check, which exists because a LAN on ``10.42.0.0/16`` — the
k3s pod default — collides with the default answer. It cannot: the screen
pre-checks with the QUIET validator before drawing, so a conflict makes
"Change" the default item and says why, and choosing "Use these" anyway is
still refused by the loud validator.

These tests DRIVE the function with a stubbed whiptail, because that is the
only way to see which item is defaulted and whether a conflicting accept is
refused — both are runtime decisions, invisible to any structural check.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_install_k3s_cidr_screen.py -v
"""

from __future__ import annotations

import subprocess

from _installer_source import extract_fn


def _drive(
    *,
    lease_subnet: str = "192.168.1.0/24",
    answers: list[str],
    pod: str = "10.42.0.0/16",
    svc: str = "10.43.0.0/16",
) -> subprocess.CompletedProcess[str]:
    """Run ask_k3s_cidrs with whiptail stubbed.

    ``answers`` are consumed one per whiptail call; each call logs its
    --title and --default-item to stderr so the test can assert on what the
    operator was actually shown.
    """
    fns = "\n".join(
        extract_fn(f)
        for f in (
            "_k3s_cidr_error",
            "_validate_k3s_cidrs",
            "_k3s_cidr_canonical",
            "_whiptail_height",
            "_whiptail_width",
            "ask_k3s_cidrs",
        )
    )
    answers_sh = " ".join(f'"{a}"' for a in answers)
    script = f"""
set -uo pipefail
log() {{ :; }}
BACKTITLE=t
ROLE=control-plane
# _k3s_cidr_error reads these; dhcp mode means it falls back to the lease
# subnet, which is the path #995 item 22 added and the one most installs take.
NET_MODE=dhcp
NET_IP=""
NET_PREFIX=""
K3S_POD_CIDR="{pod}"
K3S_SERVICE_CIDR="{svc}"
_dhcp_lease_subnet() {{ printf '%s' "{lease_subnet}"; }}
ANSWERS=({answers_sh})
IDXF=$(mktemp); echo 0 > "$IDXF"
TRACEF=$(mktemp)
# Mimic REAL whiptail, which is subtler than it looks:
#
#  * it writes its RESULT to STDERR, not stdout — that is the whole reason
#    every call site does `3>&1 1>&2 2>&3`. A stub that printfs to stdout
#    has its answer swapped onto the terminal and hands the caller an empty
#    string, which here spun ask_k3s_cidrs forever.
#  * it is invoked inside $( ), so the stub runs in a SUBSHELL and cannot
#    keep a counter in a variable — the index lives in a file.
#  * the trace goes to a FILE. No file descriptor is safe: the call sites
#    do `3>&1 1>&2 2>&3`, which rebinds fd 1, 2 AND 3 for the duration of
#    the call, so a trace written to any of them lands inside the value the
#    caller captures (POD=SCREEN kind=inputbox... was the symptom).
whiptail() {{
    local title="" dflt="" kind=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --title) title="$2"; shift 2 ;;
            --default-item) dflt="$2"; shift 2 ;;
            --menu) kind=menu; shift ;;
            --inputbox) kind=inputbox; shift ;;
            --msgbox) kind=msgbox; shift ;;
            *) shift ;;
        esac
    done
    echo "SCREEN kind=$kind title=$title default=$dflt" >> "$TRACEF"
    [ "$kind" = msgbox ] && return 0
    local i; i=$(cat "$IDXF")
    if [ "$i" -ge "${{#ANSWERS[@]}}" ]; then
        echo "HARNESS: ran out of scripted answers at call $((i+1))" >> "$TRACEF"
        exit 91
    fi
    local a="${{ANSWERS[$i]}}"; echo $((i+1)) > "$IDXF"
    case "$a" in
        BACK) return 1 ;;
        *) printf '%s' "$a" >&2 ;;
    esac
}}
{fns}
ask_k3s_cidrs
echo "RC=$?"
echo "POD=$K3S_POD_CIDR SVC=$K3S_SERVICE_CIDR"
cat "$TRACEF" >&2
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def test_one_screen_and_one_keypress_on_the_happy_path() -> None:
    r = _drive(answers=["accept"])
    screens = [l for l in r.stderr.splitlines() if l.startswith("SCREEN")]
    assert len(screens) == 1, f"the common path must be ONE screen, got: {screens}"
    assert "kind=menu" in screens[0]
    assert "RC=0" in r.stdout
    assert "POD=10.42.0.0/16 SVC=10.43.0.0/16" in r.stdout


def test_use_these_is_the_default_item_when_there_is_no_conflict() -> None:
    r = _drive(answers=["accept"])
    assert "default=accept" in r.stderr, r.stderr


def test_a_conflicting_lan_makes_change_the_default_item() -> None:
    """#995 item 22: a LAN that already uses the k3s pod default."""
    r = _drive(lease_subnet="10.42.0.0/16", answers=["change", "10.44.0.0/16", "10.45.0.0/16"])
    first = next(l for l in r.stderr.splitlines() if l.startswith("SCREEN"))
    assert "default=change" in first, f"expected Change to be pre-selected: {first}"


def test_accepting_a_conflicting_default_is_still_refused() -> None:
    """The fast path must not become a way past the check.

    Accept once against a colliding LAN, then Back out: the screen must have
    shown the explanatory msgbox and returned to the menu rather than
    committing.
    """
    r = _drive(lease_subnet="10.42.0.0/16", answers=["accept", "BACK"])
    assert "HARNESS:" not in r.stderr, r.stderr
    assert "kind=msgbox" in r.stderr, "no explanation was shown"
    assert "RC=1" in r.stdout, "a conflicting CIDR set was committed"


def test_change_then_back_returns_to_the_summary_not_out_of_the_screen() -> None:
    """Back from the pod box is 'I did not mean to edit', not 'leave'."""
    r = _drive(answers=["change", "BACK", "accept"])
    menus = [l for l in r.stderr.splitlines() if "kind=menu" in l]
    assert len(menus) == 2, f"expected to land back on the summary: {r.stderr}"
    assert "RC=0" in r.stdout


def test_changed_values_are_committed_and_canonicalised() -> None:
    """Host bits set must be stored masked (#974)."""
    r = _drive(answers=["change", "10.44.1.1/16", "10.45.0.0/16"])
    assert "RC=0" in r.stdout
    assert "POD=10.44.0.0/16" in r.stdout, r.stdout


def test_back_from_the_summary_leaves_the_screen() -> None:
    r = _drive(answers=["BACK"])
    assert "RC=1" in r.stdout


def test_an_appliance_node_never_sees_the_screen() -> None:
    """#995 item 14 — pinning CIDRs makes the node impossible to promote."""
    fns = extract_fn("ask_k3s_cidrs")
    r = subprocess.run(
        ["bash", "-c", f'set -uo pipefail\nROLE=appliance\nNET_MODE=dhcp\nNET_IP=""\nNET_PREFIX=""\nlog() {{ :; }}\n{fns}\nask_k3s_cidrs\necho "RC=$?"'],
        capture_output=True, text=True, check=False,
    )
    assert "RC=2" in r.stdout, r.stdout + r.stderr
