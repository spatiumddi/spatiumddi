import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { dhcpApi, type DHCPPool, type DHCPScope } from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";

export function CreatePoolModal({
  pool,
  scope,
  onClose,
}: {
  pool?: DHCPPool;
  scope: DHCPScope;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!pool;
  const [name, setName] = useState(pool?.name ?? "");
  const [startIp, setStartIp] = useState(pool?.start_ip ?? "");
  const [endIp, setEndIp] = useState(pool?.end_ip ?? "");
  const [poolType, setPoolType] = useState(pool?.pool_type ?? "dynamic");
  const [classRestriction, setClassRestriction] = useState(
    pool?.class_restriction ?? "",
  );
  const [leaseOverride, setLeaseOverride] = useState(
    pool?.lease_time_override != null ? String(pool.lease_time_override) : "",
  );
  const [error, setError] = useState("");
  const [existingWarning, setExistingWarning] = useState<
    { address: string; status: string; hostname: string }[] | null
  >(null);

  const { data: classes = [] } = useQuery({
    queryKey: ["dhcp-client-classes", scope.server_id],
    queryFn: () =>
      scope.server_id
        ? dhcpApi.listClientClasses(scope.server_id)
        : Promise.resolve([]),
    enabled: !!scope.server_id,
  });

  const mut = useMutation({
    mutationFn: () => {
      const data: Partial<DHCPPool> = {
        name,
        start_ip: startIp,
        end_ip: endIp,
        pool_type: poolType,
        class_restriction: classRestriction || null,
        lease_time_override: leaseOverride ? parseInt(leaseOverride, 10) : null,
      };
      return editing
        ? dhcpApi.updatePool(scope.id, pool!.id, data)
        : dhcpApi.createPool(scope.id, data);
    },
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["dhcp-pools", scope.id] });
      const existing = (result as any)?.existing_ips_in_range;
      if (existing && existing.length > 0) {
        setExistingWarning(existing);
      } else {
        onClose();
      }
    },
    onError: (e) => setError(errMsg(e, "Failed to save pool")),
  });

  return (
    <Modal title={editing ? "Edit Pool" : "New Pool"} onClose={onClose} wide>
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
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Start IP">
            <input
              className={inputCls}
              value={startIp}
              onChange={(e) => setStartIp(e.target.value)}
              required
            />
          </Field>
          <Field label="End IP">
            <input
              className={inputCls}
              value={endIp}
              onChange={(e) => setEndIp(e.target.value)}
              required
            />
          </Field>
          <Field label="Type">
            <select
              className={inputCls}
              value={poolType}
              onChange={(e) => setPoolType(e.target.value)}
            >
              <option value="dynamic">Dynamic</option>
              <option value="excluded">Excluded</option>
              <option value="reserved">Reserved</option>
            </select>
          </Field>
          <Field label="Lease Time Override (sec)">
            <input
              type="number"
              className={inputCls}
              value={leaseOverride}
              onChange={(e) => setLeaseOverride(e.target.value)}
            />
          </Field>
          <Field label="Restrict to Client Class">
            <select
              className={inputCls}
              value={classRestriction}
              onChange={(e) => setClassRestriction(e.target.value)}
            >
              <option value="">— No restriction —</option>
              {classes.map((c) => (
                <option key={c.id} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        {existingWarning && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 space-y-2">
            <p className="text-sm font-medium text-amber-700 dark:text-amber-400">
              Pool created — {existingWarning.length} existing IP
              {existingWarning.length !== 1 ? "s" : ""} in this range:
            </p>
            <ul className="text-xs space-y-0.5 max-h-32 overflow-y-auto">
              {existingWarning.map((ip) => (
                <li key={ip.address} className="font-mono">
                  {ip.address}{" "}
                  <span className="text-muted-foreground">
                    ({ip.status}){ip.hostname ? ` — ${ip.hostname}` : ""}
                  </span>
                </li>
              ))}
            </ul>
            <p className="text-xs text-muted-foreground">
              These IPs may conflict with dynamic DHCP leases. Consider marking
              them as excluded or reserved.
            </p>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground hover:bg-primary/90"
            >
              OK
            </button>
          </div>
        )}
        {!existingWarning && <Btns onClose={onClose} pending={mut.isPending} />}
      </form>
    </Modal>
  );
}

export const EditPoolModal = CreatePoolModal;
