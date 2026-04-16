import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  dhcpApi,
  ipamApi,
  settingsApi,
  type DHCPScope,
  type DHCPOption,
} from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";
import { DHCPOptionsEditor } from "./DHCPOptionsEditor";

// Suggest a dynamic pool range for a v4 subnet: skip the first 10 hosts
// (reserve for infra / static) and the last host (broadcast). Returns null
// for IPv6 or subnets too small to be useful.
function suggestRange(
  subnet: { network?: string | null } | undefined,
): { start: string; end: string } | null {
  if (!subnet?.network) return null;
  const [cidr, prefixStr] = subnet.network.split("/");
  if (!cidr || !prefixStr || cidr.includes(":")) return null;
  const prefix = parseInt(prefixStr, 10);
  if (prefix < 8 || prefix > 30) return null;
  const parts = cidr.split(".").map((n) => parseInt(n, 10));
  if (parts.length !== 4 || parts.some((n) => isNaN(n))) return null;
  const netInt =
    ((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) >>> 0;
  const mask = (0xffffffff << (32 - prefix)) >>> 0;
  const base = (netInt & mask) >>> 0;
  const hostBits = 32 - prefix;
  const total = 1 << hostBits;
  if (total < 16) return null;
  const startInt = (base + 10) >>> 0;
  const endInt = (base + total - 2) >>> 0;
  const fmt = (n: number) =>
    `${(n >>> 24) & 0xff}.${(n >>> 16) & 0xff}.${(n >>> 8) & 0xff}.${n & 0xff}`;
  return { start: fmt(startInt), end: fmt(endInt) };
}

export function CreateScopeModal({
  scope,
  subnetId: fixedSubnetId,
  onClose,
}: {
  scope?: DHCPScope;
  /** When creating from a subnet, pin the subnet; otherwise show a picker. */
  subnetId?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!scope;
  const [subnetId, setSubnetId] = useState<string>(
    scope?.subnet_id ?? fixedSubnetId ?? "",
  );
  const [serverId, setServerId] = useState<string>(scope?.server_id ?? "");
  const [name, setName] = useState(scope?.name ?? "");
  const [description, setDescription] = useState(scope?.description ?? "");
  const [enabled, setEnabled] = useState(scope?.enabled ?? true);
  const [leaseTime, setLeaseTime] = useState(String(scope?.lease_time ?? 86400));
  const [minLease, setMinLease] = useState(
    scope?.min_lease_time != null ? String(scope.min_lease_time) : "",
  );
  const [maxLease, setMaxLease] = useState(
    scope?.max_lease_time != null ? String(scope.max_lease_time) : "",
  );
  const [ddnsEnabled, setDdnsEnabled] = useState(scope?.ddns_enabled ?? false);
  const [ddnsPolicy, setDdnsPolicy] = useState(
    scope?.ddns_hostname_policy ?? "client",
  );
  const [ddnsDomain, setDdnsDomain] = useState(
    scope?.ddns_domain_override ?? "",
  );
  const [hostnameSync, setHostnameSync] = useState(
    scope?.hostname_sync_mode ?? "ipam",
  );
  const [options, setOptions] = useState<DHCPOption[]>(scope?.options ?? []);
  // Initial pool — only used when creating; edits happen in the Pools tab.
  const [poolStart, setPoolStart] = useState("");
  const [poolEnd, setPoolEnd] = useState("");
  const [error, setError] = useState("");

  const { data: subnets = [] } = useQuery({
    queryKey: ["subnets"],
    queryFn: () => ipamApi.listSubnets(),
    enabled: !fixedSubnetId,
  });
  const { data: servers = [] } = useQuery({
    queryKey: ["dhcp-servers"],
    queryFn: () => dhcpApi.listServers(),
  });
  // Settings + specific subnet feed the auto-prefill for new scopes.
  const { data: settings } = useQuery({
    queryKey: ["settings"],
    queryFn: settingsApi.get,
    enabled: !editing,
  });
  const { data: subnetDetail } = useQuery({
    queryKey: ["subnet", subnetId],
    queryFn: () => ipamApi.getSubnet(subnetId),
    enabled: !editing && !!subnetId,
  });

  const [prefilled, setPrefilled] = useState(false);
  useEffect(() => {
    if (editing || prefilled) return;
    if (!settings && !subnetDetail) return;
    const next: DHCPOption[] = [];
    const gw = subnetDetail?.gateway;
    if (gw) next.push({ code: 3, value: [gw] });
    if (settings?.dhcp_default_dns_servers?.length)
      next.push({ code: 6, value: settings.dhcp_default_dns_servers });
    if (settings?.dhcp_default_domain_name)
      next.push({ code: 15, value: settings.dhcp_default_domain_name });
    if (settings?.dhcp_default_domain_search?.length)
      next.push({ code: 119, value: settings.dhcp_default_domain_search });
    if (settings?.dhcp_default_ntp_servers?.length)
      next.push({ code: 42, value: settings.dhcp_default_ntp_servers });
    if (next.length) setOptions(next);
    if (settings?.dhcp_default_lease_time)
      setLeaseTime(String(settings.dhcp_default_lease_time));
    // Suggest a pool range: skip the first 10 and last 1 host of the subnet.
    // User can freely edit or clear.
    const range = suggestRange(subnetDetail);
    if (range) {
      setPoolStart(range.start);
      setPoolEnd(range.end);
    }
    setPrefilled(true);
  }, [editing, prefilled, settings, subnetDetail]);

  const mut = useMutation({
    mutationFn: () => {
      const data: Partial<DHCPScope> = {
        server_id: serverId || null,
        name,
        description,
        enabled,
        lease_time: parseInt(leaseTime, 10) || 86400,
        min_lease_time: minLease ? parseInt(minLease, 10) : null,
        max_lease_time: maxLease ? parseInt(maxLease, 10) : null,
        ddns_enabled: ddnsEnabled,
        ddns_hostname_policy: ddnsEnabled ? ddnsPolicy : null,
        ddns_domain_override: ddnsDomain || null,
        hostname_sync_mode: hostnameSync,
        options,
      };
      if (editing) return dhcpApi.updateScope(scope!.id, data);
      return dhcpApi.createScope(subnetId, data).then(async (created) => {
        if (poolStart && poolEnd) {
          await dhcpApi.createPool(created.id, {
            name: "default",
            start_ip: poolStart,
            end_ip: poolEnd,
            pool_type: "dynamic",
          });
        }
        return created;
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-scopes"] });
      qc.invalidateQueries({ queryKey: ["dhcp-scopes-subnet", subnetId] });
      onClose();
    },
    onError: (e) => setError(errMsg(e, "Failed to save scope")),
  });

  return (
    <Modal
      title={editing ? "Edit DHCP Scope" : "New DHCP Scope"}
      onClose={onClose}
      wide
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mut.mutate();
        }}
        className="space-y-3"
      >
        {!fixedSubnetId && !editing && (
          <Field label="Subnet">
            <select
              className={inputCls}
              value={subnetId}
              onChange={(e) => setSubnetId(e.target.value)}
              required
            >
              <option value="">— Pick a subnet —</option>
              {subnets.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.network} {s.name ? `— ${s.name}` : ""}
                </option>
              ))}
            </select>
          </Field>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">
            <input
              className={inputCls}
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </Field>
          <Field label="Server (optional)">
            <select
              className={inputCls}
              value={serverId}
              onChange={(e) => setServerId(e.target.value)}
            >
              <option value="">— Any in group —</option>
              {servers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.driver})
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        <div className="grid grid-cols-3 gap-3">
          <Field label="Lease Time (sec)">
            <input
              type="number"
              className={inputCls}
              value={leaseTime}
              onChange={(e) => setLeaseTime(e.target.value)}
            />
          </Field>
          <Field label="Min Lease (sec)">
            <input
              type="number"
              className={inputCls}
              value={minLease}
              onChange={(e) => setMinLease(e.target.value)}
            />
          </Field>
          <Field label="Max Lease (sec)">
            <input
              type="number"
              className={inputCls}
              value={maxLease}
              onChange={(e) => setMaxLease(e.target.value)}
            />
          </Field>
        </div>

        {!editing && (
          <div className="rounded-md border bg-muted/30 p-3">
            <div className="mb-2 flex items-baseline justify-between">
              <span className="text-sm font-medium">Initial pool</span>
              <span className="text-xs text-muted-foreground">
                Address range DHCP will hand out. Leave blank to add pools
                later from the Pools tab.
              </span>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Start IP">
                <input
                  type="text"
                  className={inputCls}
                  placeholder="10.0.0.10"
                  value={poolStart}
                  onChange={(e) => setPoolStart(e.target.value)}
                />
              </Field>
              <Field label="End IP">
                <input
                  type="text"
                  className={inputCls}
                  placeholder="10.0.0.254"
                  value={poolEnd}
                  onChange={(e) => setPoolEnd(e.target.value)}
                />
              </Field>
            </div>
          </div>
        )}

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          <span>Enabled (serve leases from this scope)</span>
        </label>

        <div className="rounded-md border p-3 space-y-2">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={ddnsEnabled}
              onChange={(e) => setDdnsEnabled(e.target.checked)}
            />
            <span>DDNS — push lease updates to DNS</span>
          </label>
          {ddnsEnabled && (
            <div className="grid grid-cols-2 gap-3 pl-6">
              <Field label="Hostname Policy">
                <select
                  className={inputCls}
                  value={ddnsPolicy}
                  onChange={(e) => setDdnsPolicy(e.target.value)}
                >
                  <option value="client">Client-supplied</option>
                  <option value="ipam">From IPAM</option>
                  <option value="generate">Generate</option>
                </select>
              </Field>
              <Field label="Domain Override" hint="Blank = use subnet DNS zone.">
                <input
                  className={inputCls}
                  value={ddnsDomain}
                  onChange={(e) => setDdnsDomain(e.target.value)}
                />
              </Field>
            </div>
          )}
        </div>

        <Field
          label="Hostname → IPAM Sync"
          hint="How learned hostnames from DHCP clients feed back into IPAM address records."
        >
          <select
            className={inputCls}
            value={hostnameSync}
            onChange={(e) => setHostnameSync(e.target.value)}
          >
            <option value="none">None</option>
            <option value="ipam">Write to IPAM on lease</option>
            <option value="learned">Store as learned hostname</option>
          </select>
        </Field>

        <div className="border-t pt-3">
          <h3 className="text-sm font-semibold mb-2">Options</h3>
          <DHCPOptionsEditor value={options} onChange={setOptions} />
        </div>

        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

export const EditScopeModal = CreateScopeModal;
