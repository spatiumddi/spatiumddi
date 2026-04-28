import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  dhcpApi,
  type DHCPOption,
  type DHCPOptionTemplate,
  type DHCPOptionTemplateWrite,
} from "@/lib/api";
import { Modal, Field, Btns, inputCls, errMsg } from "./_shared";
import { DHCPOptionsEditor } from "./DHCPOptionsEditor";

export function CreateOptionTemplateModal({
  template,
  groupId,
  onClose,
}: {
  template?: DHCPOptionTemplate;
  groupId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!template;
  const [name, setName] = useState(template?.name ?? "");
  const [description, setDescription] = useState(template?.description ?? "");
  const [addressFamily, setAddressFamily] = useState<"ipv4" | "ipv6">(
    template?.address_family ?? "ipv4",
  );
  const initialOptions: DHCPOption[] = template?.options
    ? Object.entries(template.options).map(([n, value]) => ({
        code: 0,
        name: n,
        value: value as string | string[],
      }))
    : [];
  const [options, setOptions] = useState<DHCPOption[]>(initialOptions);
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: () => {
      const optionsDict: Record<string, string | string[]> = {};
      for (const opt of options) {
        const key = opt.name || `option-${opt.code}`;
        optionsDict[key] = opt.value;
      }
      const data: DHCPOptionTemplateWrite = {
        name,
        description,
        address_family: addressFamily,
        options: optionsDict,
      };
      return editing
        ? dhcpApi.updateOptionTemplate(groupId, template!.id, data)
        : dhcpApi.createOptionTemplate(groupId, data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-option-templates", groupId] });
      onClose();
    },
    onError: (e) => setError(errMsg(e, "Failed to save option template")),
  });

  return (
    <Modal
      title={editing ? "Edit Option Template" : "New Option Template"}
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
        <Field
          label="Name"
          hint="Short name shown in the apply dropdown (e.g. 'VoIP phones', 'PXE BIOS clients')."
        >
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
        <Field label="Address family">
          <select
            className={inputCls}
            value={addressFamily}
            onChange={(e) =>
              setAddressFamily(e.target.value as "ipv4" | "ipv6")
            }
          >
            <option value="ipv4">IPv4 (Dhcp4)</option>
            <option value="ipv6">IPv6 (Dhcp6)</option>
          </select>
        </Field>
        <div className="border-t pt-3">
          <h3 className="mb-2 text-sm font-semibold">Options</h3>
          <DHCPOptionsEditor value={options} onChange={setOptions} />
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Btns onClose={onClose} pending={mut.isPending} />
      </form>
    </Modal>
  );
}
