import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { dhcpApi, type DHCPServer } from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";

export function CreateServerModal({
  server,
  defaultGroupId,
  onClose,
}: {
  server?: DHCPServer;
  defaultGroupId?: string | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!server;
  const [name, setName] = useState(server?.name ?? "");
  const [driver, setDriver] = useState(server?.driver ?? "kea");
  const [host, setHost] = useState(server?.host ?? "");
  const [port, setPort] = useState(String(server?.port ?? 67));
  const [groupId, setGroupId] = useState<string>(
    server?.server_group_id ?? defaultGroupId ?? "",
  );
  const [description, setDescription] = useState(server?.description ?? "");
  const [error, setError] = useState("");

  const { data: groups = [] } = useQuery({
    queryKey: ["dhcp-groups"],
    queryFn: dhcpApi.listGroups,
  });

  const mut = useMutation({
    mutationFn: () => {
      const data: Partial<DHCPServer> = {
        name,
        driver,
        host,
        port: parseInt(port, 10) || 67,
        server_group_id: groupId || null,
        description,
      };
      return editing
        ? dhcpApi.updateServer(server!.id, data)
        : dhcpApi.createServer(data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-servers"] });
      qc.invalidateQueries({ queryKey: ["dhcp-groups"] });
      onClose();
    },
    onError: (e) => setError(errMsg(e, "Failed to save server")),
  });

  return (
    <Modal
      title={editing ? "Edit DHCP Server" : "New DHCP Server"}
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
          <Field label="Name">
            <input
              className={inputCls}
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </Field>
          <Field label="Driver">
            <select
              className={inputCls}
              value={driver}
              onChange={(e) => setDriver(e.target.value)}
            >
              <option value="kea">Kea</option>
              <option value="isc_dhcp">ISC DHCP</option>
            </select>
          </Field>
          <Field label="Host">
            <input
              className={inputCls}
              value={host}
              onChange={(e) => setHost(e.target.value)}
              placeholder="10.0.0.10"
              required
            />
          </Field>
          <Field label="Port">
            <input
              type="number"
              className={inputCls}
              value={port}
              onChange={(e) => setPort(e.target.value)}
            />
          </Field>
          <Field label="Server Group">
            <select
              className={inputCls}
              value={groupId}
              onChange={(e) => setGroupId(e.target.value)}
            >
              <option value="">— None —</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Description">
          <textarea
            className={inputCls}
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

export const EditServerModal = CreateServerModal;
