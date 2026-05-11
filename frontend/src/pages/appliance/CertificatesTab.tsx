import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Plus,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";

import {
  applianceTlsApi,
  type ApplianceCertificate,
  type CertificateSource,
} from "@/lib/api";
import { Modal } from "@/components/ui/modal";
import { ConfirmModal } from "@/components/ui/confirm-modal";

/**
 * Phase 4b.1 — Web UI Certificate management tab.
 *
 * Surfaces:
 *   - list of certs in the DB, active row pinned to the top
 *   - upload form (paste cert PEM + private key PEM + name + notes)
 *   - activate button per row (the activated row becomes the one
 *     nginx will serve when Phase 4b.2 wires the deployer)
 *   - delete (disabled while a row is active — operator must
 *     activate a different cert first)
 *
 * Phase 4b.3 will add a "Generate CSR" button next to "Upload";
 * 4b.4 will add "Issue via Let's Encrypt"; 4b.5 will pre-populate
 * a self-signed default on first boot. For now everything else is
 * an inert placeholder.
 */
export function CertificatesTab() {
  const qc = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ApplianceCertificate | null>(
    null,
  );

  const { data, isLoading, error } = useQuery({
    queryKey: ["appliance", "tls"],
    queryFn: applianceTlsApi.list,
  });

  const activate = useMutation({
    mutationFn: applianceTlsApi.activate,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["appliance", "tls"] }),
  });
  const remove = useMutation({
    mutationFn: applianceTlsApi.remove,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["appliance", "tls"] });
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
            Upload a PEM cert + private key, activate it, and the appliance
            picks it up on the next reload (Phase 4b.2 wires the reload —
            until then this stores the cert but nginx still uses the
            self-signed default).
          </p>
        </div>
        <button
          type="button"
          onClick={() => setUploadOpen(true)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        >
          <Plus className="h-3.5 w-3.5" />
          Upload certificate
        </button>
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
        <EmptyState onUpload={() => setUploadOpen(true)} />
      ) : (
        <div className="space-y-3">
          {data.map((cert) => (
            <CertificateCard
              key={cert.id}
              cert={cert}
              onActivate={() => activate.mutate(cert.id)}
              onDelete={() => setDeleteTarget(cert)}
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
            qc.invalidateQueries({ queryKey: ["appliance", "tls"] });
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
              ? This cannot be undone. The PEM and private key are wiped from
              the database.
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

function EmptyState({ onUpload }: { onUpload: () => void }) {
  return (
    <div className="rounded-lg border border-dashed bg-muted/30 px-6 py-12 text-center">
      <ShieldCheck className="mx-auto h-8 w-8 text-muted-foreground/50" />
      <h3 className="mt-3 text-sm font-medium">No certificates yet</h3>
      <p className="mt-1 text-xs text-muted-foreground">
        Upload an existing cert + key, or wait for Phase 4b.4 to issue one
        via Let's Encrypt. Until then nginx serves the self-signed default
        the appliance generated on first boot.
      </p>
      <button
        type="button"
        onClick={onUpload}
        className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
      >
        <Upload className="h-3.5 w-3.5" />
        Upload your first certificate
      </button>
    </div>
  );
}

function CertificateCard({
  cert,
  onActivate,
  onDelete,
  activating,
}: {
  cert: ApplianceCertificate;
  onActivate: () => void;
  onDelete: () => void;
  activating: boolean;
}) {
  const expiresInDays = Math.floor(
    (new Date(cert.valid_to).getTime() - Date.now()) / 86_400_000,
  );
  const expiryTone =
    expiresInDays < 0
      ? "text-destructive"
      : expiresInDays < 14
        ? "text-amber-600 dark:text-amber-400"
        : expiresInDays < 30
          ? "text-amber-500"
          : "text-muted-foreground";

  const expiryLabel =
    expiresInDays < 0
      ? `expired ${Math.abs(expiresInDays)}d ago`
      : `expires in ${expiresInDays}d`;

  return (
    <div
      className={`rounded-lg border bg-card p-4 shadow-sm ${
        cert.is_active ? "ring-1 ring-primary/40" : ""
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
            <SourceBadge source={cert.source} />
          </div>

          <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
            <Field label="Subject" value={cert.subject_cn} mono />
            <Field label="Issuer" value={cert.issuer_cn} mono />
            <Field
              label="SANs"
              value={cert.sans.length ? cert.sans.join(", ") : "—"}
              mono
              span={2}
            />
            <Field
              label="Fingerprint"
              value={cert.fingerprint_sha256}
              mono
              span={2}
              truncate
            />
            <Field label="Valid from" value={fmtDate(cert.valid_from)} />
            <Field
              label="Valid to"
              value={
                <span className={expiryTone}>
                  {fmtDate(cert.valid_to)} · {expiryLabel}
                </span>
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
          {!cert.is_active && (
            <button
              type="button"
              onClick={onActivate}
              disabled={activating || expiresInDays < 0}
              className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
              title={
                expiresInDays < 0
                  ? "Cannot activate an expired certificate"
                  : "Make this the cert nginx serves"
              }
            >
              <CheckCircle2 className="h-3 w-3" />
              {activating ? "Activating…" : "Activate"}
            </button>
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
    onError: (err: unknown) => {
      // Axios-style error — message lives on .response.data.detail when
      // the backend raised an HTTPException, .message otherwise.
      const e = err as {
        response?: { data?: { detail?: string } };
        message?: string;
      };
      setError(e.response?.data?.detail ?? e.message ?? "upload failed");
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    upload.mutate({
      name: name.trim(),
      cert_pem: certPem,
      key_pem: keyPem,
      notes: notes.trim() || null,
      activate,
    });
  };

  return (
    <Modal title="Upload certificate" onClose={onClose} wide>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="block text-xs font-medium text-muted-foreground">
            Name <span className="text-destructive">*</span>
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder="e.g. wildcard-prod-2026"
            className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 text-sm"
          />
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            Operator label only — independent from the cert's subject CN.
          </p>
        </div>

        <div>
          <label className="block text-xs font-medium text-muted-foreground">
            Certificate PEM <span className="text-destructive">*</span>
          </label>
          <textarea
            value={certPem}
            onChange={(e) => setCertPem(e.target.value)}
            required
            placeholder="-----BEGIN CERTIFICATE-----&#10;…&#10;-----END CERTIFICATE-----"
            rows={6}
            className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs"
          />
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            Paste the full chain (leaf + intermediates concatenated).
          </p>
        </div>

        <div>
          <label className="block text-xs font-medium text-muted-foreground">
            Private key PEM <span className="text-destructive">*</span>
          </label>
          <textarea
            value={keyPem}
            onChange={(e) => setKeyPem(e.target.value)}
            required
            placeholder="-----BEGIN PRIVATE KEY-----&#10;…&#10;-----END PRIVATE KEY-----"
            rows={6}
            className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 font-mono text-xs"
          />
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            Encrypted at rest with the appliance's credential key.
            Encrypted keys (-----BEGIN ENCRYPTED PRIVATE KEY-----) aren't
            supported — decrypt before uploading.
          </p>
        </div>

        <div>
          <label className="block text-xs font-medium text-muted-foreground">
            Notes
          </label>
          <input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="optional — e.g. 'Renewal script in cron'"
            className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </div>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={activate}
            onChange={(e) => setActivate(e.target.checked)}
            className="rounded border-input"
          />
          Activate immediately
        </label>

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-2 text-xs text-destructive">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-accent"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={upload.isPending}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Upload className="h-3.5 w-3.5" />
            {upload.isPending ? "Uploading…" : "Upload"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function fmtDate(s: string): string {
  const d = new Date(s);
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
