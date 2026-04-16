import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  dhcpApi,
  type DHCPStaticAssignment,
  type DHCPScope,
} from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";

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
  const [mac, setMac] = useState(staticAssignment?.mac_address ?? "");
  const [ip, setIp] = useState(staticAssignment?.ip_address ?? "");
  const [hostname, setHostname] = useState(staticAssignment?.hostname ?? "");
  const [description, setDescription] = useState(
    staticAssignment?.description ?? "",
  );
  const [clientId, setClientId] = useState(
    staticAssignment?.client_id ?? "",
  );
  const [error, setError] = useState("");

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
        mac_address: mac,
        ip_address: ip,
        hostname,
        description,
        client_id: clientId || null,
      };
      return editing
        ? dhcpApi.updateStatic(scope.id, staticAssignment!.id, data)
        : dhcpApi.createStatic(scope.id, data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-statics", scope.id] });
      // Static upserts an IPAM row (status=static_dhcp) — refresh IPAM views.
      qc.invalidateQueries({ queryKey: ["addresses", scope.subnet_id] });
      qc.invalidateQueries({ queryKey: ["subnet-dns-sync", scope.subnet_id] });
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
          <Field label="Client ID" hint="Optional DHCP client identifier override">
            <input
              className={inputCls}
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
            />
          </Field>
        </div>
        <Field label="Description">
          <input
            className={inputCls}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
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
