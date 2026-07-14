import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { dhcpApi, type DHCPServerGroup } from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";

export function CreateServerGroupModal({
  group,
  onClose,
}: {
  group?: DHCPServerGroup;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!group;
  const [name, setName] = useState(group?.name ?? "");
  const [description, setDescription] = useState(group?.description ?? "");
  const [mode, setMode] = useState(group?.mode ?? "standalone");
  const [heartbeat, setHeartbeat] = useState(
    String(group?.heartbeat_delay_ms ?? 10000),
  );
  const [maxResponse, setMaxResponse] = useState(
    String(group?.max_response_delay_ms ?? 60000),
  );
  const [maxAck, setMaxAck] = useState(
    String(group?.max_ack_delay_ms ?? 10000),
  );
  const [maxUnacked, setMaxUnacked] = useState(
    String(group?.max_unacked_clients ?? 5),
  );
  const [autoFailover, setAutoFailover] = useState(
    group?.auto_failover ?? true,
  );
  const [socketMode, setSocketMode] = useState<"direct" | "relay">(
    group?.dhcp_socket_mode ?? "direct",
  );
  // #637 — Kea lease cache. Empty string in the max-age box means "uncapped",
  // which the API models as null.
  const [leaseCacheThreshold, setLeaseCacheThreshold] = useState(
    String(group?.lease_cache_threshold ?? 0),
  );
  const [leaseCacheMaxAge, setLeaseCacheMaxAge] = useState(
    group?.lease_cache_max_age != null ? String(group.lease_cache_max_age) : "",
  );
  const [error, setError] = useState("");

  const isHA = mode === "hot-standby" || mode === "load-balancing";

  const mut = useMutation({
    mutationFn: () => {
      const data = {
        name,
        description,
        mode: mode as "standalone" | "hot-standby" | "load-balancing",
        dhcp_socket_mode: socketMode,
        heartbeat_delay_ms: parseInt(heartbeat, 10) || 10000,
        max_response_delay_ms: parseInt(maxResponse, 10) || 60000,
        max_ack_delay_ms: parseInt(maxAck, 10) || 10000,
        max_unacked_clients: parseInt(maxUnacked, 10) || 5,
        auto_failover: autoFailover,
        lease_cache_threshold: parseFloat(leaseCacheThreshold) || 0,
        lease_cache_max_age: leaseCacheMaxAge
          ? parseInt(leaseCacheMaxAge, 10)
          : null,
      };
      return editing
        ? dhcpApi.updateGroup(group!.id, data)
        : dhcpApi.createGroup(data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-groups"] });
      onClose();
    },
    onError: (e) => setError(errMsg(e, "Failed to save group")),
  });

  return (
    <Modal
      title={editing ? "Edit Server Group" : "New DHCP Server Group"}
      onClose={onClose}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mut.mutate();
        }}
        className="space-y-3"
      >
        <Field label="Name">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        <Field
          label="Mode"
          hint="How servers in this group coordinate. HA modes render the Kea libdhcp_ha.so hook when the group has 2 Kea members."
        >
          <select
            className={inputCls}
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            <option value="standalone">Standalone</option>
            <option value="hot-standby">
              Hot Standby (one active, one passive)
            </option>
            <option value="load-balancing">Load Balancing (both active)</option>
          </select>
        </Field>

        <Field
          label="Client reachability"
          hint="How Kea receives client traffic. Directly attached uses raw sockets, so Kea hears broadcast DISCOVERs from clients on the same LAN (and also serves relayed clients) — the right choice for an all-in-one / on-LAN server. Relay-only uses UDP sockets; pick it only when every client reaches Kea through a DHCP relay, or the host can't grant raw-socket capability."
        >
          <select
            className={inputCls}
            value={socketMode}
            onChange={(e) =>
              setSocketMode(e.target.value as "direct" | "relay")
            }
          >
            <option value="direct">
              Directly attached / mixed (raw sockets) — recommended
            </option>
            <option value="relay">Relay-only (UDP sockets)</option>
          </select>
        </Field>

        <div className="rounded-md border bg-muted/20 p-3 space-y-3">
          <p className="text-xs font-semibold text-muted-foreground">
            Lease cache
          </p>
          <p className="text-xs text-muted-foreground">
            Kea 3.0 can hand a returning client its existing lease without
            writing to the lease database. That cuts disk churn, but SpatiumDDI
            derives lease events from those writes — so a non-zero threshold
            means fewer DDNS updates and staler IPAM “last seen” timestamps for
            chatty clients. Leave it at 0 unless you need the write reduction.
            Individual scopes can override this.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field
              label="Cache threshold"
              hint="Fraction of the lease lifetime (0–1). 0 disables caching — every renewal writes through, matching pre-3.0 behaviour. 0.25 is Kea's own default: a client renewing with more than 75% of its lease left is handed the same lease with no database write."
            >
              <input
                className={inputCls}
                type="number"
                min="0"
                max="1"
                step="0.05"
                value={leaseCacheThreshold}
                onChange={(e) => setLeaseCacheThreshold(e.target.value)}
              />
            </Field>
            <Field
              label="Cache max age (sec)"
              hint="Upper bound on how long a cached lease may be reused, regardless of the threshold. Leave blank for no cap (Kea's default)."
            >
              <input
                className={inputCls}
                type="number"
                min="1"
                placeholder="uncapped"
                value={leaseCacheMaxAge}
                onChange={(e) => setLeaseCacheMaxAge(e.target.value)}
              />
            </Field>
          </div>
        </div>

        {isHA && (
          <div className="rounded-md border bg-muted/20 p-3 space-y-3">
            <p className="text-xs font-semibold text-muted-foreground">
              HA Hook Tuning
            </p>
            <p className="text-[11px] text-muted-foreground">
              Rendered into <code>libdhcp_ha.so</code> on every Kea peer in this
              group. Defaults match Kea&apos;s documented recommendations; only
              tweak if your environment genuinely needs it.
            </p>
            <div className="grid grid-cols-2 gap-3">
              <Field
                label="Heartbeat Delay (ms)"
                hint="How often peers ping each other."
              >
                <input
                  type="number"
                  min={1000}
                  className={inputCls}
                  value={heartbeat}
                  onChange={(e) => setHeartbeat(e.target.value)}
                />
              </Field>
              <Field
                label="Max Response Delay (ms)"
                hint="How long to wait for a heartbeat reply before marking comms interrupted."
              >
                <input
                  type="number"
                  min={1000}
                  className={inputCls}
                  value={maxResponse}
                  onChange={(e) => setMaxResponse(e.target.value)}
                />
              </Field>
              <Field label="Max Ack Delay (ms)">
                <input
                  type="number"
                  min={100}
                  className={inputCls}
                  value={maxAck}
                  onChange={(e) => setMaxAck(e.target.value)}
                />
              </Field>
              <Field
                label="Max Unacked Clients"
                hint="Clients with pending lease-updates before peer is considered down."
              >
                <input
                  type="number"
                  min={0}
                  className={inputCls}
                  value={maxUnacked}
                  onChange={(e) => setMaxUnacked(e.target.value)}
                />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={autoFailover}
                onChange={(e) => setAutoFailover(e.target.checked)}
              />
              <span>
                Auto-failover — let peers transition to{" "}
                <code>partner-down</code> without operator approval
              </span>
            </label>
          </div>
        )}

        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

export const EditServerGroupModal = CreateServerGroupModal;
