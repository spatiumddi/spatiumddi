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
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: () => {
      const data = { name, description, mode };
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
    <Modal title={editing ? "Edit Server Group" : "New DHCP Server Group"} onClose={onClose}>
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
        <Field label="Mode" hint="How servers in this group coordinate.">
          <select
            className={inputCls}
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            <option value="standalone">Standalone</option>
            <option value="load-balancing">Load Balancing</option>
            <option value="hot-standby">Hot Standby</option>
          </select>
        </Field>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}

export const EditServerGroupModal = CreateServerGroupModal;
