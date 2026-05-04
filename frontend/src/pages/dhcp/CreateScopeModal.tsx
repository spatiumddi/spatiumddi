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
  defaultGroupId,
  onClose,
}: {
  scope?: DHCPScope;
  /** When creating from a subnet, pin the subnet; otherwise show a picker. */
  subnetId?: string;
  /** When opened from within a specific group's view, pin the group so
   * the user isn't re-picking it from the list. */
  defaultGroupId?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!scope;
  const [subnetId, setSubnetId] = useState<string>(
    scope?.subnet_id ?? fixedSubnetId ?? "",
  );
  const [groupId, setGroupId] = useState<string>(
    scope?.group_id ?? defaultGroupId ?? "",
  );
  const [name, setName] = useState(scope?.name ?? "");
  const [description, setDescription] = useState(scope?.description ?? "");
  const [enabled, setEnabled] = useState(scope?.enabled ?? true);
  const [leaseTime, setLeaseTime] = useState(
    String(scope?.lease_time ?? 86400),
  );
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
  const [pxeProfileId, setPxeProfileId] = useState<string>(
    scope?.pxe_profile_id ?? "",
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
  const { data: dhcpGroups = [] } = useQuery({
    queryKey: ["dhcp-groups"],
    queryFn: () => dhcpApi.listGroups(),
  });
  // Effective DHCP group for this subnet, resolved up the IPAM hierarchy
  // (subnet → block ancestry → space). If set, we default the scope's
  // group to it, so scopes land on whatever DHCP was configured at the
  // IPAM level.
  const { data: effectiveDhcp } = useQuery({
    queryKey: ["subnet-effective-dhcp", subnetId],
    queryFn: () => ipamApi.getEffectiveSubnetDhcp(subnetId),
    enabled: !editing && !defaultGroupId && !!subnetId,
  });
  const effectiveGroupId = effectiveDhcp?.dhcp_server_group_id ?? null;
  const effectiveGroup = dhcpGroups.find((g) => g.id === effectiveGroupId);
  const inheritSource = effectiveDhcp?.inherited_from_block_id
    ? "a parent block"
    : effectiveDhcp?.inherited_from_space
      ? "the space"
      : "this subnet";

  // Auto-select the inherited group on first load, unless the user has
  // already picked one explicitly.
  const [groupAutoPicked, setGroupAutoPicked] = useState(!!groupId);
  useEffect(() => {
    if (editing || defaultGroupId) return;
    if (groupAutoPicked) return;
    if (!effectiveGroupId) return;
    setGroupId(effectiveGroupId);
    setGroupAutoPicked(true);
  }, [editing, defaultGroupId, groupAutoPicked, effectiveGroupId]);

  // When the parent already chose the subnet (the "+ New Scope on subnet"
  // dropdown), the modal hides the subnet picker but we still need the
  // network label + gateway for the pinned display and for prefill.
  const { data: pinnedSubnet } = useQuery({
    queryKey: ["subnet", fixedSubnetId ?? ""],
    queryFn: () => ipamApi.getSubnet(fixedSubnetId!),
    enabled: !!fixedSubnetId,
  });
  const pinnedGroup = dhcpGroups.find((g) => g.id === defaultGroupId);
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
      const parsedLeaseTime = parseInt(leaseTime, 10) || 86400;
      const parsedMinLease = minLease ? parseInt(minLease, 10) : null;
      const parsedMaxLease = maxLease ? parseInt(maxLease, 10) : null;

      if (parsedMinLease !== null && parsedMinLease > parsedLeaseTime) {
        throw new Error(
          "Minimum lease time must be less than or equal to lease time.",
        );
      }
      if (parsedMaxLease !== null && parsedLeaseTime > parsedMaxLease) {
        throw new Error(
          "Lease time must be less than or equal to maximum lease time.",
        );
      }
      if (
        parsedMinLease !== null &&
        parsedMaxLease !== null &&
        parsedMinLease > parsedMaxLease
      ) {
        throw new Error(
          "Minimum lease time must be less than or equal to maximum lease time.",
        );
      }

      const data: Partial<DHCPScope> & {
        group_id?: string;
        clear_pxe_profile?: boolean;
      } = {
        group_id: groupId || undefined,
        name,
        description,
        enabled,
        lease_time: parsedLeaseTime,
        min_lease_time: parsedMinLease,
        max_lease_time: parsedMaxLease,
        ddns_enabled: ddnsEnabled,
        ddns_hostname_policy: ddnsEnabled ? ddnsPolicy : null,
        ddns_domain_override: ddnsDomain || null,
        hostname_sync_mode: hostnameSync,
        options,
      };
      // PXE binding (issue #51). The backend distinguishes "no
      // change" from "explicit detach" via ``clear_pxe_profile``;
      // pass it true when the operator picked "(none)" on an
      // existing scope that previously had a profile bound.
      if (pxeProfileId) {
        data.pxe_profile_id = pxeProfileId;
      } else if (editing && scope?.pxe_profile_id) {
        data.clear_pxe_profile = true;
      }
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
      // Invalidate every shape of the scope query so the DHCP page
      // picks up the new row without a hard reload:
      //   * ``dhcp-scopes-subnet`` — IPAM's subnet-panel list
      //   * ``dhcp-scopes-group`` — DHCPPage's per-server lookup
      //     (it reads via the server's group now, not the server id)
      //   * ``dhcp-pools`` — per-scope pool query keys (broad prefix
      //     invalidation so the seeded initial pool shows up too)
      qc.invalidateQueries({ queryKey: ["dhcp-scopes"] });
      qc.invalidateQueries({ queryKey: ["dhcp-scopes-subnet", subnetId] });
      qc.invalidateQueries({ queryKey: ["dhcp-scopes-group"] });
      qc.invalidateQueries({ queryKey: ["dhcp-pools"] });
      if (editing && scope?.subnet_id && scope.subnet_id !== subnetId) {
        qc.invalidateQueries({
          queryKey: ["dhcp-scopes-subnet", scope.subnet_id],
        });
      }
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
        {!editing && (
          <p className="rounded border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
            A DHCP scope binds an <strong>IPAM subnet</strong> to a{" "}
            <strong>DHCP server</strong> so that server hands out leases from
            the subnet&apos;s address range. The subnet must exist in IPAM first
            — create it under IPAM → Subnets if it doesn&apos;t.
          </p>
        )}

        {/* Subnet — pin as a read-only pill when passed from the parent
            (the "+ New Scope on subnet" dropdown picked it already), else
            show a picker. */}
        {!editing &&
          (fixedSubnetId ? (
            <Field label="Subnet (IPAM)">
              <div className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-1.5 text-sm">
                <span className="font-mono">
                  {pinnedSubnet?.network ?? "…"}
                </span>
                {pinnedSubnet?.name && (
                  <span className="text-muted-foreground">
                    — {pinnedSubnet.name}
                  </span>
                )}
              </div>
            </Field>
          ) : (
            <Field label="Subnet (IPAM)">
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
          ))}

        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">
            <input
              className={inputCls}
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </Field>
          {/* Group — pin as a read-only pill when the parent already
              picked one (e.g. opened from inside a group's Scopes tab),
              else show a picker. Defaults to the DHCP group inherited
              from the subnet / block / space. Scopes belong to groups,
              and every server in the group serves this scope. */}
          {defaultGroupId && pinnedGroup ? (
            <Field label="DHCP Server Group">
              <div className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-1.5 text-sm">
                <span className="font-medium">{pinnedGroup.name}</span>
                <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                  {pinnedGroup.mode}
                </span>
              </div>
            </Field>
          ) : (
            <Field
              label="DHCP Server Group"
              hint={
                effectiveGroupId && effectiveGroup
                  ? `Inherited group "${effectiveGroup.name}" from ${inheritSource}.`
                  : effectiveGroupId === null && subnetId && !editing
                    ? "No DHCP group set on this subnet's space/block. Edit the subnet to set one, or pick any group below."
                    : undefined
              }
            >
              <select
                className={inputCls}
                value={groupId}
                onChange={(e) => {
                  setGroupId(e.target.value);
                  setGroupAutoPicked(true);
                }}
                required
              >
                <option value="">— Pick a group —</option>
                {dhcpGroups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name} ({g.mode})
                  </option>
                ))}
              </select>
            </Field>
          )}
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
              min="0"
              step="1"
              className={inputCls}
              value={leaseTime}
              onChange={(e) => setLeaseTime(e.target.value)}
            />
          </Field>
          <Field label="Min Lease (sec)">
            <input
              type="number"
              min="0"
              step="1"
              className={inputCls}
              value={minLease}
              onChange={(e) => setMinLease(e.target.value)}
            />
          </Field>
          <Field label="Max Lease (sec)">
            <input
              type="number"
              min="0"
              step="1"
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
                Address range DHCP will hand out. Leave blank to add pools later
                from the Pools tab.
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
              <Field
                label="Domain Override"
                hint="Blank = use subnet DNS zone."
              >
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
          <div className="mb-2 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">Options</h3>
            <ApplyTemplateControl
              groupId={groupId}
              currentOptions={options}
              onApply={setOptions}
            />
          </div>
          <DHCPOptionsEditor value={options} onChange={setOptions} />
        </div>

        <PXEProfileSection
          groupId={groupId}
          value={pxeProfileId}
          onChange={setPxeProfileId}
        />

        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

export const EditScopeModal = CreateScopeModal;

/**
 * PXE / iPXE profile picker on the scope edit modal (issue #51).
 *
 * Renders a single dropdown of profiles in the scope's group plus a
 * read-only summary of the selected profile's next-server + first
 * few arch-matches. Profile CRUD lives at ``/dhcp/groups/:gid/pxe`` —
 * the picker doesn't open a nested editor (keeps this modal focused
 * on the scope).
 */
function PXEProfileSection({
  groupId,
  value,
  onChange,
}: {
  groupId: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const { data: profiles = [] } = useQuery({
    queryKey: ["dhcp-pxe-profiles", groupId],
    queryFn: () =>
      groupId ? dhcpApi.listPxeProfiles(groupId) : Promise.resolve([]),
    enabled: !!groupId,
  });
  const selected = profiles.find((p) => p.id === value);

  if (!groupId) return null;

  return (
    <div className="space-y-2 border-t pt-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">PXE / iPXE provisioning</h3>
        {profiles.length > 0 && (
          <span className="text-[11px] text-muted-foreground">
            {profiles.length} profile{profiles.length === 1 ? "" : "s"} in group
          </span>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">
        Bind a PXE profile to render Kea client-classes for BIOS / UEFI / iPXE
        boot. Manage profiles at{" "}
        <a
          href={`/dhcp/groups/${groupId}/pxe`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary hover:underline"
        >
          DHCP → PXE Profiles
        </a>
        .
      </p>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={inputCls}
      >
        <option value="">— none (no PXE) —</option>
        {profiles.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
            {!p.enabled ? " (disabled)" : ""} — {p.matches.length} arch
            {p.matches.length === 1 ? "" : "es"}
          </option>
        ))}
      </select>
      {selected && (
        <div className="rounded border bg-muted/20 p-2 text-[11px]">
          <p>
            <strong>next-server:</strong>{" "}
            <code className="font-mono">{selected.next_server}</code>
          </p>
          <p className="mt-0.5">
            <strong>matches</strong> (priority order):
          </p>
          <ul className="ml-3 mt-0.5 space-y-0.5 font-mono text-[11px]">
            {selected.matches.slice(0, 6).map((m) => (
              <li key={m.id}>
                #{m.priority}{" "}
                {m.vendor_class_match ? `[${m.vendor_class_match}]` : "[any]"}
                {m.arch_codes && m.arch_codes.length > 0
                  ? ` arch=${m.arch_codes.join(",")}`
                  : ""}{" "}
                → {m.boot_filename}
              </li>
            ))}
            {selected.matches.length > 6 && (
              <li className="text-muted-foreground">
                … and {selected.matches.length - 6} more
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * "Apply template…" dropdown above the options editor. Client-side merge
 * into the local options state — operator still has to hit Save to persist.
 * On conflict, template wins (most natural — operator just picked it). The
 * conflict-key list is shown in a small caption so the operator knows what
 * was overwritten.
 */
function ApplyTemplateControl({
  groupId,
  currentOptions,
  onApply,
}: {
  groupId: string;
  currentOptions: DHCPOption[];
  onApply: (next: DHCPOption[]) => void;
}) {
  const { data: templates = [] } = useQuery({
    queryKey: ["dhcp-option-templates", groupId],
    queryFn: () =>
      groupId ? dhcpApi.listOptionTemplates(groupId) : Promise.resolve([]),
    enabled: !!groupId,
  });
  const [overwritten, setOverwritten] = useState<string[]>([]);
  const [pickerKey, setPickerKey] = useState(0);

  if (!groupId || templates.length === 0) return null;

  function handleSelect(templateId: string) {
    if (!templateId) return;
    const tpl = templates.find((t) => t.id === templateId);
    if (!tpl) return;
    const tplOptions = tpl.options ?? {};
    // Build a name->existing-DHCPOption map for the current value so we can
    // diff and report which keys we're about to clobber.
    const byName = new Map<string, DHCPOption>();
    for (const o of currentOptions) {
      const n = o.name || `option-${o.code}`;
      byName.set(n, o);
    }
    const conflicts: string[] = [];
    for (const [n, v] of Object.entries(tplOptions)) {
      const existing = byName.get(n);
      if (existing) {
        const a = JSON.stringify(existing.value);
        const b = JSON.stringify(v);
        if (a !== b) conflicts.push(n);
        byName.set(n, { ...existing, name: n, value: v });
      } else {
        byName.set(n, { code: 0, name: n, value: v });
      }
    }
    onApply(Array.from(byName.values()));
    setOverwritten(conflicts);
    // Reset the select so picking the same template again still fires.
    setPickerKey((k) => k + 1);
  }

  return (
    <div className="flex items-center gap-2">
      {overwritten.length > 0 && (
        <span className="text-[11px] text-amber-600 dark:text-amber-400">
          Overwrote: {overwritten.join(", ")}
        </span>
      )}
      <select
        key={pickerKey}
        defaultValue=""
        onChange={(e) => handleSelect(e.target.value)}
        className="rounded-md border bg-background px-2 py-1 text-xs hover:bg-accent"
      >
        <option value="">Apply template…</option>
        {templates.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
            {t.address_family === "ipv6" ? " (v6)" : ""}
          </option>
        ))}
      </select>
    </div>
  );
}
