import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  Download,
  FilePlus2,
  KeyRound,
  Plus,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";

import {
  applianceTlsApi,
  type ApplianceCertificate,
  type CSRKeyType,
  type CertificateSource,
} from "@/lib/api";
import { Modal } from "@/components/ui/modal";
import { ConfirmModal } from "@/components/ui/confirm-modal";

/**
 * Phase 4b.1 + 4b.3 — Web UI Certificate management tab.
 *
 * Surfaces:
 *   - list of certs + CSR-pending rows in the DB; active row pinned
 *     to the top
 *   - "Upload" path (4b.1): paste cert + key PEM, save, optionally
 *     activate
 *   - "Generate CSR" path (4b.3): the appliance generates a private
 *     key locally (never leaves the server), builds + signs a CSR
 *     against operator-supplied subject + SANs, and surfaces the CSR
 *     PEM for the operator to copy / download. When the CA returns
 *     the signed cert, operator pastes it back via the row's "Paste
 *     signed cert" action.
 *   - Activate (non-pending rows only) / Delete (non-active rows only)
 *
 * Phase 4b.4 will add "Issue via Let's Encrypt"; 4b.5 will pre-populate
 * a self-signed default on first boot.
 */
export function CertificatesTab() {
  const qc = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [csrOpen, setCsrOpen] = useState(false);
  const [csrViewing, setCsrViewing] = useState<ApplianceCertificate | null>(null);
  const [importTarget, setImportTarget] = useState<ApplianceCertificate | null>(
    null,
  );
  const [deleteTarget, setDeleteTarget] = useState<ApplianceCertificate | null>(
    null,
  );

  const { data, isLoading, error } = useQuery({
    queryKey: ["appliance", "tls"],
    queryFn: applianceTlsApi.list,
  });

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["appliance", "tls"] });

  const activate = useMutation({
    mutationFn: applianceTlsApi.activate,
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: applianceTlsApi.remove,
    onSuccess: () => {
      invalidate();
      setDeleteTarget(null);
    },
  });

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <ShieldCheck className="h-4 w-4 text-muted-foreground" />
            Web UI Certificate
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            The certificate nginx serves on the appliance's HTTPS frontend.
            Upload a PEM cert + private key from an existing CA, or generate
            a CSR locally (key stays on the server) and bring back the
            signed cert. Phase 4b.2 wires the active row into nginx; until
            then this stores certs but nginx still uses the self-signed
            default.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            onClick={() => setCsrOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent"
          >
            <FilePlus2 className="h-3.5 w-3.5" />
            Generate CSR
          </button>
          <button
            type="button"
            onClick={() => setUploadOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Plus className="h-3.5 w-3.5" />
            Upload certificate
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          Failed to load certificates: {(error as Error).message}
        </div>
      )}

      {isLoading ? (
        <div className="py-12 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      ) : !data || data.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="space-y-3">
          {data.map((cert) => (
            <CertificateCard
              key={cert.id}
              cert={cert}
              onActivate={() => activate.mutate(cert.id)}
              onDelete={() => setDeleteTarget(cert)}
              onViewCsr={() => setCsrViewing(cert)}
              onPasteCert={() => setImportTarget(cert)}
              activating={activate.isPending && activate.variables === cert.id}
            />
          ))}
        </div>
      )}

      {uploadOpen && (
        <UploadCertificateModal
          onClose={() => setUploadOpen(false)}
          onSuccess={() => {
            setUploadOpen(false);
            invalidate();
          }}
        />
      )}

      {csrOpen && (
        <GenerateCsrModal
          onClose={() => setCsrOpen(false)}
          onSuccess={(newRow) => {
            setCsrOpen(false);
            invalidate();
            // Pop the CSR-viewer immediately so the operator can copy
            // the PEM they just generated.
            setCsrViewing(newRow);
          }}
        />
      )}

      {csrViewing && (
        <ViewCsrModal
          cert={csrViewing}
          onClose={() => setCsrViewing(null)}
          onPasteCertNow={() => {
            const target = csrViewing;
            setCsrViewing(null);
            setImportTarget(target);
          }}
        />
      )}

      {importTarget && (
        <ImportSignedCertModal
          cert={importTarget}
          onClose={() => setImportTarget(null)}
          onSuccess={() => {
            setImportTarget(null);
            invalidate();
          }}
        />
      )}

      <ConfirmModal
        open={deleteTarget !== null}
        title="Delete certificate"
        message={
          deleteTarget && (
            <span>
              Delete{" "}
              <span className="font-mono text-foreground">
                {deleteTarget.name}
              </span>
              ?
              {deleteTarget.pending ? (
                <>
                  {" "}
                  This is a CSR-pending row — the generated private key will
                  be lost. If the CA later issues a cert against this CSR you
                  won't be able to import it.
                </>
              ) : (
                <> This cannot be undone. The PEM and private key are wiped.</>
              )}
            </span>
          )
        }
        confirmLabel="Delete"
        tone="destructive"
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && remove.mutate(deleteTarget.id)}
        loading={remove.isPending}
      />
    </div>
  );
}

function EmptyState() {
  return (
    <div className="rounded-lg border border-dashed bg-muted/30 px-6 py-12 text-center">
      <ShieldCheck className="mx-auto h-8 w-8 text-muted-foreground/50" />
      <h3 className="mt-3 text-sm font-medium">No certificates yet</h3>
      <p className="mt-1 text-xs text-muted-foreground">
        Use the buttons above to upload an existing cert + key, or generate a
        CSR so a CA can sign a fresh cert for this appliance. Until you
        activate something, nginx serves the self-signed default the
        appliance generated on first boot.
      </p>
    </div>
  );
}

function CertificateCard({
  cert,
  onActivate,
  onDelete,
  onViewCsr,
  onPasteCert,
  activating,
}: {
  cert: ApplianceCertificate;
  onActivate: () => void;
  onDelete: () => void;
  onViewCsr: () => void;
  onPasteCert: () => void;
  activating: boolean;
}) {
  const expiresInDays =
    cert.valid_to !== null
      ? Math.floor(
          (new Date(cert.valid_to).getTime() - Date.now()) / 86_400_000,
        )
      : null;
  const expiryTone =
    expiresInDays === null
      ? "text-muted-foreground"
      : expiresInDays < 0
        ? "text-destructive"
        : expiresInDays < 14
          ? "text-amber-600 dark:text-amber-400"
          : expiresInDays < 30
            ? "text-amber-500"
            : "text-muted-foreground";

  const expiryLabel =
    expiresInDays === null
      ? "—"
      : expiresInDays < 0
        ? `expired ${Math.abs(expiresInDays)}d ago`
        : `expires in ${expiresInDays}d`;

  return (
    <div
      className={`rounded-lg border bg-card p-4 shadow-sm ${
        cert.is_active
          ? "ring-1 ring-primary/40"
          : cert.pending
            ? "ring-1 ring-amber-500/30 bg-amber-500/[0.03]"
            : ""
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-sm font-semibold">{cert.name}</h3>
            {cert.is_active && (
              <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-primary">
                <CheckCircle2 className="h-3 w-3" />
                Active
              </span>
            )}
            {cert.pending && (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400">
                <KeyRound className="h-3 w-3" />
                Awaiting signed cert
              </span>
            )}
            <SourceBadge source={cert.source} />
          </div>

          <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
            <Field label="Subject" value={cert.subject_cn} mono />
            <Field
              label="Issuer"
              value={cert.issuer_cn ?? "— (CSR pending)"}
              mono
            />
            <Field
              label="SANs"
              value={cert.sans.length ? cert.sans.join(", ") : "—"}
              mono
              span={2}
            />
            <Field
              label="Fingerprint"
              value={
                cert.fingerprint_sha256 ?? "— (cert not signed yet)"
              }
              mono
              span={2}
              truncate
            />
            <Field
              label="Valid from"
              value={cert.valid_from ? fmtDate(cert.valid_from) : "—"}
            />
            <Field
              label="Valid to"
              value={
                cert.valid_to ? (
                  <span className={expiryTone}>
                    {fmtDate(cert.valid_to)} · {expiryLabel}
                  </span>
                ) : (
                  "—"
                )
              }
            />
          </dl>

          {cert.notes && (
            <p className="mt-2 text-xs italic text-muted-foreground">
              {cert.notes}
            </p>
          )}
        </div>

        <div className="flex shrink-0 flex-col gap-1.5">
          {cert.pending ? (
            <>
              <button
                type="button"
                onClick={onViewCsr}
                className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs hover:bg-accent"
              >
                <Download className="h-3 w-3" />
                View CSR
              </button>
              <button
                type="button"
                onClick={onPasteCert}
                className="inline-flex items-center gap-1 rounded-md border bg-amber-500/10 px-2 py-1 text-xs font-medium text-amber-700 hover:bg-amber-500/20 dark:text-amber-400"
              >
                <Upload className="h-3 w-3" />
                Paste signed cert
              </button>
            </>
          ) : (
            !cert.is_active && (
              <button
                type="button"
                onClick={onActivate}
                disabled={
                  activating ||
                  (expiresInDays !== null && expiresInDays < 0)
                }
                className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
                title={
                  expiresInDays !== null && expiresInDays < 0
                    ? "Cannot activate an expired certificate"
                    : "Make this the cert nginx serves"
                }
              >
                <CheckCircle2 className="h-3 w-3" />
                {activating ? "Activating…" : "Activate"}
              </button>
            )
          )}
          <button
            type="button"
            onClick={onDelete}
            disabled={cert.is_active}
            className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs text-destructive hover:bg-destructive/10 disabled:cursor-not-allowed disabled:opacity-50"
            title={
              cert.is_active
                ? "Activate a different certificate first"
                : "Delete this certificate"
            }
          >
            <Trash2 className="h-3 w-3" />
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

function SourceBadge({ source }: { source: CertificateSource }) {
  const label =
    source === "letsencrypt"
      ? "Let's Encrypt"
      : source === "self-signed"
        ? "Self-signed"
        : source === "csr"
          ? "CSR-signed"
          : "Uploaded";
  return (
    <span className="inline-flex items-center rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
      {label}
    </span>
  );
}

function Field({
  label,
  value,
  mono,
  span,
  truncate,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  span?: 1 | 2;
  truncate?: boolean;
}) {
  return (
    <div className={span === 2 ? "sm:col-span-2" : ""}>
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
        {label}
      </dt>
      <dd
        className={`mt-0.5 ${mono ? "font-mono" : ""} ${
          truncate ? "truncate" : ""
        }`}
      >
        {value}
      </dd>
    </div>
  );
}

// ── Upload modal (4b.1) ─────────────────────────────────────────────

function UploadCertificateModal({
  onClose,
  onSuccess,
}: {
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [name, setName] = useState("");
  const [certPem, setCertPem] = useState("");
  const [keyPem, setKeyPem] = useState("");
  const [notes, setNotes] = useState("");
  const [activate, setActivate] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const upload = useMutation({
    mutationFn: applianceTlsApi.upload,
    onSuccess,
    onError: (err) => setError(extractError(err)),
  });

  return (
    <Modal title="Upload certificate" onClose={onClose} wide>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          upload.mutate({
            name: name.trim(),
            cert_pem: certPem,
            key_pem: keyPem,
            notes: notes.trim() || null,
            activate,
          });
        }}
        className="space-y-3"
      >
        <FieldText
          label="Name"
          required
          value={name}
          onChange={setName}
          placeholder="e.g. wildcard-prod-2026"
          hint="Operator label only — independent from the cert's subject CN."
        />
        <FieldTextarea
          label="Certificate PEM"
          required
          value={certPem}
          onChange={setCertPem}
          rows={6}
          placeholder={"-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----"}
          hint="Paste the full chain (leaf + intermediates concatenated)."
        />
        <FieldTextarea
          label="Private key PEM"
          required
          value={keyPem}
          onChange={setKeyPem}
          rows={6}
          placeholder={"-----BEGIN PRIVATE KEY-----\n…\n-----END PRIVATE KEY-----"}
          hint="Encrypted at rest with the appliance's credential key. Encrypted private keys aren't supported — decrypt before uploading."
        />
        <FieldText
          label="Notes"
          value={notes}
          onChange={setNotes}
          placeholder="optional — e.g. 'Renewal script in cron'"
        />

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={activate}
            onChange={(e) => setActivate(e.target.checked)}
            className="rounded border-input"
          />
          Activate immediately
        </label>

        {error && <ErrorBanner message={error} />}

        <FormActions
          submitLabel={
            <>
              <Upload className="h-3.5 w-3.5" />
              {upload.isPending ? "Uploading…" : "Upload"}
            </>
          }
          submitting={upload.isPending}
          onCancel={onClose}
        />
      </form>
    </Modal>
  );
}

// ── Generate CSR modal (4b.3) ───────────────────────────────────────

const KEY_TYPE_OPTIONS: { value: CSRKeyType; label: string }[] = [
  { value: "rsa-2048", label: "RSA 2048 (default)" },
  { value: "rsa-3072", label: "RSA 3072" },
  { value: "rsa-4096", label: "RSA 4096" },
  { value: "ec-p256", label: "EC P-256" },
  { value: "ec-p384", label: "EC P-384" },
];

function GenerateCsrModal({
  onClose,
  onSuccess,
}: {
  onClose: () => void;
  onSuccess: (cert: ApplianceCertificate) => void;
}) {
  const [name, setName] = useState("");
  const [commonName, setCommonName] = useState("");
  const [sans, setSans] = useState("");
  const [organization, setOrganization] = useState("");
  const [ou, setOu] = useState("");
  const [country, setCountry] = useState("");
  const [state, setState] = useState("");
  const [locality, setLocality] = useState("");
  const [email, setEmail] = useState("");
  const [keyType, setKeyType] = useState<CSRKeyType>("rsa-2048");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);

  const generate = useMutation({
    mutationFn: applianceTlsApi.generateCsr,
    onSuccess,
    onError: (err) => setError(extractError(err)),
  });

  return (
    <Modal title="Generate CSR" onClose={onClose} wide>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          const sanList = sans
            .split(/[\n,]/)
            .map((s) => s.trim())
            .filter(Boolean);
          generate.mutate({
            name: name.trim(),
            common_name: commonName.trim(),
            organization: organization.trim() || null,
            organizational_unit: ou.trim() || null,
            country: country.trim().toUpperCase() || null,
            state: state.trim() || null,
            locality: locality.trim() || null,
            email: email.trim() || null,
            sans: sanList,
            key_type: keyType,
            notes: notes.trim() || null,
          });
        }}
        className="space-y-3"
      >
        <p className="rounded-md bg-muted/50 p-2.5 text-xs text-muted-foreground">
          The appliance generates a fresh private key locally — the key
          never leaves the server. You'll receive a CSR PEM to hand to
          your CA; when the CA returns the signed cert, paste it back via
          the row's "Paste signed cert" button.
        </p>

        <FieldText
          label="Name"
          required
          value={name}
          onChange={setName}
          placeholder="e.g. wildcard-prod-2026"
        />
        <FieldText
          label="Common Name (CN)"
          required
          value={commonName}
          onChange={setCommonName}
          placeholder="appliance.example.com"
          hint="The primary domain. Most modern CAs put this in SAN too."
        />
        <FieldTextarea
          label="Subject Alternative Names"
          value={sans}
          onChange={setSans}
          rows={3}
          placeholder={"appliance.example.com\nappliance-alt.example.com\n192.168.1.10"}
          hint="One per line (or comma-separated). DNS names + IPs both accepted; the form auto-detects IP literals."
        />

        <div className="grid grid-cols-2 gap-3">
          <FieldText
            label="Organization (O)"
            value={organization}
            onChange={setOrganization}
            placeholder="Acme Corp"
          />
          <FieldText
            label="Org Unit (OU)"
            value={ou}
            onChange={setOu}
            placeholder="IT Operations"
          />
          <FieldText
            label="Country (C)"
            value={country}
            onChange={setCountry}
            placeholder="US"
            hint="2-letter ISO code"
          />
          <FieldText
            label="State / Province (ST)"
            value={state}
            onChange={setState}
            placeholder="California"
          />
          <FieldText
            label="Locality (L)"
            value={locality}
            onChange={setLocality}
            placeholder="San Francisco"
          />
          <FieldText
            label="Email"
            value={email}
            onChange={setEmail}
            placeholder="ops@example.com"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-muted-foreground">
            Key type
          </label>
          <select
            value={keyType}
            onChange={(e) => setKeyType(e.target.value as CSRKeyType)}
            className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            {KEY_TYPE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            RSA-2048 is the safe default (universally accepted). EC keys
            handshake faster and produce shorter chains — pick those if
            your CA supports them.
          </p>
        </div>

        <FieldText
          label="Notes"
          value={notes}
          onChange={setNotes}
          placeholder="optional — e.g. 'Submitted to internal CA on 2026-05-12'"
        />

        {error && <ErrorBanner message={error} />}

        <FormActions
          submitLabel={
            <>
              <FilePlus2 className="h-3.5 w-3.5" />
              {generate.isPending ? "Generating…" : "Generate CSR"}
            </>
          }
          submitting={generate.isPending}
          onCancel={onClose}
        />
      </form>
    </Modal>
  );
}

// ── View CSR modal (4b.3) ───────────────────────────────────────────

function ViewCsrModal({
  cert,
  onClose,
  onPasteCertNow,
}: {
  cert: ApplianceCertificate;
  onClose: () => void;
  onPasteCertNow: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const csr = cert.csr_pem ?? "";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(csr);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard not available — operator can still select+copy */
    }
  };

  const download = () => {
    const blob = new Blob([csr], { type: "application/pkcs10" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${cert.name}.csr`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <Modal title={`CSR · ${cert.name}`} onClose={onClose} wide>
      <div className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Submit this CSR to your CA. When they return a signed certificate,
          come back here and click "Paste signed cert" on the same row — the
          appliance pairs it with the stored private key.
        </p>
        <textarea
          readOnly
          value={csr}
          rows={10}
          className="w-full rounded-md border bg-muted/40 px-2 py-1.5 font-mono text-[11px]"
          onClick={(e) => e.currentTarget.select()}
        />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex gap-2">
            <button
              type="button"
              onClick={copy}
              className="inline-flex items-center gap-1 rounded-md border bg-background px-2.5 py-1.5 text-xs hover:bg-accent"
            >
              <Copy className="h-3 w-3" />
              {copied ? "Copied!" : "Copy"}
            </button>
            <button
              type="button"
              onClick={download}
              className="inline-flex items-center gap-1 rounded-md border bg-background px-2.5 py-1.5 text-xs hover:bg-accent"
            >
              <Download className="h-3 w-3" />
              Download {cert.name}.csr
            </button>
          </div>
          <button
            type="button"
            onClick={onPasteCertNow}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Upload className="h-3 w-3" />
            Have the signed cert? Paste it now
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Import signed cert modal (4b.3) ─────────────────────────────────

function ImportSignedCertModal({
  cert,
  onClose,
  onSuccess,
}: {
  cert: ApplianceCertificate;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [certPem, setCertPem] = useState("");
  const [activate, setActivate] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const importIt = useMutation({
    mutationFn: (body: { cert_pem: string; activate: boolean }) =>
      applianceTlsApi.importSignedCert(cert.id, body),
    onSuccess,
    onError: (err) => setError(extractError(err)),
  });

  return (
    <Modal title={`Paste signed certificate · ${cert.name}`} onClose={onClose} wide>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          importIt.mutate({ cert_pem: certPem, activate });
        }}
        className="space-y-3"
      >
        <div className="rounded-md bg-muted/50 p-2.5 text-xs text-muted-foreground">
          <p>
            Paste the certificate your CA returned for{" "}
            <span className="font-mono text-foreground">{cert.subject_cn}</span>.
            The appliance pairs it with the private key you generated for this
            row.
          </p>
          <p className="mt-2">
            If the cert's public key doesn't match the stored private key, the
            paste is rejected — the CA gave you a cert for a different CSR.
          </p>
        </div>

        <FieldTextarea
          label="Signed certificate PEM"
          required
          value={certPem}
          onChange={setCertPem}
          rows={10}
          placeholder={"-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----"}
          hint="Paste the full chain — leaf + intermediates."
        />

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={activate}
            onChange={(e) => setActivate(e.target.checked)}
            className="rounded border-input"
          />
          Activate immediately
        </label>

        {error && <ErrorBanner message={error} />}

        <FormActions
          submitLabel={
            <>
              <Upload className="h-3.5 w-3.5" />
              {importIt.isPending ? "Importing…" : "Import"}
            </>
          }
          submitting={importIt.isPending}
          onCancel={onClose}
        />
      </form>
    </Modal>
  );
}

// ── Small form primitives ───────────────────────────────────────────

function FieldText({
  label,
  required,
  value,
  onChange,
  placeholder,
  hint,
}: {
  label: string;
  required?: boolean;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  hint?: string;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-muted-foreground">
        {label}
        {required && <span className="text-destructive"> *</span>}
      </label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        placeholder={placeholder}
        className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 text-sm"
      />
      {hint && <p className="mt-0.5 text-[10px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

function FieldTextarea({
  label,
  required,
  value,
  onChange,
  rows,
  placeholder,
  hint,
}: {
  label: string;
  required?: boolean;
  value: string;
  onChange: (v: string) => void;
  rows: number;
  placeholder?: string;
  hint?: string;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-muted-foreground">
        {label}
        {required && <span className="text-destructive"> *</span>}
      </label>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        placeholder={placeholder}
        rows={rows}
        className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs"
      />
      {hint && <p className="mt-0.5 text-[10px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

function FormActions({
  submitLabel,
  submitting,
  onCancel,
}: {
  submitLabel: React.ReactNode;
  submitting: boolean;
  onCancel: () => void;
}) {
  return (
    <div className="flex justify-end gap-2 pt-2">
      <button
        type="button"
        onClick={onCancel}
        className="rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-accent"
      >
        Cancel
      </button>
      <button
        type="submit"
        disabled={submitting}
        className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {submitLabel}
      </button>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-2 text-xs text-destructive">
      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

function extractError(err: unknown): string {
  const e = err as {
    response?: { data?: { detail?: string } };
    message?: string;
  };
  return e.response?.data?.detail ?? e.message ?? "request failed";
}

function fmtDate(s: string): string {
  const d = new Date(s);
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
