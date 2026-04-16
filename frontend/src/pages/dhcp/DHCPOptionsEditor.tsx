import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Plus, X } from "lucide-react";
import type { DHCPOption } from "@/lib/api";
import { Field, inputCls } from "./_shared";

/**
 * Editor for a set of DHCP options (standard + custom). Used in scope, pool,
 * and static-assignment modals.
 *
 * The value format `DHCPOption[]` uses either `value: string` (single-value
 * options like Domain Name or Bootfile Name) or `value: string[]` (list
 * options like Routers, DNS Servers, NTP Servers).
 *
 * NTP (option 42) is a first-class labeled input — SpatiumDDI treats NTP
 * as a peer of DNS/DHCP, so it must be visible without diving into a
 * custom-options accordion.
 */
interface StandardDef {
  code: number;
  name: string;
  label: string;
  hint?: string;
  kind: "ip-list" | "string" | "ip";
  max?: number;
}

const STANDARD_OPTIONS: StandardDef[] = [
  {
    code: 3,
    name: "routers",
    label: "Routers (option 3)",
    kind: "ip-list",
    hint: "Default gateway(s) for clients on this scope.",
  },
  {
    code: 6,
    name: "domain-name-servers",
    label: "DNS Servers (option 6)",
    kind: "ip-list",
    max: 3,
    hint: "Up to 3 DNS resolver IPs.",
  },
  {
    code: 15,
    name: "domain-name",
    label: "Domain Name (option 15)",
    kind: "string",
  },
  {
    code: 42,
    name: "ntp-servers",
    label: "NTP Servers (option 42)",
    kind: "ip-list",
    hint: "Time-sync servers pushed to clients. Required for Kerberos, TLS certs, and audit accuracy.",
  },
  {
    code: 66,
    name: "tftp-server-name",
    label: "TFTP Server Name (option 66)",
    kind: "string",
    hint: "Hostname or IP string for PXE/firmware download.",
  },
  {
    code: 67,
    name: "bootfile-name",
    label: "Bootfile Name (option 67)",
    kind: "string",
  },
  {
    code: 119,
    name: "domain-search",
    label: "Domain Search (option 119)",
    kind: "ip-list",
    hint: "Search-domain suffixes (comma-separated).",
  },
  {
    code: 150,
    name: "tftp-server-address",
    label: "TFTP Server Address (option 150)",
    kind: "ip-list",
    hint: "List of TFTP server IPs (Cisco phones, etc).",
  },
];

function splitList(s: string): string[] {
  return s
    .split(/[,\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function joinList(xs: string[] | string | undefined): string {
  if (!xs) return "";
  if (Array.isArray(xs)) return xs.join(", ");
  return xs;
}

export function DHCPOptionsEditor({
  value,
  onChange,
}: {
  value: DHCPOption[];
  onChange: (next: DHCPOption[]) => void;
}) {
  const byCode = useMemo(() => {
    const m = new Map<number, DHCPOption>();
    for (const o of value) m.set(o.code, o);
    return m;
  }, [value]);

  const [showCustom, setShowCustom] = useState(false);

  function setStandard(def: StandardDef, rawInput: string) {
    const clean = rawInput.trim();
    const next = value.filter((o) => o.code !== def.code);
    if (!clean) {
      onChange(next);
      return;
    }
    if (def.kind === "string") {
      next.push({ code: def.code, name: def.name, value: clean });
    } else if (def.kind === "ip" || def.kind === "ip-list") {
      const list = splitList(clean);
      if (def.max) list.splice(def.max);
      next.push({ code: def.code, name: def.name, value: list });
    }
    onChange(next);
  }

  const customOptions = value.filter(
    (o) => !STANDARD_OPTIONS.some((s) => s.code === o.code),
  );

  return (
    <div className="space-y-3">
      {STANDARD_OPTIONS.map((def) => {
        const current = byCode.get(def.code);
        const rendered = joinList(current?.value);
        return (
          <div key={def.code}>
            <Field label={def.label} hint={def.hint}>
              <input
                className={inputCls}
                placeholder={
                  def.kind === "ip-list"
                    ? "e.g. 10.0.0.1, 10.0.0.2"
                    : def.kind === "string"
                      ? ""
                      : "e.g. 10.0.0.1"
                }
                defaultValue={rendered}
                onBlur={(e) => setStandard(def, e.target.value)}
              />
            </Field>
          </div>
        );
      })}

      <div className="border-t pt-3">
        <button
          type="button"
          onClick={() => setShowCustom((v) => !v)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          {showCustom ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          Custom options{" "}
          {customOptions.length > 0 && `(${customOptions.length})`}
        </button>

        {showCustom && (
          <div className="mt-2 space-y-2">
            {customOptions.map((opt, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <input
                  type="number"
                  className="w-20 shrink-0 rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="code"
                  value={opt.code}
                  onChange={(e) => {
                    const code = parseInt(e.target.value, 10) || 0;
                    const next = [...value];
                    const fullIdx = next.findIndex(
                      (o) =>
                        o === opt ||
                        (o.code === opt.code && o.value === opt.value),
                    );
                    if (fullIdx >= 0) next[fullIdx] = { ...opt, code };
                    onChange(next);
                  }}
                />
                <input
                  className="min-w-0 flex-1 rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="value"
                  value={
                    typeof opt.value === "string"
                      ? opt.value
                      : opt.value.join(", ")
                  }
                  onChange={(e) => {
                    const next = [...value];
                    const fullIdx = next.findIndex(
                      (o) =>
                        o === opt ||
                        (o.code === opt.code && o.value === opt.value),
                    );
                    if (fullIdx >= 0)
                      next[fullIdx] = { ...opt, value: e.target.value };
                    onChange(next);
                  }}
                />
                <button
                  type="button"
                  onClick={() => onChange(value.filter((o) => o !== opt))}
                  className="rounded p-1 text-muted-foreground hover:text-destructive"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => onChange([...value, { code: 0, value: "" }])}
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            >
              <Plus className="h-3 w-3" /> Add custom option
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
