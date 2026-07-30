// Operator-facing vocabulary for the OT / industrial registry (issue
// #542, part of #543). Lives in its own module so the OT page and the
// IPAM IP-detail modal render the same words for the same enum value —
// a page that shows "ethernet_ip" while another shows "EtherNet/IP" is
// the kind of drift that makes two surfaces look like two features.
//
// Mirrors the frozensets in ``app.models.ot``; the backend stays the
// single source of truth for which values are legal.

/** ``OT_PROTOCOLS``, ordered the way an OT engineer reads them
 *  (fieldbus-over-Ethernet families first) rather than alphabetically. */
export const OT_PROTOCOL_LABELS: Record<string, string> = {
  profinet: "PROFINET",
  ethernet_ip: "EtherNet/IP",
  modbus_tcp: "Modbus TCP",
  opc_ua: "OPC UA",
  s7comm: "S7comm",
  bacnet_ip: "BACnet/IP",
  dnp3: "DNP3",
  iec61850: "IEC 61850",
  other: "Other",
};

/** ``OT_ROLES``, ordered roughly by Purdue level (process floor upward). */
export const OT_ROLE_LABELS: Record<string, string> = {
  plc: "PLC",
  io_device: "I/O device",
  hmi: "HMI",
  drive: "Drive",
  gateway: "Gateway",
  sensor: "Sensor",
  historian: "Historian",
  ews: "Engineering workstation",
  switch: "Switch",
  other: "Other",
};

/** ``OT_DEVICE_SOURCES`` — how the descriptor got here. */
export const OT_SOURCE_LABELS: Record<string, string> = {
  manual: "Manual",
  import: "Import",
  enip: "EtherNet/IP identity",
  dcp: "PROFINET DCP",
  modbus: "Modbus",
  opcua: "OPC UA",
  profiling: "Device profiling",
};

/** ``PURDUE_LEVELS`` in ascending order. Canonical **strings**, never
 *  numbers: 3.5 (the industrial DMZ) is a real level, and the column is
 *  ``Numeric(2, 1)`` so parsing it as a float would round-trip badly. */
export const PURDUE_LEVELS: readonly string[] = [
  "0",
  "1",
  "2",
  "3",
  "3.5",
  "4",
  "5",
];

export const PURDUE_DESCRIPTIONS: Record<string, string> = {
  "0": "Level 0 — process (sensors / actuators)",
  "1": "Level 1 — basic control (PLC / DCS)",
  "2": "Level 2 — area supervisory (HMI / SCADA)",
  "3": "Level 3 — site operations (historian / MES)",
  "3.5": "Level 3.5 — industrial DMZ",
  "4": "Level 4 — site business planning",
  "5": "Level 5 — enterprise network",
};
