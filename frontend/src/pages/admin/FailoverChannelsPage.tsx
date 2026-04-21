import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import {
  dhcpApi,
  type DHCPFailoverChannel,
  type DHCPFailoverChannelCreate,
  type DHCPServer,
} from "@/lib/api";
import { cn, zebraBodyCls } from "@/lib/utils";
import { Modal } from "@/components/ui/modal";
import { HeaderButton } from "@/components/ui/header-button";

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

const HA_STATE_COLOURS: Record<string, string> = {
  normal:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300",
  "load-balancing":
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300",
  "hot-standby":
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300",
  ready:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300",
  syncing:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
  waiting:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
  "communications-interrupted":
    "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
  "partner-down":
    "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
  terminated: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
};

function HAStateBadge({ state }: { state: string | null | undefined }) {
  if (!state) {
    return (
      <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
        unknown
      </span>
    );
  }
  return (
    <span
      className={cn(
        "rounded px-2 py-0.5 text-xs font-medium",
        HA_STATE_COLOURS[state] ?? "bg-muted text-muted-foreground",
      )}
    >
      {state}
    </span>
  );
}

function Field({
  label,
  description,
  children,
}: {
  label: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
      {description && (
        <p className="text-[11px] text-muted-foreground">{description}</p>
      )}
    </div>
  );
}

function ChannelModal({
  initial,
  servers,
  onClose,
  onSave,
  isSaving,
  errorMsg,
}: {
  initial: DHCPFailoverChannel | null;
  servers: DHCPServer[];
  onClose: () => void;
  onSave: (data: DHCPFailoverChannelCreate) => void;
  isSaving: boolean;
  errorMsg: string | null;
}) {
  const [form, setForm] = useState<DHCPFailoverChannelCreate>({
    name: initial?.name ?? "",
    description: initial?.description ?? "",
    mode: initial?.mode ?? "hot-standby",
    primary_server_id: initial?.primary_server_id ?? "",
    secondary_server_id: initial?.secondary_server_id ?? "",
    primary_peer_url: initial?.primary_peer_url ?? "",
    secondary_peer_url: initial?.secondary_peer_url ?? "",
    heartbeat_delay_ms: initial?.heartbeat_delay_ms ?? 10000,
    max_response_delay_ms: initial?.max_response_delay_ms ?? 60000,
    max_ack_delay_ms: initial?.max_ack_delay_ms ?? 10000,
    max_unacked_clients: initial?.max_unacked_clients ?? 5,
    auto_failover: initial?.auto_failover ?? true,
  });

  function set<K extends keyof DHCPFailoverChannelCreate>(
    k: K,
    v: DHCPFailoverChannelCreate[K],
  ) {
    setForm((prev) => ({ ...prev, [k]: v }));
  }

  // Kea HA is a Kea-specific hook — only Kea servers can pair.
  const keaServers = servers.filter((s) => s.driver === "kea");

  const canSave =
    form.name.trim() !== "" &&
    form.primary_server_id !== "" &&
    form.secondary_server_id !== "" &&
    form.primary_server_id !== form.secondary_server_id;

  return (
    <Modal
      title={initial ? "Edit Failover Channel" : "New Failover Channel"}
      onClose={onClose}
      wide
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">
            <input
              className={inputCls}
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="dc-east-pair"
            />
          </Field>
          <Field
            label="Mode"
            description={
              form.mode === "hot-standby"
                ? "One active peer + one passive standby. Failover is explicit when the primary drops."
                : "Both peers active; traffic splits by client-identifier hash."
            }
          >
            <select
              className={inputCls}
              value={form.mode}
              onChange={(e) =>
                set("mode", e.target.value as DHCPFailoverChannelCreate["mode"])
              }
            >
              <option value="hot-standby">hot-standby</option>
              <option value="load-balancing">load-balancing</option>
            </select>
          </Field>
        </div>

        <Field label="Description">
          <input
            className={inputCls}
            value={form.description ?? ""}
            onChange={(e) => set("description", e.target.value)}
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field
            label="Primary server"
            description="The peer with role=primary in the HA hook."
          >
            <select
              className={inputCls}
              value={form.primary_server_id}
              onChange={(e) => set("primary_server_id", e.target.value)}
            >
              <option value="">Select…</option>
              {keaServers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.host})
                </option>
              ))}
            </select>
          </Field>
          <Field
            label={
              form.mode === "hot-standby"
                ? "Standby server"
                : "Secondary server"
            }
            description={
              form.mode === "hot-standby"
                ? "The passive peer — role=standby in the HA hook."
                : "The second active peer — role=secondary in the HA hook."
            }
          >
            <select
              className={inputCls}
              value={form.secondary_server_id}
              onChange={(e) => set("secondary_server_id", e.target.value)}
            >
              <option value="">Select…</option>
              {keaServers
                .filter((s) => s.id !== form.primary_server_id)
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.host})
                  </option>
                ))}
            </select>
          </Field>
        </div>

        <div className="rounded-md border border-sky-500/30 bg-sky-500/5 px-3 py-2 text-xs text-sky-900 dark:text-sky-200">
          <p className="font-medium">Peer URLs</p>
          <p className="mt-1 text-muted-foreground">
            Each URL is that peer's{" "}
            <strong className="text-foreground">own</strong>{" "}
            <code>kea-ctrl-agent</code> endpoint — the other peer calls it to
            exchange HA state. The address must be resolvable{" "}
            <strong className="text-foreground">
              from the other peer's host
            </strong>
            . On the Docker compose bridge that's the service hostname (e.g.{" "}
            <code>http://dhcp-kea:8000/</code>); on a routed LAN use the IP that
            both sides can reach.
          </p>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field
            label="Primary server URL"
            description="URL of the primary's own kea-ctrl-agent. The secondary uses this to reach the primary."
          >
            <input
              className={inputCls}
              placeholder="http://dhcp-kea:8000/"
              value={form.primary_peer_url ?? ""}
              onChange={(e) => set("primary_peer_url", e.target.value)}
            />
          </Field>
          <Field
            label={
              form.mode === "hot-standby"
                ? "Standby server URL"
                : "Secondary server URL"
            }
            description={
              form.mode === "hot-standby"
                ? "URL of the standby's own kea-ctrl-agent. The primary uses this to reach the standby."
                : "URL of the secondary's own kea-ctrl-agent. The primary uses this to reach the secondary."
            }
          >
            <input
              className={inputCls}
              placeholder="http://dhcp-kea-2:8000/"
              value={form.secondary_peer_url ?? ""}
              onChange={(e) => set("secondary_peer_url", e.target.value)}
            />
          </Field>
        </div>

        <details className="group rounded-md border bg-muted/20 px-3 py-2">
          <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">
            Tuning (advanced — defaults are Kea's recommendations)
          </summary>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Field label="Heartbeat delay (ms)">
              <input
                type="number"
                min={1000}
                className={inputCls}
                value={form.heartbeat_delay_ms ?? 10000}
                onChange={(e) =>
                  set("heartbeat_delay_ms", Number(e.target.value))
                }
              />
            </Field>
            <Field label="Max response delay (ms)">
              <input
                type="number"
                min={1000}
                className={inputCls}
                value={form.max_response_delay_ms ?? 60000}
                onChange={(e) =>
                  set("max_response_delay_ms", Number(e.target.value))
                }
              />
            </Field>
            <Field label="Max ack delay (ms)">
              <input
                type="number"
                min={100}
                className={inputCls}
                value={form.max_ack_delay_ms ?? 10000}
                onChange={(e) =>
                  set("max_ack_delay_ms", Number(e.target.value))
                }
              />
            </Field>
            <Field label="Max unacked clients">
              <input
                type="number"
                min={0}
                className={inputCls}
                value={form.max_unacked_clients ?? 5}
                onChange={(e) =>
                  set("max_unacked_clients", Number(e.target.value))
                }
              />
            </Field>
          </div>
        </details>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!form.auto_failover}
            onChange={(e) => set("auto_failover", e.target.checked)}
          />
          Auto-failover — let peers transition to <code>partner-down</code>{" "}
          without operator approval
        </label>

        {errorMsg && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {errorMsg}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm font-medium hover:bg-accent"
          >
            Cancel
          </button>
          <button
            disabled={!canSave || isSaving}
            onClick={() => onSave(form)}
            className="rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          >
            {isSaving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

export function FailoverChannelsPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<DHCPFailoverChannel | null>(null);
  const [creating, setCreating] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const {
    data: channels,
    isLoading,
    isFetching,
  } = useQuery({
    queryKey: ["dhcp-failover-channels"],
    queryFn: dhcpApi.listFailoverChannels,
    refetchInterval: 30_000,
  });
  const { data: servers } = useQuery({
    queryKey: ["dhcp-servers"],
    queryFn: () => dhcpApi.listServers(),
  });

  function refresh() {
    qc.invalidateQueries({ queryKey: ["dhcp-failover-channels"] });
    qc.invalidateQueries({ queryKey: ["dhcp-servers"] });
  }

  const serverMap = useMemo(() => {
    const m = new Map<string, DHCPServer>();
    for (const s of servers ?? []) m.set(s.id, s);
    return m;
  }, [servers]);

  const createMut = useMutation({
    mutationFn: (d: DHCPFailoverChannelCreate) =>
      dhcpApi.createFailoverChannel(d),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-failover-channels"] });
      setCreating(false);
      setErrorMsg(null);
    },
    onError: (e: unknown) => {
      setErrorMsg(parseApiError(e));
    },
  });

  const updateMut = useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data: Partial<DHCPFailoverChannelCreate>;
    }) => dhcpApi.updateFailoverChannel(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-failover-channels"] });
      setEditing(null);
      setErrorMsg(null);
    },
    onError: (e: unknown) => {
      setErrorMsg(parseApiError(e));
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => dhcpApi.deleteFailoverChannel(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dhcp-failover-channels"] });
    },
  });

  function parseApiError(e: unknown): string {
    if (typeof e === "object" && e !== null && "response" in e) {
      const r = (
        e as {
          response?: { data?: { detail?: string } };
        }
      ).response;
      return r?.data?.detail ?? "Request failed";
    }
    return "Request failed";
  }

  return (
    <div className="space-y-4 p-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold">DHCP Failover Channels</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            Pair two Kea DHCP servers in an HA relationship. The control plane
            renders the <code>libdhcp_ha.so</code> hook into each peer's config;
            agents report live state back via <code>ha-status-get</code>. A
            server can belong to at most one channel.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <HeaderButton
            icon={RefreshCw}
            iconClassName={cn(isFetching && "animate-spin")}
            onClick={refresh}
            title="Refresh channels + peer HA state"
          >
            Refresh
          </HeaderButton>
          <HeaderButton
            variant="primary"
            icon={Plus}
            onClick={() => {
              setEditing(null);
              setCreating(true);
              setErrorMsg(null);
            }}
          >
            New Channel
          </HeaderButton>
        </div>
      </div>

      <div className="rounded-lg border bg-card">
        <table className="w-full text-sm">
          <thead className="border-b bg-muted/40 text-xs uppercase tracking-wider text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left">Name</th>
              <th className="px-3 py-2 text-left">Mode</th>
              <th className="px-3 py-2 text-left">Primary</th>
              <th className="px-3 py-2 text-left">Secondary/Standby</th>
              <th className="px-3 py-2 text-left">Primary state</th>
              <th className="px-3 py-2 text-left">Secondary state</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody className={zebraBodyCls}>
            {isLoading && (
              <tr>
                <td
                  colSpan={7}
                  className="px-3 py-6 text-center text-xs text-muted-foreground"
                >
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && (channels ?? []).length === 0 && (
              <tr>
                <td
                  colSpan={7}
                  className="px-3 py-6 text-center text-xs text-muted-foreground"
                >
                  No failover channels yet — click <em>New Channel</em> to pair
                  two Kea servers.
                </td>
              </tr>
            )}
            {(channels ?? []).map((c) => {
              const primary = serverMap.get(c.primary_server_id);
              const secondary = serverMap.get(c.secondary_server_id);
              return (
                <tr key={c.id} className="border-b last:border-0">
                  <td className="px-3 py-2 font-medium">
                    {c.name}
                    {c.description && (
                      <div className="text-[11px] text-muted-foreground">
                        {c.description}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <code className="text-xs">{c.mode}</code>
                  </td>
                  <td className="px-3 py-2">
                    {primary?.name ?? c.primary_server_name}
                    <div className="text-[11px] text-muted-foreground">
                      {c.primary_peer_url || "no peer URL"}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    {secondary?.name ?? c.secondary_server_name}
                    <div className="text-[11px] text-muted-foreground">
                      {c.secondary_peer_url || "no peer URL"}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <HAStateBadge state={c.primary_ha_state} />
                  </td>
                  <td className="px-3 py-2">
                    <HAStateBadge state={c.secondary_ha_state} />
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center justify-end gap-2">
                      <button
                        onClick={() => {
                          setEditing(c);
                          setCreating(false);
                          setErrorMsg(null);
                        }}
                        className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                        title="Edit"
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => {
                          if (
                            confirm(
                              `Delete failover channel "${c.name}"? The HA hook will be removed from both peers on the next config push.`,
                            )
                          ) {
                            deleteMut.mutate(c.id);
                          }
                        }}
                        className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                        title="Delete"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {(creating || editing) && (
        <ChannelModal
          initial={editing}
          servers={servers ?? []}
          onClose={() => {
            setCreating(false);
            setEditing(null);
            setErrorMsg(null);
          }}
          onSave={(data) => {
            if (editing) {
              updateMut.mutate({ id: editing.id, data });
            } else {
              createMut.mutate(data);
            }
          }}
          isSaving={createMut.isPending || updateMut.isPending}
          errorMsg={errorMsg}
        />
      )}
    </div>
  );
}
