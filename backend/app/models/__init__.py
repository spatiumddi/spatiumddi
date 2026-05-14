from app.models.acme import ACMEAccount
from app.models.ai import AIChatMessage, AIChatSession, AIProvider
from app.models.alerts import AlertEvent, AlertRule
from app.models.appliance import (
    PAIRING_KIND_DHCP,
    PAIRING_KIND_DNS,
    PAIRING_KINDS,
    ApplianceCertificate,
    PairingCode,
)
from app.models.asn import ASN, ASNRpkiRoa, BGPCommunity, BGPPeering
from app.models.audit import AuditLog
from app.models.audit_forward import AuditForwardTarget
from app.models.auth import APIToken, Group, Role, User, UserSession, user_group
from app.models.auth_provider import AuthGroupMapping, AuthProvider
from app.models.backup import BackupTarget
from app.models.base import Base
from app.models.circuit import Circuit
from app.models.conformity import ConformityPolicy, ConformityResult
from app.models.dhcp import (
    DHCPClientClass,
    DHCPConfigOp,
    DHCPLease,
    DHCPLeaseHistory,
    DHCPMACBlock,
    DHCPPool,
    DHCPRecordOp,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.models.dhcp_fingerprint import DHCPFingerprint
from app.models.diagnostics import InternalError
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
from app.models.domain import Domain
from app.models.event_subscription import EventOutbox, EventSubscription
from app.models.feature_module import FeatureModule
from app.models.ipam import (
    CustomFieldDefinition,
    IPAddress,
    IPBlock,
    IPSpace,
    NATMapping,
    RouterZone,
    Subnet,
    SubnetDomain,
    SubnetPlan,
    VLANMapping,
)
from app.models.kubernetes import KubernetesCluster
from app.models.logs import DHCPLogEntry, DNSQueryLogEntry
from app.models.metrics import DHCPMetricSample, DNSMetricSample
from app.models.multicast import (
    MulticastDomain,
    MulticastGroup,
    MulticastGroupPort,
    MulticastMembership,
)
from app.models.network import (
    NetworkArpEntry,
    NetworkDevice,
    NetworkFdbEntry,
    NetworkInterface,
    NetworkNeighbour,
)
from app.models.network_service import NetworkService, NetworkServiceResource
from app.models.nmap import NmapScan
from app.models.oui import OUIVendor
from app.models.overlay import (
    ApplicationCategory,
    OverlayNetwork,
    OverlaySite,
    RoutingPolicy,
)
from app.models.ownership import Customer, Provider, Site
from app.models.proxmox import ProxmoxNode
from app.models.settings import PlatformSettings
from app.models.tailscale import TailscaleTenant
from app.models.unifi import UnifiController
from app.models.vlans import VLAN, Router
from app.models.vrf import VRF

__all__ = [
    "ACMEAccount",
    "AIChatMessage",
    "AIChatSession",
    "AIProvider",
    "Base",
    "AuditLog",
    "AuditForwardTarget",
    "AlertRule",
    "AlertEvent",
    "ApplianceCertificate",
    "PairingCode",
    "PAIRING_KIND_DNS",
    "PAIRING_KIND_DHCP",
    "PAIRING_KINDS",
    "ASN",
    "ASNRpkiRoa",
    "BGPCommunity",
    "BGPPeering",
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
    "SubnetPlan",
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
    "VRF",
    "DHCPServerGroup",
    "DHCPServer",
    "DHCPScope",
    "DHCPPool",
    "DHCPStaticAssignment",
    "DHCPClientClass",
    "DHCPMACBlock",
    "DHCPFingerprint",
    "DHCPLease",
    "DHCPLeaseHistory",
    "DHCPConfigOp",
    "DHCPRecordOp",
    "NATMapping",
    "DNSMetricSample",
    "DHCPMetricSample",
    "DNSQueryLogEntry",
    "DHCPLogEntry",
    "DockerHost",
    "Domain",
    "KubernetesCluster",
    "ProxmoxNode",
    "TailscaleTenant",
    "UnifiController",
    "NetworkDevice",
    "NetworkInterface",
    "NetworkArpEntry",
    "NetworkFdbEntry",
    "NetworkNeighbour",
    "MulticastDomain",
    "MulticastGroup",
    "MulticastGroupPort",
    "MulticastMembership",
    "NmapScan",
    "EventSubscription",
    "EventOutbox",
    "Customer",
    "Site",
    "Provider",
    "Circuit",
    "NetworkService",
    "NetworkServiceResource",
    "OverlayNetwork",
    "OverlaySite",
    "RoutingPolicy",
    "ApplicationCategory",
    "ConformityPolicy",
    "ConformityResult",
    "FeatureModule",
    "InternalError",
    "BackupTarget",
]
