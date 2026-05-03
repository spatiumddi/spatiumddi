import { useQuery } from "@tanstack/react-query";

import { vrfsApi, type VRF } from "@/lib/api";

interface VrfPickerProps {
  value: string | null;
  onChange: (vrfId: string | null) => void;
  className?: string;
  disabled?: boolean;
}

/** Single-select dropdown bound to the VRF list. NULL = "no VRF". */
export function VrfPicker({
  value,
  onChange,
  className,
  disabled,
}: VrfPickerProps) {
  const { data } = useQuery({
    queryKey: ["vrfs-picker"],
    queryFn: () => vrfsApi.list(),
    staleTime: 60_000,
  });
  const vrfs = data ?? [];

  return (
    <select
      className={className}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      disabled={disabled}
    >
      <option value="">— None —</option>
      {vrfs.map((v: VRF) => (
        <option key={v.id} value={v.id}>
          {v.name}
          {v.route_distinguisher ? ` — RD ${v.route_distinguisher}` : ""}
        </option>
      ))}
    </select>
  );
}
