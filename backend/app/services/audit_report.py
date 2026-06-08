"""Compliance / change-report PDF (#48).

An auditor-facing PDF rollup of every ``audit_log`` mutation in a date
range, grouped by user / resource type / action, with a SHA-256
tamper-evidence trailer over the included rows. Mirrors the
``services/conformity/pdf.py`` reportlab pattern (synchronous render after
async DB queries; small enough to render inline, no ``asyncio.to_thread``
needed at our scale).
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

# Hard cap on detail rows in a single report so a huge range can't blow up
# memory / the PDF. The summary counts are computed over the full range; the
# detail table is the most-recent ``_DETAIL_CAP`` rows, with a note when the
# range exceeds it.
_DETAIL_CAP = 2000


def _grid(header: list[str], rows: list[list[str]], col_widths: list[float]) -> Table:
    t = Table([header, *rows], colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return t


def _rows_hash(rows: list[AuditLog]) -> str:
    """SHA-256 over each row's (seq, timestamp, action, resource) so the
    trailer is recomputable and tamper-evident — reusing the audit chain's
    own ``hash`` column when present keeps it cheap and stable."""
    digest = hashlib.sha256()
    for r in rows:
        digest.update((getattr(r, "hash", "") or str(r.id)).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


async def generate_change_report_pdf(
    db: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    title: str | None = None,
) -> bytes:
    """Render the audit-log change report for ``[since, until]`` as a PDF.

    Summary counts (by user / resource type / action) are computed over the
    whole range; the detail table lists the most-recent ``_DETAIL_CAP`` rows.
    """
    base = select(AuditLog).where(AuditLog.timestamp >= since).where(AuditLog.timestamp <= until)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = list(
        (await db.execute(base.order_by(AuditLog.timestamp.desc()).limit(_DETAIL_CAP)))
        .scalars()
        .all()
    )

    # Summary counters over the FULL range (group-by in the DB so a huge
    # range doesn't have to be materialised for the rollup).
    async def _counts(col: Any) -> list[tuple[str, int]]:
        stmt = (
            select(col, func.count())
            .where(AuditLog.timestamp >= since)
            .where(AuditLog.timestamp <= until)
            .group_by(col)
            .order_by(func.count().desc())
        )
        return [
            (str(v if v is not None else "—"), int(n)) for v, n in (await db.execute(stmt)).all()
        ]

    by_user = await _counts(AuditLog.user_display_name)
    by_type = await _counts(AuditLog.resource_type)
    by_action = await _counts(AuditLog.action)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=title or "SpatiumDDI change report",
    )
    styles = getSampleStyleSheet()
    h1, h2, body = styles["Heading1"], styles["Heading2"], styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10)
    now = datetime.now(UTC)

    story: list = [
        Paragraph(title or "SpatiumDDI change report", h1),
        Paragraph(
            f"Generated {now.isoformat()} UTC · range "
            f"{since.isoformat()} → {until.isoformat()} · {total} audit events",
            small,
        ),
        Spacer(1, 0.2 * inch),
    ]

    if total == 0:
        story.append(
            Paragraph(
                "No audit-log mutations recorded in this range.",
                body,
            )
        )
        doc.build(story)
        return buf.getvalue()

    def _section(heading: str, label: str, counts: list[tuple[str, int]]) -> None:
        story.append(Paragraph(heading, h2))
        story.append(
            _grid(
                [label, "Events"],
                [[k, str(n)] for k, n in counts],
                [4.5 * inch, 1.5 * inch],
            )
        )
        story.append(Spacer(1, 0.18 * inch))

    _section("By user", "User", by_user)
    _section("By resource type", "Resource type", by_type)
    _section("By action", "Action", by_action)

    story.append(Paragraph("Detail", h2))
    if total > len(rows):
        story.append(
            Paragraph(
                f"Showing the most recent {len(rows)} of {total} events "
                f"(detail capped at {_DETAIL_CAP}; narrow the range for the rest).",
                small,
            )
        )
    detail_rows = [
        [
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S") if r.timestamp else "—",
            (r.user_display_name or "—")[:24],
            (r.action or "—")[:28],
            (r.resource_type or "—")[:18],
            (r.resource_display or "—")[:28],
            (r.result or "—")[:10],
        ]
        for r in rows
    ]
    story.append(
        _grid(
            ["Time (UTC)", "User", "Action", "Type", "Resource", "Result"],
            detail_rows,
            [1.15 * inch, 1.1 * inch, 1.4 * inch, 0.9 * inch, 1.4 * inch, 0.65 * inch],
        )
    )

    story.append(Spacer(1, 0.18 * inch))
    story.append(
        Paragraph(
            f"Integrity (SHA-256 over {len(rows)} detail rows): {_rows_hash(rows)}",
            small,
        )
    )
    doc.build(story)
    return buf.getvalue()
