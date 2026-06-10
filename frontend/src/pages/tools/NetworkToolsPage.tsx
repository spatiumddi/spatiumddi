import { useState } from "react";
import {
  Activity,
  Award,
  Globe,
  Loader2,
  Network,
  Plug,
  Route,
  Search,
  ShieldCheck,
  Terminal,
  Wrench,
} from "lucide-react";
import {
  type NetToolCommandResult,
  type NetToolMacVendorResult,
  type NetToolPortTestResult,
  type NetToolTlsCertResult,
  type PropagationCheckResult,
  networkToolsApi,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { HeaderButton } from "@/components/ui/header-button";

// ── tool catalog ────────────────────────────────────────────────────

type ToolId =
  | "ping"
  | "traceroute"
  | "mtr"
  | "dig"
  | "whois"
  | "port-test"
  | "tls-cert"
  | "dns-propagation"
  | "mac-vendor";

interface ToolDef {
  id: ToolId;
  label: string;
  icon: typeof Wrench;
  blurb: string;
  /** off-prem tools carry a tighter rate-limit budget on the server */
  offPrem?: boolean;
}

const TOOLS: ToolDef[] = [
  {
    id: "ping",
    label: "Ping",
    icon: Activity,
    blurb: "4 ICMP echoes — reachability + round-trip latency.",
  },
  {
    id: "traceroute",
    label: "Traceroute",
    icon: Route,
    blurb: "Per-hop path to a host (max 20 hops).",
  },
  {
    id: "mtr",
    label: "MTR",
    icon: Network,
    blurb: "Combined traceroute + ping loss report.",
  },
  {
    id: "dig",
    label: "Dig",
    icon: Search,
    blurb: "DNS query against a record type / resolver.",
  },
  {
    id: "port-test",
    label: "Port test",
    icon: Plug,
    blurb: "TCP / UDP reachability — open / closed / filtered.",
  },
  {
    id: "tls-cert",
    label: "TLS certificate",
    icon: ShieldCheck,
    blurb: "Inspect the cert a host presents — expiry, SAN, issuer.",
  },
  {
    id: "dns-propagation",
    label: "DNS propagation",
    icon: Globe,
    blurb: "Query a record across several public resolvers.",
    offPrem: true,
  },
  {
    id: "whois",
    label: "WHOIS",
    icon: Terminal,
    blurb: "Public WHOIS lookup for an IP / domain / ASN.",
    offPrem: true,
  },
  {
    id: "mac-vendor",
    label: "MAC vendor",
    icon: Award,
    blurb: "Resolve OUI vendor names for a batch of MACs.",
  },
];

const inputCls =
  "block w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus:border-ring focus:outline-none focus:ring-1 focus:ring-ring";

const labelCls = "mb-1 block text-xs font-medium text-muted-foreground";

const RECORD_TYPES = [
  "A",
  "AAAA",
  "CNAME",
  "MX",
  "TXT",
  "NS",
  "SOA",
  "PTR",
  "SRV",
  "CAA",
  "TLSA",
  "DS",
  "DNSKEY",
  "NAPTR",
  "ANY",
];

function apiError(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string };
    if (first?.msg) return first.msg;
  }
  return "Request failed";
}

// ── page ──────────────────────────────────────────────────────────────

export function NetworkToolsPage() {
  const [active, setActive] = useState<ToolId>("ping");
  const tool = TOOLS.find((t) => t.id === active)!;

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="border-b bg-card px-6 py-4">
        <div className="flex items-center gap-2">
          <Wrench className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">Network Tools</h1>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-muted-foreground">
          Stateless ping / traceroute / MTR / dig / whois / port-test / TLS-cert
          / DNS-propagation / MAC-vendor utilities run from the SpatiumDDI
          server perspective. Rate-limited per user.
        </p>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Left tool picker */}
        <div className="w-60 shrink-0 overflow-auto border-r bg-card/40 p-2">
          <RunFromSelector />
          <div className="mt-2 space-y-1">
            {TOOLS.map((t) => {
              const Icon = t.icon;
              return (
                <button
                  key={t.id}
                  onClick={() => setActive(t.id)}
                  className={cn(
                    "flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-sm",
                    active === t.id
                      ? "bg-primary/10 text-primary"
                      : "hover:bg-muted/60",
                  )}
                >
                  <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1 font-medium">
                      {t.label}
                      {t.offPrem && (
                        <span
                          title="Sends traffic off-prem — tighter rate limit"
                          className="rounded bg-amber-500/15 px-1 text-[9px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400"
                        >
                          off-prem
                        </span>
                      )}
                    </span>
                    <span className="block text-[11px] leading-tight text-muted-foreground">
                      {t.blurb}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Right form + result */}
        <div className="flex-1 overflow-auto p-6">
          <div className="mx-auto max-w-3xl">
            <ToolPanel key={active} tool={tool} />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Deferred agent-perspective work (#58 follow-up). Render a disabled
 *  selector placeholder so the surface telegraphs the future capability. */
function RunFromSelector() {
  return (
    <div className="rounded-md border border-dashed border-border bg-muted/30 p-2">
      <label className={labelCls}>Run from</label>
      <select
        disabled
        className={cn(inputCls, "cursor-not-allowed opacity-60")}
        title="Agent-perspective runs are coming soon"
      >
        <option>Server</option>
        <option>Agent… (coming soon)</option>
      </select>
      <p className="mt-1 text-[10px] leading-tight text-muted-foreground/70">
        Runs execute from the control-plane server. Per-agent perspective is
        coming soon.
      </p>
    </div>
  );
}

function ToolPanel({ tool }: { tool: ToolDef }) {
  const Icon = tool.icon;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Icon className="h-5 w-5 text-muted-foreground" />
        <h2 className="text-base font-semibold">{tool.label}</h2>
      </div>
      {tool.id === "ping" && <CommandTool kind="ping" />}
      {tool.id === "traceroute" && <CommandTool kind="traceroute" />}
      {tool.id === "mtr" && <CommandTool kind="mtr" />}
      {tool.id === "dig" && <DigTool />}
      {tool.id === "whois" && <WhoisTool />}
      {tool.id === "port-test" && <PortTestTool />}
      {tool.id === "tls-cert" && <TlsCertTool />}
      {tool.id === "dns-propagation" && <PropagationTool />}
      {tool.id === "mac-vendor" && <MacVendorTool />}
    </div>
  );
}

// ── shared result blocks ──────────────────────────────────────────────

function ErrorBanner({ msg }: { msg: string }) {
  return (
    <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
      {msg}
    </p>
  );
}

function CommandOutput({ res }: { res: NetToolCommandResult }) {
  const body = res.stdout || res.stderr || res.error || "(no output)";
  return (
    <div className="space-y-2">
      {!res.available && res.error && <ErrorBanner msg={res.error} />}
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <code className="rounded bg-muted px-1.5 py-0.5 font-mono">
          {res.argv.join(" ")}
        </code>
        {res.exit_code !== null && (
          <span
            className={cn(
              "rounded px-1.5 py-0.5 font-medium",
              res.exit_code === 0
                ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                : "bg-amber-500/15 text-amber-600 dark:text-amber-400",
            )}
          >
            exit {res.exit_code}
          </span>
        )}
        {res.timed_out && (
          <span className="rounded bg-destructive/15 px-1.5 py-0.5 font-medium text-destructive">
            timed out
          </span>
        )}
        {res.duration_ms !== null && (
          <span>{Math.round(res.duration_ms)} ms</span>
        )}
      </div>
      <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted/40 p-3 font-mono text-xs">
        {body}
      </pre>
    </div>
  );
}

// ── ping / traceroute / mtr ───────────────────────────────────────────

function CommandTool({ kind }: { kind: "ping" | "traceroute" | "mtr" }) {
  const [host, setHost] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<NetToolCommandResult | null>(null);

  const run = async () => {
    if (!host.trim()) {
      setErr("Host is required");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      const fn =
        kind === "ping"
          ? networkToolsApi.ping
          : kind === "traceroute"
            ? networkToolsApi.traceroute
            : networkToolsApi.mtr;
      setRes(await fn(host.trim()));
    } catch (e) {
      setErr(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div>
        <label className={labelCls}>Host</label>
        <input
          className={cn(inputCls, "font-mono")}
          value={host}
          onChange={(e) => setHost(e.target.value)}
          placeholder="IP or hostname — e.g. 1.1.1.1, router1.lan"
          onKeyDown={(e) => e.key === "Enter" && run()}
          autoFocus
        />
      </div>
      {err && <ErrorBanner msg={err} />}
      <RunButton busy={busy} onClick={run} label={host || "host"} />
      {res && <CommandOutput res={res} />}
    </div>
  );
}

// ── dig ───────────────────────────────────────────────────────────────

function DigTool() {
  const [name, setName] = useState("");
  const [recordType, setRecordType] = useState("A");
  const [server, setServer] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<NetToolCommandResult | null>(null);

  const run = async () => {
    if (!name.trim()) {
      setErr("Name is required");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      setRes(
        await networkToolsApi.dig({
          name: name.trim(),
          record_type: recordType,
          server: server.trim() || null,
        }),
      );
    } catch (e) {
      setErr(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <div className="sm:col-span-2">
          <label className={labelCls}>Name</label>
          <input
            className={cn(inputCls, "font-mono")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="example.com"
            onKeyDown={(e) => e.key === "Enter" && run()}
            autoFocus
          />
        </div>
        <div>
          <label className={labelCls}>Type</label>
          <select
            className={inputCls}
            value={recordType}
            onChange={(e) => setRecordType(e.target.value)}
          >
            {RECORD_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div>
        <label className={labelCls}>Resolver (optional)</label>
        <input
          className={cn(inputCls, "font-mono")}
          value={server}
          onChange={(e) => setServer(e.target.value)}
          placeholder="@server — e.g. 1.1.1.1 (defaults to system resolver)"
        />
      </div>
      {err && <ErrorBanner msg={err} />}
      <RunButton busy={busy} onClick={run} label="dig" />
      {res && <CommandOutput res={res} />}
    </div>
  );
}

// ── whois ─────────────────────────────────────────────────────────────

function WhoisTool() {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<NetToolCommandResult | null>(null);

  const run = async () => {
    if (!query.trim()) {
      setErr("Query is required");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      setRes(await networkToolsApi.whois(query.trim()));
    } catch (e) {
      setErr(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div>
        <label className={labelCls}>Query</label>
        <input
          className={cn(inputCls, "font-mono")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="IP, domain, or ASN — e.g. 1.1.1.1, example.com, AS13335"
          onKeyDown={(e) => e.key === "Enter" && run()}
          autoFocus
        />
        <p className="mt-1 text-[11px] text-muted-foreground/70">
          Sends an outbound WHOIS query to a public registry.
        </p>
      </div>
      {err && <ErrorBanner msg={err} />}
      <RunButton busy={busy} onClick={run} label="whois" />
      {res && <CommandOutput res={res} />}
    </div>
  );
}

// ── port test ─────────────────────────────────────────────────────────

function PortTestTool() {
  const [host, setHost] = useState("");
  const [port, setPort] = useState("443");
  const [protocol, setProtocol] = useState<"tcp" | "udp">("tcp");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<NetToolPortTestResult | null>(null);

  const run = async () => {
    const p = Number(port);
    if (!host.trim()) {
      setErr("Host is required");
      return;
    }
    if (!Number.isInteger(p) || p < 1 || p > 65535) {
      setErr("Port must be 1–65535");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      setRes(
        await networkToolsApi.portTest({
          host: host.trim(),
          port: p,
          protocol,
        }),
      );
    } catch (e) {
      setErr(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  const stateColor = (s: string) =>
    s === "open"
      ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
      : s === "closed"
        ? "bg-destructive/15 text-destructive"
        : "bg-amber-500/15 text-amber-600 dark:text-amber-400";

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <div className="sm:col-span-2">
          <label className={labelCls}>Host</label>
          <input
            className={cn(inputCls, "font-mono")}
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="IP or hostname"
            onKeyDown={(e) => e.key === "Enter" && run()}
            autoFocus
          />
        </div>
        <div>
          <label className={labelCls}>Port</label>
          <input
            className={cn(inputCls, "font-mono")}
            value={port}
            onChange={(e) => setPort(e.target.value)}
            placeholder="443"
            onKeyDown={(e) => e.key === "Enter" && run()}
          />
        </div>
      </div>
      <div>
        <label className={labelCls}>Protocol</label>
        <div className="flex gap-2">
          {(["tcp", "udp"] as const).map((p) => (
            <label
              key={p}
              className={cn(
                "flex cursor-pointer items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm",
                protocol === p
                  ? "border-primary bg-primary/5"
                  : "border-border hover:bg-muted/40",
              )}
            >
              <input
                type="radio"
                name="port-proto"
                checked={protocol === p}
                onChange={() => setProtocol(p)}
              />
              {p.toUpperCase()}
            </label>
          ))}
        </div>
      </div>
      {err && <ErrorBanner msg={err} />}
      <RunButton busy={busy} onClick={run} label="port" />
      {res && (
        <div className="space-y-2 rounded-md border bg-card p-3 text-sm">
          <div className="flex items-center gap-2">
            <span className="font-mono">
              {res.host}:{res.port}/{res.protocol}
            </span>
            <span
              className={cn(
                "rounded px-1.5 py-0.5 text-xs font-semibold",
                stateColor(res.state),
              )}
            >
              {res.state}
            </span>
            {res.rtt_ms !== null && (
              <span className="text-xs text-muted-foreground">
                {res.rtt_ms.toFixed(1)} ms
              </span>
            )}
          </div>
          {res.error && (
            <p className="text-xs text-muted-foreground">{res.error}</p>
          )}
        </div>
      )}
    </div>
  );
}

// ── TLS cert ──────────────────────────────────────────────────────────

function TlsCertTool() {
  const [host, setHost] = useState("");
  const [port, setPort] = useState("443");
  const [serverName, setServerName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<NetToolTlsCertResult | null>(null);

  const run = async () => {
    const p = Number(port);
    if (!host.trim()) {
      setErr("Host is required");
      return;
    }
    if (!Number.isInteger(p) || p < 1 || p > 65535) {
      setErr("Port must be 1–65535");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      setRes(
        await networkToolsApi.tlsCert({
          host: host.trim(),
          port: p,
          server_name: serverName.trim() || null,
        }),
      );
    } catch (e) {
      setErr(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <div className="sm:col-span-2">
          <label className={labelCls}>Host</label>
          <input
            className={cn(inputCls, "font-mono")}
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="example.com"
            onKeyDown={(e) => e.key === "Enter" && run()}
            autoFocus
          />
        </div>
        <div>
          <label className={labelCls}>Port</label>
          <input
            className={cn(inputCls, "font-mono")}
            value={port}
            onChange={(e) => setPort(e.target.value)}
          />
        </div>
      </div>
      <div>
        <label className={labelCls}>SNI override (optional)</label>
        <input
          className={cn(inputCls, "font-mono")}
          value={serverName}
          onChange={(e) => setServerName(e.target.value)}
          placeholder="Defaults to host"
        />
      </div>
      {err && <ErrorBanner msg={err} />}
      <RunButton busy={busy} onClick={run} label="cert" />
      {res && <TlsCertCard res={res} />}
    </div>
  );
}

function TlsCertCard({ res }: { res: NetToolTlsCertResult }) {
  if (!res.ok) {
    return <ErrorBanner msg={res.error || "TLS inspection failed"} />;
  }
  const chip = (label: string, ok: boolean) => (
    <span
      className={cn(
        "rounded px-1.5 py-0.5 text-xs font-semibold",
        ok
          ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
          : "bg-destructive/15 text-destructive",
      )}
    >
      {label}
    </span>
  );
  const days = res.days_remaining;
  return (
    <div className="space-y-2 rounded-md border bg-card p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {chip(res.expired ? "expired" : "valid", !res.expired)}
        {res.hostname_matches !== null &&
          chip(
            res.hostname_matches ? "hostname ok" : "hostname mismatch",
            res.hostname_matches,
          )}
        {res.self_signed && (
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-semibold text-amber-600 dark:text-amber-400">
            self-signed
          </span>
        )}
        {days !== null && (
          <span
            className={cn(
              "rounded px-1.5 py-0.5 text-xs font-medium",
              days < 0
                ? "bg-destructive/15 text-destructive"
                : days < 30
                  ? "bg-amber-500/15 text-amber-600 dark:text-amber-400"
                  : "bg-muted text-muted-foreground",
            )}
          >
            {days < 0 ? `expired ${-days}d ago` : `${days}d remaining`}
          </span>
        )}
      </div>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-[8rem_1fr]">
        <Field label="Subject" value={res.subject} mono />
        <Field label="Issuer" value={res.issuer} mono />
        <Field label="Not before" value={res.not_before} />
        <Field label="Not after" value={res.not_after} />
        <Field label="Serial" value={res.serial} mono />
        <Field label="Sig alg" value={res.signature_algorithm} />
        <Field
          label="SAN"
          value={res.san.length ? res.san.join(", ") : "(none)"}
          mono
        />
      </dl>
    </div>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: string | null;
  mono?: boolean;
}) {
  return (
    <>
      <dt className="font-medium text-muted-foreground">{label}</dt>
      <dd className={cn("break-all", mono && "font-mono")}>{value ?? "—"}</dd>
    </>
  );
}

// ── DNS propagation ───────────────────────────────────────────────────

function PropagationTool() {
  const [name, setName] = useState("");
  const [recordType, setRecordType] = useState("A");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<PropagationCheckResult | null>(null);

  const run = async () => {
    if (!name.trim()) {
      setErr("Name is required");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      setRes(
        await networkToolsApi.dnsPropagation({
          name: name.trim(),
          record_type: recordType,
        }),
      );
    } catch (e) {
      setErr(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  const statusColor = (s: string) =>
    s === "ok"
      ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
      : s === "nxdomain"
        ? "bg-amber-500/15 text-amber-600 dark:text-amber-400"
        : "bg-destructive/15 text-destructive";

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <div className="sm:col-span-2">
          <label className={labelCls}>Name</label>
          <input
            className={cn(inputCls, "font-mono")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="example.com"
            onKeyDown={(e) => e.key === "Enter" && run()}
            autoFocus
          />
        </div>
        <div>
          <label className={labelCls}>Type</label>
          <select
            className={inputCls}
            value={recordType}
            onChange={(e) => setRecordType(e.target.value)}
          >
            {RECORD_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
      </div>
      {err && <ErrorBanner msg={err} />}
      <RunButton busy={busy} onClick={run} label="propagation" />
      {res && (
        <div className="space-y-2">
          {res.results.map((r) => (
            <div
              key={r.resolver}
              className="flex items-start gap-2 rounded-md border bg-card p-2 text-xs"
            >
              <span
                className={cn(
                  "mt-0.5 rounded px-1.5 py-0.5 font-semibold",
                  statusColor(r.status),
                )}
              >
                {r.status}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{r.name ?? r.resolver}</span>
                  <span className="font-mono text-muted-foreground">
                    {r.resolver}
                  </span>
                  {r.rtt_ms !== null && (
                    <span className="text-muted-foreground">
                      {r.rtt_ms.toFixed(0)} ms
                    </span>
                  )}
                </div>
                {r.answers.length > 0 && (
                  <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-[11px] text-muted-foreground">
                    {r.answers.join("\n")}
                  </pre>
                )}
                {r.error && (
                  <p className="mt-1 text-muted-foreground">{r.error}</p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── MAC vendor ────────────────────────────────────────────────────────

function MacVendorTool() {
  const [raw, setRaw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<NetToolMacVendorResult | null>(null);

  const run = async () => {
    const macs = raw
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!macs.length) {
      setErr("Enter at least one MAC address");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      setRes(await networkToolsApi.macVendor(macs));
    } catch (e) {
      setErr(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div>
        <label className={labelCls}>MAC addresses</label>
        <textarea
          className={cn(inputCls, "min-h-[5rem] font-mono")}
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          placeholder="One per line or comma-separated — e.g. 00:11:22:33:44:55"
        />
      </div>
      {err && <ErrorBanner msg={err} />}
      <RunButton busy={busy} onClick={run} label="lookup" />
      {res && (
        <div className="space-y-2">
          {!res.oui_enabled && (
            <p className="rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs text-amber-700 dark:text-amber-400">
              OUI lookup is disabled. Enable it under Settings → IPAM → OUI
              vendor lookup to resolve vendor names.
            </p>
          )}
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b text-left text-muted-foreground">
                <th className="py-1 font-medium">MAC</th>
                <th className="py-1 font-medium">Vendor</th>
              </tr>
            </thead>
            <tbody>
              {res.entries.map((e, i) => (
                <tr key={`${e.mac}-${i}`} className="border-b last:border-0">
                  <td className="py-1 font-mono">{e.mac}</td>
                  <td className="py-1">
                    {e.vendor ?? (
                      <span className="text-muted-foreground">— unknown —</span>
                    )}
                    {e.is_voip_phone && (
                      <span className="ml-2 rounded bg-sky-500/15 px-1 text-[10px] font-semibold text-sky-600 dark:text-sky-400">
                        VoIP
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── run button ────────────────────────────────────────────────────────

function RunButton({
  busy,
  onClick,
  label,
}: {
  busy: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <div className="flex justify-end">
      <HeaderButton variant="primary" onClick={onClick} disabled={busy}>
        {busy ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Search className="h-3.5 w-3.5" />
        )}
        Run {label}
      </HeaderButton>
    </div>
  );
}
