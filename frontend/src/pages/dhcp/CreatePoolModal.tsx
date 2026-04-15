import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  dhcpApi,
  type DHCPPool,
  type DHCPOption,
  type DHCPScope,
} from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";
import { DHCPOptionsEditor } from "./DHCPOptionsEditor";

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
  const [clientClassId, setClientClassId] = useState(
    pool?.client_class_id ?? "",
  );
  const [leaseOverride, setLeaseOverride] = useState(
    pool?.lease_time_override != null ? String(pool.lease_time_override) : "",
  );
  const [options, setOptions] = useState<DHCPOption[]>(pool?.options ?? []);
  const [error, setError] = useState("");

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
        client_class_id: clientClassId || null,
        lease_time_override: leaseOverride ? parseInt(leaseOverride, 10) : null,
        options,
      };
      return editing
        ? dhcpApi.updatePool(scope.id, pool!.id, data)
        : dhcpApi.createPool(scope.id, data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-pools", scope.id] });
      onClose();
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
              value={clientClassId}
              onChange={(e) => setClientClassId(e.target.value)}
            >
              <option value="">— No restriction —</option>
              {classes.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="border-t pt-3">
          <h3 className="text-sm font-semibold mb-2">Options Override</h3>
          <DHCPOptionsEditor value={options} onChange={setOptions} />
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

export const EditPoolModal = CreatePoolModal;
