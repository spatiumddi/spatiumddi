"""Rich render functions for the TUI — header/ribbon/vitals/ledger/events/footer,
the manifest picker, and the confirm modal. Colour coding follows the §6.5 live
test-watch thresholds. All functions are pure: (snapshot, ui) -> renderable.
"""

from __future__ import annotations

from typing import Any

from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .runmon import RunSnapshot

# Status → colour (mirrors checkpoint statuses).
_STATUS_STYLE = {
    "provisioning": "cyan", "seeding": "cyan", "running": "bold green",
    "draining": "yellow", "collecting": "cyan", "done": "bold bright_green",
    "aborted": "bold red", "invalid": "bold red",
}


def _g(d: dict, *path: str, default: Any = None) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _val(label: str, value: Any, style: str = "white", unit: str = "") -> tuple[Text, Text]:
    v = "—" if value is None else (f"{value}{unit}")
    return Text(label, style="dim"), Text(str(v), style=style)


def _ratio_style(v: float | None, warn: float, crit: float, *, invert: bool = False) -> str:
    """Green/yellow/red by threshold. invert=True means LOWER is worse."""
    if v is None:
        return "dim"
    if invert:
        if v <= crit:
            return "bold red"
        if v <= warn:
            return "yellow"
        return "green"
    if v >= crit:
        return "bold red"
    if v >= warn:
        return "yellow"
    return "green"


def _kv(rows: list[tuple[Text, Text]]) -> Table:
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="left", no_wrap=True)
    t.add_column(justify="right", no_wrap=True)
    for a, b in rows:
        t.add_row(a, b)
    return t


def _fmt_clock(elapsed_s: Any) -> str:
    s = _num(elapsed_s) or 0.0
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    return f"T+{h:02d}:{m:02d}"


# ── header + ribbon ──────────────────────────────────────────────────────────

def header_bar(snap: RunSnapshot, controller_alive: bool) -> Panel:
    st = _g(snap.state, "status", default="—")
    style = _STATUS_STYLE.get(st, "white")
    drive = Text(" ● live" if controller_alive else " ○ attached", style="green" if controller_alive else "dim")
    line = Text()
    line.append("SpatiumDDI perf  ", style="bold cyan")
    line.append(snap.run_id or "(no run)", style="white")
    line.append("   profile=", style="dim")
    line.append(str(_g(snap.state, "profile", default="—")), style="magenta")
    line.append("   status=", style="dim")
    line.append(str(st).upper(), style=style)
    line.append_text(drive)
    return Panel(line, height=3, border_style=style)


def ribbon(snap: RunSnapshot) -> Panel:
    sp = snap.setpoint
    dora = _num(_g(sp, "dhcp", "new_dora_per_s"))
    renew = _num(_g(sp, "dhcp", "renew_per_s"))
    online = _num(_g(sp, "dhcp", "active_devices"))
    qps = _num(_g(sp, "dns", "qps"))
    le = (dora or 0) + (renew or 0)
    op = _num(_g(sp, "operator_mutation_per_s"))
    phase = _g(snap.state, "phase", default="—")

    t = Table.grid(padding=(0, 2))
    for _ in range(7):
        t.add_column(justify="center")
    def cell(label: str, val: str, style: str = "bold white") -> Group:
        return Group(Text(val, style=style, justify="center"), Text(label, style="dim", justify="center"))
    t.add_row(
        cell("PHASE", str(phase), "bold yellow"),
        cell("CLOCK", _fmt_clock(_g(snap.state, "elapsed_test_s")), "bold white"),
        cell("ONLINE", f"{int(online):,}" if online is not None else "—", "bold cyan"),
        cell("DORA/s", f"{dora:.1f}" if dora is not None else "—", "bold white"),
        cell("RENEW/s", f"{renew:.1f}" if renew is not None else "—", "bold white"),
        cell("LEASE-EV/s", f"{le:.0f}", "bold white"),
        cell("DNS qps", f"{qps:,.0f}" if qps is not None else "—", "bold green"),
    )
    flags = Text()
    flags.append(f"  ddns={_g(sp,'ddns_enabled',default='?')}  qlog={_g(sp,'query_log_enabled',default='?')}  operator/s={op if op is not None else '—'}", style="dim")
    return Panel(Group(t, flags), height=5, title="setpoint", title_align="left", border_style="blue")


# ── vitals panes ─────────────────────────────────────────────────────────────

def db_pane(snap: RunSnapshot) -> Panel:
    pg = snap.pg_overview
    conns = _num(_g(pg, "active_connections"))
    maxc = _num(_g(pg, "max_connections")) or 200.0
    ratio = (conns / maxc) if conns is not None else None
    cache = _num(_g(pg, "cache_hit_ratio"))
    longest = _num(_g(pg, "longest_txn_age_s"))
    iit = _num(_g(snap.pg_connections, "by_state", "idle_in_transaction"))
    # psql_probe writes deadlocks under pg_database and waiters as locks_waiting.
    deadlocks = _num(_g(snap.pg_locks, "pg_database", "deadlocks"))
    waiters = _num(_g(snap.pg_locks, "locks_waiting"))
    rows = [
        _val("conns", f"{int(conns)}/{int(maxc)}" if conns is not None else None,
             _ratio_style(ratio, 0.70, 0.85)),
        _val("cache hit", f"{cache*100:.1f}%" if cache is not None else None,
             _ratio_style(cache, 0.95, 0.90, invert=True)),
        _val("idle-in-tx", iit, _ratio_style(iit, 10, 20)),
        _val("longest txn", f"{longest:.0f}s" if longest is not None else None,
             _ratio_style(longest, 30, 120)),
        _val("lock waiters", waiters, _ratio_style(waiters, 1, 5)),
        _val("deadlocks", deadlocks, "bold red" if (deadlocks or 0) > 0 else "green"),
    ]
    return Panel(_kv(rows), title="DATABASE (a)", title_align="left",
                 border_style="red" if (deadlocks or 0) > 0 else "white")


def control_pane(snap: RunSnapshot) -> Panel:
    q = _g(snap.celery_queues, "queues", default={}) or {}
    qtotal = sum(_num(v) or 0 for v in q.values()) if isinstance(q, dict) else 0
    r = snap.redis_overview
    used = _num(_g(r, "used_memory"))
    maxmem = _num(_g(r, "maxmemory")) or 1.0
    evicted = _num(_g(r, "evicted_keys"))
    rows = [
        _val("celery queue", f"{int(qtotal):,}", _ratio_style(qtotal, 5000, 50000)),
        *[_val(f"  q:{k}", int(_num(v) or 0)) for k, v in (q.items() if isinstance(q, dict) else [])],
        _val("redis used", f"{used/maxmem*100:.0f}%" if used is not None else None,
             _ratio_style((used / maxmem) if used is not None else None, 0.80, 0.90)),
        _val("redis evicted", evicted, "bold red" if (evicted or 0) > 0 else "green"),
    ]
    return Panel(_kv(rows), title="CONTROL PLANE", title_align="left", border_style="white")


def load_pane(snap: RunSnapshot) -> Panel:
    rows: list[tuple[Text, Text]] = []
    if not snap.generators:
        rows.append((Text("(no generator data yet)", style="dim"), Text("")))
    for name, g in sorted(snap.generators.items()):
        ach = _num(g.get("achieved_qps")) or _num(g.get("achieved_rate"))
        off = _num(g.get("offered_qps")) or _num(g.get("offered_rate"))
        p99 = _num(g.get("p99_ms")) or _num(g.get("ack_p99_ms")) or _num(g.get("dns_p99_ms"))
        ratio = (ach / off) if (ach is not None and off) else None
        label = name.replace("orchestrator.", "orch.").replace(".stat", "")
        val = []
        if ach is not None:
            val.append(f"{ach:,.0f}")
        if p99 is not None:
            val.append(f"p99 {p99:.0f}ms")
        rows.append((Text(label[:18], style="dim"),
                     Text("  ".join(val) or "—", style=_ratio_style(ratio, 0.95, 0.90, invert=True))))
    return Panel(_kv(rows), title="LOAD (generators)", title_align="left", border_style="white")


def ledger_pane(snap: RunSnapshot) -> Panel:
    d = snap.domain_counts
    cells = [
        ("active leases", _g(d, "active_leases")),
        ("ipam mirror", _g(d, "ipam_mirror")),
        ("dns_record", _g(d, "dns_records")),
        ("record_op pend", _g(d, "dns_record_op_pending")),
        ("record_op tot", _g(d, "dns_record_op_total")),
        ("audit rows", _g(d, "audit_rows")),
        ("lease total", _g(d, "dhcp_lease_total")),
        ("lease history", _g(d, "dhcp_lease_history")),
    ]
    t = Table.grid(padding=(0, 2))
    for _ in cells:
        t.add_column(justify="center")
    t.add_row(*[Text(f"{int(v):,}" if _num(v) is not None else "—",
                     style="bold white", justify="center") for _, v in cells])
    t.add_row(*[Text(label, style="dim", justify="center") for label, _ in cells])
    return Panel(t, title="domain-truth ledger (§8.2.4)", title_align="left", height=4, border_style="white")


def events_pane(snap: RunSnapshot) -> Panel:
    body = Text()
    wd_banner = None
    for ev in snap.events:
        kind = ev.get("kind", "")
        if kind == "watchdog_abort":
            wd_banner = ("bold white on red", f" WATCHDOG ABORT: {'; '.join(ev.get('reasons', []))} ")
        elif kind == "watchdog_throttle":
            wd_banner = ("bold black on yellow", f" WATCHDOG THROTTLE ×{ev.get('factor','?')}: {'; '.join(ev.get('reasons', []))} ")
    for ev in snap.events[-9:]:
        ts = str(ev.get("ts", ""))[11:19]
        kind = ev.get("kind", "")
        extra = " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("ts", "kind"))
        style = "red" if "abort" in kind else "yellow" if "throttle" in kind or "died" in kind else "dim"
        body.append(f"{ts} ", style="dim")
        body.append(f"{kind} ", style=style)
        body.append(f"{extra}\n", style="white")
    if not snap.events:
        body = Text("(no events yet)", style="dim")
    group = [body]
    if wd_banner:
        group = [Text(wd_banner[1], style=wd_banner[0]), Text(""), body]
    return Panel(Group(*group), title="events / watchdog", title_align="left", border_style="white")


def footer(controller_alive: bool, has_run: bool) -> Panel:
    keys = [
        ("F1/s", "Start", not has_run or not controller_alive),
        ("F3/p", "Prune", has_run),
        ("F4/r", "Resume", has_run),
        ("F5/x", "Stop", has_run and controller_alive),
        ("F6", "Abort", has_run and controller_alive),
        ("F8/m", "Manifests", True),
        ("q", "Quit", True),
    ]
    t = Text()
    for k, label, enabled in keys:
        style = "bold black on cyan" if enabled else "dim"
        t.append(f" {k} ", style=style)
        t.append(f"{label}  ", style="white" if enabled else "dim")
    return Panel(t, height=3, border_style="cyan")


# ── composed screens ─────────────────────────────────────────────────────────

def build_dashboard(snap: RunSnapshot, controller_alive: bool) -> Layout:
    root = Layout()
    root.split_column(
        Layout(header_bar(snap, controller_alive), size=3, name="header"),
        Layout(ribbon(snap), size=5, name="ribbon"),
        Layout(name="body"),
        Layout(ledger_pane(snap), size=4, name="ledger"),
        Layout(events_pane(snap), size=9, name="events"),
        Layout(footer(controller_alive, has_run=bool(snap.run_id)), size=3, name="footer"),
    )
    root["body"].split_row(
        Layout(db_pane(snap)), Layout(control_pane(snap)), Layout(load_pane(snap)),
    )
    return root


def build_picker(manifests: list[str], idx: int, preview: list[str]) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column()
    t.add_column()
    left = Text()
    for i, m in enumerate(manifests):
        mark = "▶ " if i == idx else "  "
        style = "bold cyan" if i == idx else "white"
        left.append(f"{mark}{m}\n", style=style)
    right = Text("\n".join(preview), style="dim")
    t.add_row(Panel(left, title="manifests", border_style="cyan"),
              Panel(right, title="resolved plan", border_style="white"))
    help_line = Text("\n ↑/↓ select   Enter preview   F1/s Start   a Attach latest   q Quit", style="dim")
    return Panel(Group(t, help_line), title="SpatiumDDI perf — pick a run", border_style="cyan")


def build_modal(title: str, body: str, selected_yes: bool) -> Align:
    yes = Text(" Yes ", style="bold white on red" if selected_yes else "white on grey23")
    no = Text(" No ", style="white on grey23" if selected_yes else "bold black on cyan")
    choices = Text()
    choices.append_text(yes)
    choices.append("   ")
    choices.append_text(no)
    inner = Group(Text(body, style="white"), Text(""), Align.center(choices),
                  Text("\n ←/→ choose · Enter confirm · Esc cancel", style="dim"))
    return Align.center(Panel(inner, title=title, border_style="yellow", padding=(1, 4)), vertical="middle")
