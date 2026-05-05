"""Application catalog — well-known SaaS / voice / video apps + boot seed.

Curated list mirroring common SD-WAN vendor app catalogs (Viptela /
Meraki / Fortinet ship similar libraries). Operators reference these
by name from ``routing_policy.match_value`` when ``match_kind=
application``.

Pattern matches ``services.bgp_communities``: builtin rows
(``is_builtin=true``) are owned by the platform — refreshed on every
boot so an upgrade can reword a description without an admin edit.
Operator-added rows (``is_builtin=false``) stay untouched.

DSCP suggestions follow RFC 4594 where applicable:

* 46 (EF)  — voice
* 34 (AF41) — interactive video
* 26 (AF31) — broadcast video
* 18 (AF21) — low-latency data
* 10 (AF11) — high-throughput data
* 0  (BE)  — best effort

Not all apps have a canonical recommendation; ``default_dscp=None`` is
fine.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.overlay import ApplicationCategory

logger = structlog.get_logger(__name__)


# Each entry: ``(name, description, default_dscp, category)``.
BUILTIN_APPLICATIONS: tuple[tuple[str, str, int | None, str], ...] = (
    # Microsoft / Office productivity
    ("office365", "Microsoft 365 (Outlook, Word, Excel, OneDrive web)", 18, "saas"),
    ("microsoft_teams", "Microsoft Teams chat / meetings / calling", 34, "collaboration"),
    ("onedrive", "OneDrive sync + Teams file storage", 10, "file_transfer"),
    ("sharepoint", "SharePoint Online", 18, "saas"),
    ("exchange_online", "Exchange Online (Outlook / OWA mail flow)", 18, "saas"),
    # Google Workspace
    ("google_workspace", "Google Workspace web (Gmail / Drive / Docs)", 18, "saas"),
    ("google_meet", "Google Meet video calling", 34, "video"),
    ("youtube", "YouTube streaming", 26, "video"),
    # Real-time collaboration
    ("zoom", "Zoom video meetings + Zoom Phone", 34, "video"),
    ("webex", "Cisco Webex meetings + calling", 34, "video"),
    ("slack", "Slack messaging + voice / video huddles", 18, "collaboration"),
    ("discord", "Discord voice / video / chat", 34, "voice"),
    # Voice / SIP
    ("sip_voice", "Generic SIP / RTP voice traffic", 46, "voice"),
    ("ringcentral", "RingCentral cloud PBX", 46, "voice"),
    # Salesforce / CRM / support
    ("salesforce", "Salesforce CRM web + APIs", 18, "saas"),
    ("hubspot", "HubSpot marketing / CRM", 18, "saas"),
    ("zendesk", "Zendesk support desk", 18, "saas"),
    ("servicenow", "ServiceNow ITSM", 18, "saas"),
    # File / storage
    ("dropbox", "Dropbox sync", 10, "file_transfer"),
    ("box", "Box.com sync + collab", 10, "file_transfer"),
    ("github", "GitHub web + git over HTTPS", 10, "saas"),
    ("gitlab", "GitLab web + git over HTTPS", 10, "saas"),
    # Cloud control plane
    ("aws_console", "AWS console + API", 18, "saas"),
    ("azure_portal", "Azure portal + ARM API", 18, "saas"),
    ("gcp_console", "GCP console + APIs", 18, "saas"),
    # Security / endpoint
    ("crowdstrike", "CrowdStrike Falcon EDR cloud", 10, "security"),
    ("okta", "Okta identity / SSO", 18, "security"),
    ("zscaler", "Zscaler internet / private access", 18, "security"),
    # Backup
    ("backup_generic", "Generic site-to-cloud backup traffic", 10, "file_transfer"),
    # Streaming entertainment (frequently rate-shaped on guest WiFi)
    ("netflix", "Netflix streaming", 26, "video"),
    ("spotify", "Spotify audio streaming", 18, "saas"),
    # ML / AI workloads
    ("openai_api", "OpenAI API traffic", 18, "ml"),
    ("anthropic_api", "Anthropic API traffic", 18, "ml"),
)


async def seed_builtin_applications() -> None:
    """Insert / refresh the curated application catalog rows.

    Idempotent: re-runs after each restart and overwrites the
    description / DSCP / category fields on builtin rows. Operator-
    added rows (``is_builtin=false``) are not touched.
    """
    async with AsyncSessionLocal() as session:
        try:
            existing = (
                (
                    await session.execute(
                        select(ApplicationCategory).where(ApplicationCategory.is_builtin.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            by_name = {row.name: row for row in existing}

            for name, description, default_dscp, category in BUILTIN_APPLICATIONS:
                row = by_name.get(name)
                if row is None:
                    session.add(
                        ApplicationCategory(
                            name=name,
                            description=description,
                            default_dscp=default_dscp,
                            category=category,
                            is_builtin=True,
                        )
                    )
                else:
                    row.description = description
                    row.default_dscp = default_dscp
                    row.category = category
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("builtin_applications_seed_skipped", reason=str(exc))


__all__ = ["seed_builtin_applications", "BUILTIN_APPLICATIONS"]
