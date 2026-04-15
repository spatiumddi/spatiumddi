import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  dhcpApi,
  type DHCPStaticAssignment,
  type DHCPOption,
  type DHCPScope,
} from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";
import { DHCPOptionsEditor } from "./DHCPOptionsEditor";

export function CreateStaticAssignmentModal({
  staticAssignment,
  scope,
  onClose,
}: {
  staticAssignment?: DHCPStaticAssignment;
  scope: DHCPScope;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!staticAssignment;
  const [mac, setMac] = useState(staticAssignment?.mac ?? "");
  const [ip, setIp] = useState(staticAssignment?.ip ?? "");
  const [hostname, setHostname] = useState(staticAssignment?.hostname ?? "");
  const [description, setDescription] = useState(
    staticAssignment?.description ?? "",
  );
  const [clientClassId, setClientClassId] = useState(
    staticAssignment?.client_class_id ?? "",
  );
  const [options, setOptions] = useState<DHCPOption[]>(
    staticAssignment?.options ?? [],
  );
  const [error, setError] = useState("");

  const { data: classes = [] } = useQuery({
    queryKey: ["dhcp-client-classes", scope.server_id],
    queryFn: () =>
      scope.server_id
        ? dhcpApi.listClientClasses(scope.server_id)
        : Promise.resolve([]),
    enabled: !!scope.server_id,
  });

  const { data: pools = [] } = useQuery({
    queryKey: ["dhcp-pools", scope.id],
    queryFn: () => dhcpApi.listPools(scope.id),
  });

  const eligiblePools = pools.filter(
    (p) => p.pool_type === "reserved" || p.pool_type === "dynamic",
  );

  const mut = useMutation({
    mutationFn: () => {
      const data: Partial<DHCPStaticAssignment> = {
        mac,
        ip,
        hostname,
        description,
        client_class_id: clientClassId || null,
        options,
      };
      return editing
        ? dhcpApi.updateStatic(scope.id, staticAssignment!.id, data)
        : dhcpApi.createStatic(scope.id, data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-statics", scope.id] });
      onClose();
    },
    onError: (e) => setError(errMsg(e, "Conflict or validation error")),
  });

  return (
    <Modal
      title={editing ? "Edit Static Assignment" : "New Static Assignment"}
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
        <div className="grid grid-cols-2 gap-3">
          <Field label="MAC Address" hint="Format: aa:bb:cc:dd:ee:ff">
            <input
              className={`${inputCls} font-mono`}
              value={mac}
              onChange={(e) => setMac(e.target.value)}
              required
            />
          </Field>
          <Field
            label="IP Address"
            hint={
              eligiblePools.length > 0
                ? `Must be within a reserved or dynamic pool of this scope.`
                : "No pools defined yet — any IP in subnet."
            }
          >
            <input
              className={`${inputCls} font-mono`}
              value={ip}
              onChange={(e) => setIp(e.target.value)}
              required
              list="dhcp-pool-hint"
            />
            <datalist id="dhcp-pool-hint">
              {eligiblePools.flatMap((p) => [
                <option key={`${p.id}-s`} value={p.start_ip}>
                  {p.name || p.pool_type} start
                </option>,
                <option key={`${p.id}-e`} value={p.end_ip}>
                  {p.name || p.pool_type} end
                </option>,
              ])}
            </datalist>
          </Field>
          <Field label="Hostname">
            <input
              className={inputCls}
              value={hostname}
              onChange={(e) => setHostname(e.target.value)}
            />
          </Field>
          <Field label="Client Class">
            <select
              className={inputCls}
              value={clientClassId}
              onChange={(e) => setClientClassId(e.target.value)}
            >
              <option value="">— None —</option>
              {classes.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
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
        <div className="border-t pt-3">
          <h3 className="text-sm font-semibold mb-2">Options Override</h3>
          <DHCPOptionsEditor value={options} onChange={setOptions} />
        </div>
        {error && (
          <p className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            {error}
          </p>
        )}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

export const EditStaticAssignmentModal = CreateStaticAssignmentModal;
