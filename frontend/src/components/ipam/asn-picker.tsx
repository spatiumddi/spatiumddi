import { useQuery } from "@tanstack/react-query";

import { asnsApi } from "@/lib/api";

interface AsnPickerProps {
  value: string | null;
  onChange: (asnId: string | null) => void;
  className?: string;
  disabled?: boolean;
}

/** Single-select dropdown bound to the ASN list. NULL = "no AS recorded". */
export function AsnPicker({
  value,
  onChange,
  className,
  disabled,
}: AsnPickerProps) {
  const { data } = useQuery({
    queryKey: ["asns-picker"],
    queryFn: () => asnsApi.list({ limit: 500 }),
    staleTime: 60_000,
  });
  const asns = data?.items ?? [];

  return (
    <select
      className={className}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      disabled={disabled}
    >
      <option value="">— None —</option>
      {asns.map((a) => (
        <option key={a.id} value={a.id}>
          AS{a.number}
          {a.name ? ` — ${a.name}` : ""}
          {a.kind === "private" ? " (private)" : ""}
        </option>
      ))}
    </select>
  );
}
