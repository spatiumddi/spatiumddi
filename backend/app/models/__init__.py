from app.models.acme import ACMEAccount
from app.models.acme_client import ACMEClientAccount, ACMEHTTPChallenge, ACMEOrder
from app.models.address_set import ADDRESS_SET_RANGE_KINDS, AddressSet
from app.models.ai import AIChatMessage, AIChatSession, AIProvider
from app.models.alerts import AlertEvent, AlertRule
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    APPLIANCE_STATE_PENDING_APPROVAL,
    APPLIANCE_STATE_REJECTED,
    APPLIANCE_STATES,
    Appliance,
    ApplianceCA,
    ApplianceCertificate,
    PairingClaim,
    PairingCode,
)
from app.models.asn import ASN, ASNRpkiRoa, BGPCommunity, BGPPeering
from app.models.audit import AuditLog
from app.models.audit_forward import AuditForwardTarget
from app.models.auth import APIToken, Group, Role, User, UserSession, user_group
from app.models.auth_provider import AuthGroupMapping, AuthProvider
from app.models.backup import BackupTarget
from app.models.base import Base
from app.models.bgp_looking_glass import BGPLGPeer, BGPLGRoute, LookingGlassCollector
from app.models.bgp_monitor import BGPHijackDetection, BGPTrackedPrefix
from app.models.change_request import ApprovalPolicy, ChangeRequest
from app.models.circuit import Circuit
from app.models.cloud import CloudEndpoint
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
from app.models.dnsbl import DNSBLList, DNSBLListing, DNSBLPinnedIP
from app.models.docker import DockerHost
from app.models.domain import Domain
from app.models.event_subscription import EventOutbox, EventSubscription
from app.models.feature_module import FeatureModule
from app.models.firewall import (
    FirewallAlias,
    FirewallApplyState,
    FirewallPolicy,
    FirewallRule,
)
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
from app.models.netbird import NetbirdInstance
from app.models.network import (
    NetworkArpEntry,
    NetworkDevice,
    NetworkFdbEntry,
    NetworkInterface,
    NetworkNeighbour,
)
from app.models.network_service import NetworkService, NetworkServiceResource
from app.models.nmap import NmapScan
from app.models.opnsense import OPNsenseRouter
from app.models.oui import OUIVendor
from app.models.overlay import (
    ApplicationCategory,
    OverlayNetwork,
    OverlaySite,
    RoutingPolicy,
)
from app.models.ownership import Customer, Provider, Site
from app.models.pcap import PacketCapture
from app.models.proxmox import ProxmoxNode
from app.models.saved_view import SavedView
from app.models.settings import PlatformSettings
from app.models.system_upgrade import SystemUpgradeRun
from app.models.tailscale import TailscaleTenant
from app.models.time_bound_grant import TimeBoundGrant
from app.models.tls_cert import TLSCertProbe, TLSCertTarget
from app.models.unifi import UnifiController
from app.models.vlans import VLAN, Router
from app.models.vrf import VRF
from app.models.wol_schedule import (
    WolCalendar,
    WolCalendarEvent,
    WolRun,
    WolRunTarget,
    WolSchedule,
)

__all__ = [
    "ACMEAccount",
    "ACMEClientAccount",
    "ACMEHTTPChallenge",
    "ACMEOrder",
    "ADDRESS_SET_RANGE_KINDS",
    "AddressSet",
    "AIChatMessage",
    "AIChatSession",
    "AIProvider",
    "Base",
    "AuditLog",
    "AuditForwardTarget",
    "AlertRule",
    "AlertEvent",
    "Appliance",
    "ApplianceCA",
    "ApplianceCertificate",
    "APPLIANCE_STATE_APPROVED",
    "APPLIANCE_STATE_PENDING_APPROVAL",
    "APPLIANCE_STATE_REJECTED",
    "APPLIANCE_STATES",
    "PairingClaim",
    "PairingCode",
    "ASN",
    "ASNRpkiRoa",
    "BGPCommunity",
    "BGPPeering",
    "BGPTrackedPrefix",
    "BGPHijackDetection",
    "LookingGlassCollector",
    "BGPLGPeer",
    "BGPLGRoute",
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
    "DNSBLList",
    "DNSBLListing",
    "DNSBLPinnedIP",
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
    "CloudEndpoint",
    "DockerHost",
    "Domain",
    "KubernetesCluster",
    "NetbirdInstance",
    "OPNsenseRouter",
    "ProxmoxNode",
    "SystemUpgradeRun",
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
    "PacketCapture",
    "SavedView",
    "TLSCertTarget",
    "TLSCertProbe",
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
    "FirewallAlias",
    "FirewallApplyState",
    "FirewallPolicy",
    "FirewallRule",
    "InternalError",
    "BackupTarget",
    "TimeBoundGrant",
    "ApprovalPolicy",
    "ChangeRequest",
    "WolSchedule",
    "WolRun",
    "WolRunTarget",
    "WolCalendar",
    "WolCalendarEvent",
]
