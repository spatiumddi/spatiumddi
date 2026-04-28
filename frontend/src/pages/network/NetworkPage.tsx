import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Network as NetworkIcon,
  Plus,
  RefreshCw,
} from "lucide-react";

import {
  networkApi,
  type NetworkDeviceListQuery,
  type NetworkDeviceRead,
  type NetworkDeviceType,
  type NetworkPollStatus,
} from "@/lib/api";
import { HeaderButton } from "@/components/ui/header-button";

import {
  DEVICE_TYPE_OPTIONS,
  DeviceTypePill,
  PollStatusPill,
  SnmpVersionPill,
  humanTime,
} from "./_shared";
import { DeviceFormModal } from "./DeviceFormModal";

// ── Filter chip helper ────────────────────────────────────────────────

function FilterChips<T extends string>({
  label,
  value,
  options,
  onChange,
  formatter,
}: {
  label: string;
  value: T | "all";
  options: T[];
  onChange: (v: T | "all") => void;
  formatter?: (v: T) => string;
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <button
        type="button"
        onClick={() => onChange("all")}
        className={`rounded-full px-2 py-0.5 text-[11px] ${
          value === "all"
            ? "bg-primary text-primary-foreground"
            : "border hover:bg-muted"
        }`}
      >
        All
      </button>
      {options.map((opt) => (
        <button
          key={opt}
          type="button"
          onClick={() => onChange(opt)}
          className={`rounded-full px-2 py-0.5 text-[11px] capitalize ${
            value === opt
              ? "bg-primary text-primary-foreground"
              : "border hover:bg-muted"
          }`}
        >
          {formatter ? formatter(opt) : opt}
        </button>
      ))}
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────

export function NetworkPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const [typeFilter, setTypeFilter] = useState<NetworkDeviceType | "all">(
    "all",
  );
  const [statusFilter, setStatusFilter] = useState<NetworkPollStatus | "all">(
    "all",
  );
  const [activeFilter, setActiveFilter] = useState<
    "all" | "active" | "inactive"
  >("all");
  const [showCreate, setShowCreate] = useState(false);

  const queryParams = useMemo<NetworkDeviceListQuery>(() => {
    const p: NetworkDeviceListQuery = { page: 1, page_size: 100 };
    if (typeFilter !== "all") p.device_type = typeFilter;
    if (statusFilter !== "all") p.last_poll_status = statusFilter;
    if (activeFilter === "active") p.active = true;
    else if (activeFilter === "inactive") p.active = false;
    return p;
  }, [typeFilter, statusFilter, activeFilter]);

  const { data, isFetching } = useQuery({
    queryKey: ["network-devices", queryParams],
    queryFn: () => networkApi.listDevices(queryParams),
  });
  const devices = data?.items ?? [];

  // Inline active-toggle — flips ``is_active`` without leaving the list.
  const toggleActiveMut = useMutation({
    mutationFn: (d: NetworkDeviceRead) =>
      networkApi.updateDevice(d.id, { is_active: !d.is_active }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["network-devices"] });
    },
  });

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="border-b bg-card px-6 py-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <NetworkIcon className="h-5 w-5 text-muted-foreground" />
              <h1 className="text-lg font-semibold">Network Discovery</h1>
              <span className="text-xs text-muted-foreground">
                {devices.length} device{devices.length === 1 ? "" : "s"}
              </span>
            </div>
            <p className="mt-1 max-w-3xl text-xs text-muted-foreground">
              Discovery via standard SNMP MIBs. Add routers + switches; polling
              fills ARP, FDB, and interface tables. Discovered IPs cross-
              reference into IPAM.
            </p>
          </div>
          <div className="flex flex-shrink-0 items-center gap-2">
            <HeaderButton
              icon={RefreshCw}
              iconClassName={isFetching ? "animate-spin" : ""}
              onClick={() =>
                qc.invalidateQueries({ queryKey: ["network-devices"] })
              }
            >
              Refresh
            </HeaderButton>
            <HeaderButton
              variant="primary"
              icon={Plus}
              onClick={() => setShowCreate(true)}
            >
              Add Device
            </HeaderButton>
          </div>
        </div>
        {/* Filters */}
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <FilterChips
            label="Type"
            value={typeFilter}
            options={DEVICE_TYPE_OPTIONS}
            onChange={setTypeFilter}
          />
          <FilterChips
            label="Status"
            value={statusFilter}
            options={
              [
                "pending",
                "success",
                "partial",
                "failed",
                "timeout",
              ] as NetworkPollStatus[]
            }
            onChange={setStatusFilter}
          />
          <FilterChips
            label="Active"
            value={activeFilter}
            options={["active", "inactive"]}
            onChange={(v) =>
              setActiveFilter(v as "active" | "inactive" | "all")
            }
          />
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6">
        <div className="rounded-lg border">
          {devices.length === 0 ? (
            <div className="flex flex-col items-center gap-2 p-10 text-center">
              <NetworkIcon className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">
                No network devices yet — add a router or switch to start
                discovering IPs.
              </p>
              <button
                onClick={() => setShowCreate(true)}
                className="mt-1 inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
              >
                <Plus className="h-3 w-3" /> Add Device
              </button>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1100px] text-xs">
                <thead>
                  <tr className="border-b bg-muted/30">
                    <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                      Name
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                      Hostname / IP
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                      Type
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                      Vendor
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                      SNMP
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                      Status
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                      Last poll
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                      ARP / IF / FDB
                    </th>
                    <th className="whitespace-nowrap px-3 py-2 text-center font-medium">
                      Active
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {devices.map((d) => (
                    <tr
                      key={d.id}
                      onClick={() => navigate(`/network/${d.id}`)}
                      className="cursor-pointer border-b last:border-0 hover:bg-muted/20"
                    >
                      <td className="whitespace-nowrap px-3 py-2">
                        <div className="font-medium">{d.name}</div>
                        {d.description && (
                          <div className="text-[11px] text-muted-foreground">
                            {d.description}
                          </div>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px]">
                        <div>{d.hostname}</div>
                        <div className="text-muted-foreground">
                          {d.ip_address}
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        <DeviceTypePill type={d.device_type} />
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">
                        {d.vendor ?? (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <SnmpVersionPill version={d.snmp_version} />
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-1">
                          <PollStatusPill status={d.last_poll_status} />
                          {d.last_poll_error && (
                            <AlertTriangle
                              className="h-3 w-3 text-amber-500"
                              aria-label={d.last_poll_error}
                            />
                          )}
                        </div>
                      </td>
                      <td
                        className="whitespace-nowrap px-3 py-2 text-muted-foreground"
                        title={d.last_poll_at ?? ""}
                      >
                        {humanTime(d.last_poll_at)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-muted-foreground">
                        {d.last_poll_arp_count ?? 0} /{" "}
                        {d.last_poll_interface_count ?? 0} /{" "}
                        {d.last_poll_fdb_count ?? 0}
                      </td>
                      <td
                        className="whitespace-nowrap px-3 py-2 text-center"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <label className="inline-flex cursor-pointer items-center">
                          <input
                            type="checkbox"
                            checked={d.is_active}
                            disabled={toggleActiveMut.isPending}
                            onChange={() => toggleActiveMut.mutate(d)}
                          />
                        </label>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {showCreate && <DeviceFormModal onClose={() => setShowCreate(false)} />}
    </div>
  );
}
