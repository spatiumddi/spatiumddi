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
  const [error, setError] = useState("");

  const isHA = mode === "hot-standby" || mode === "load-balancing";

  const mut = useMutation({
    mutationFn: () => {
      const data = {
        name,
        description,
        mode: mode as "standalone" | "hot-standby" | "load-balancing",
        heartbeat_delay_ms: parseInt(heartbeat, 10) || 10000,
        max_response_delay_ms: parseInt(maxResponse, 10) || 60000,
        max_ack_delay_ms: parseInt(maxAck, 10) || 10000,
        max_unacked_clients: parseInt(maxUnacked, 10) || 5,
        auto_failover: autoFailover,
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
