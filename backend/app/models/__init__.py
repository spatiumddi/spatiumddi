from app.models.acme import ACMEAccount
from app.models.alerts import AlertEvent, AlertRule
from app.models.audit import AuditLog
from app.models.audit_forward import AuditForwardTarget
from app.models.auth import APIToken, Group, Role, User, UserSession, user_group
from app.models.auth_provider import AuthGroupMapping, AuthProvider
from app.models.base import Base
from app.models.dhcp import (
    DHCPClientClass,
    DHCPConfigOp,
    DHCPLease,
    DHCPMACBlock,
    DHCPPool,
    DHCPRecordOp,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.models.dns import (
    DNSAcl,
    DNSAclEntry,
    DNSBlockList,
    DNSBlockListEntry,
    DNSBlockListException,
    DNSRecord,
    DNSServer,
    DNSServerGroup,
    DNSServerOptions,
    DNSServerZoneState,
    DNSTrustAnchor,
    DNSView,
    DNSZone,
)
from app.models.docker import DockerHost
from app.models.ipam import (
    CustomFieldDefinition,
    IPAddress,
    IPBlock,
    IPSpace,
    RouterZone,
    Subnet,
    SubnetDomain,
    VLANMapping,
)
from app.models.kubernetes import KubernetesCluster
from app.models.metrics import DHCPMetricSample, DNSMetricSample
from app.models.oui import OUIVendor
from app.models.proxmox import ProxmoxNode
from app.models.settings import PlatformSettings
from app.models.vlans import VLAN, Router

__all__ = [
    "ACMEAccount",
    "Base",
    "AuditLog",
    "AuditForwardTarget",
    "AlertRule",
    "AlertEvent",
    "User",
    "Group",
    "Role",
    "APIToken",
    "UserSession",
    "user_group",
    "AuthProvider",
    "AuthGroupMapping",
    "IPSpace",
    "IPBlock",
    "RouterZone",
    "Subnet",
    "SubnetDomain",
    "IPAddress",
    "CustomFieldDefinition",
    "VLANMapping",
    "OUIVendor",
    "PlatformSettings",
    "DNSServerGroup",
    "DNSServer",
    "DNSServerZoneState",
    "DNSServerOptions",
    "DNSTrustAnchor",
    "DNSAcl",
    "DNSAclEntry",
    "DNSView",
    "DNSZone",
    "DNSRecord",
    "DNSBlockList",
    "DNSBlockListEntry",
    "DNSBlockListException",
    "Router",
    "VLAN",
    "DHCPServerGroup",
    "DHCPServer",
    "DHCPScope",
    "DHCPPool",
    "DHCPStaticAssignment",
    "DHCPClientClass",
    "DHCPMACBlock",
    "DHCPLease",
    "DHCPConfigOp",
    "DHCPRecordOp",
    "DNSMetricSample",
    "DHCPMetricSample",
    "DockerHost",
    "KubernetesCluster",
    "ProxmoxNode",
]
