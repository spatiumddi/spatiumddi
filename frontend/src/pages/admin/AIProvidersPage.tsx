import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  XCircle,
} from "lucide-react";
import {
  aiApi,
  AI_PROVIDER_KIND_AVAILABLE,
  AI_PROVIDER_KIND_LABELS,
  AI_PROVIDER_KIND_SHORT,
  type AIProvider,
  type AIProviderCreate,
  type AIProviderKind,
  type AIProviderUpdate,
  type AITestConnectionResult,
} from "@/lib/api";
import { Modal } from "@/components/ui/modal";

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

interface ProviderForm {
  name: string;
  kind: AIProviderKind;
  base_url: string;
  // null = leave existing key untouched on update; "" = explicitly clear;
  // any other value = set / replace.
  api_key: string | null;
  default_model: string;
  is_enabled: boolean;
  priority: string;
  optionsJson: string;
}

const EMPTY: ProviderForm = {
  name: "",
  kind: "openai_compat",
  base_url: "http://host.docker.internal:11434/v1",
  api_key: "",
  default_model: "",
  is_enabled: true,
  priority: "100",
  optionsJson: "{}",
};

function formFromProvider(p: AIProvider): ProviderForm {
  return {
    name: p.name,
    kind: p.kind,
    base_url: p.base_url,
    // Edit modal starts with api_key=null so an unchanged save leaves
    // the stored ciphertext alone. Operator can paste a new value
    // (replaces) or click "clear" (sets to "").
    api_key: null,
    default_model: p.default_model,
    is_enabled: p.is_enabled,
    priority: String(p.priority),
    optionsJson: JSON.stringify(p.options ?? {}, null, 2),
  };
}

function toCreatePayload(form: ProviderForm): AIProviderCreate {
  let options: Record<string, unknown> = {};
  try {
    options = JSON.parse(form.optionsJson || "{}");
  } catch {
    throw new Error("Options must be valid JSON.");
  }
  const priority = parseInt(form.priority, 10);
  if (!Number.isFinite(priority)) throw new Error("Priority must be a number.");
  return {
    name: form.name.trim(),
    kind: form.kind,
    base_url: form.base_url.trim(),
    api_key: form.api_key && form.api_key.length > 0 ? form.api_key : null,
    default_model: form.default_model.trim(),
    is_enabled: form.is_enabled,
    priority,
    options,
  };
}

function toUpdatePayload(form: ProviderForm): AIProviderUpdate {
  let options: Record<string, unknown> = {};
  try {
    options = JSON.parse(form.optionsJson || "{}");
  } catch {
    throw new Error("Options must be valid JSON.");
  }
  const priority = parseInt(form.priority, 10);
  if (!Number.isFinite(priority)) throw new Error("Priority must be a number.");
  // null on api_key means "do not touch" — only send when operator
  // either typed a new value or explicitly cleared.
  const update: AIProviderUpdate = {
    name: form.name.trim(),
    base_url: form.base_url.trim(),
    default_model: form.default_model.trim(),
    is_enabled: form.is_enabled,
    priority,
    options,
  };
  if (form.api_key !== null) {
    update.api_key = form.api_key;
  }
  return update;
}

function ProviderEditor({
  initial,
  mode,
  onClose,
  onSave,
  saving,
  error,
  testResult,
  testing,
  onTest,
}: {
  initial: ProviderForm;
  mode: "create" | "edit";
  onClose: () => void;
  onSave: (form: ProviderForm) => void;
  saving: boolean;
  error?: string;
  testResult?: AITestConnectionResult | null;
  testing: boolean;
  onTest: (form: ProviderForm) => void;
}) {
  const [form, setForm] = useState<ProviderForm>(initial);

  function set<K extends keyof ProviderForm>(key: K, v: ProviderForm[K]) {
    setForm((p) => ({ ...p, [key]: v }));
  }

  return (
    <Modal
      title={mode === "create" ? "New AI Provider" : `Edit — ${initial.name}`}
      onClose={onClose}
      wide
    >
      <div className="space-y-4">
        {error && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              Name
            </label>
            <input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="e.g. local-ollama"
              className={inputCls}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              Kind
            </label>
            <select
              value={form.kind}
              onChange={(e) => set("kind", e.target.value as AIProviderKind)}
              disabled={mode === "edit"}
              className={`${inputCls} disabled:opacity-60`}
            >
              {AI_PROVIDER_KIND_AVAILABLE.map((k) => (
                <option key={k} value={k}>
                  {AI_PROVIDER_KIND_LABELS[k]}
                </option>
              ))}
            </select>
            {mode === "edit" && (
              <p className="mt-1 text-xs text-muted-foreground">
                Cannot change after creation.
              </p>
            )}
          </div>
          <div className="col-span-2">
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              Base URL
            </label>
            <input
              value={form.base_url}
              onChange={(e) => set("base_url", e.target.value)}
              placeholder="http://host.docker.internal:11434/v1"
              className={`${inputCls} font-mono text-xs`}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              For Ollama:{" "}
              <code className="font-mono">
                http://host.docker.internal:11434/v1
              </code>{" "}
              (note the <code>/v1</code> suffix). For OpenAI: leave empty or use{" "}
              <code>https://api.openai.com/v1</code>.
            </p>
          </div>
          <div className="col-span-2">
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              API key{" "}
              <span className="text-muted-foreground/60">
                (leave blank to keep unchanged
                {mode === "edit"
                  ? ` — currently ${initial.api_key === null ? "stored" : "—"}`
                  : ""}
                )
              </span>
            </label>
            <input
              type="password"
              value={form.api_key ?? ""}
              onChange={(e) => set("api_key", e.target.value)}
              placeholder={
                mode === "edit"
                  ? "Type to replace, or clear to remove"
                  : "Optional — local providers (Ollama, LM Studio) don't need one"
              }
              className={`${inputCls} font-mono text-xs`}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              Default model
            </label>
            <input
              value={form.default_model}
              onChange={(e) => set("default_model", e.target.value)}
              placeholder="e.g. llama3.1:8b or gpt-4o-mini"
              className={`${inputCls} font-mono text-xs`}
            />
            {testResult?.ok && testResult.sample_models.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                <span className="text-xs text-muted-foreground">Detected:</span>
                {testResult.sample_models.map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => set("default_model", m)}
                    className={`rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
                      form.default_model === m
                        ? "border-primary bg-primary/10 text-foreground"
                        : "text-muted-foreground hover:border-foreground/30 hover:text-foreground"
                    }`}
                    title={`Use ${m}`}
                  >
                    {m}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              Priority{" "}
              <span className="text-muted-foreground/60">
                (lower = preferred)
              </span>
            </label>
            <input
              value={form.priority}
              onChange={(e) => set("priority", e.target.value)}
              className={inputCls}
            />
          </div>
          <div className="col-span-2">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.is_enabled}
                onChange={(e) => set("is_enabled", e.target.checked)}
              />
              Enabled
            </label>
          </div>
          <div className="col-span-2">
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              Options (JSON){" "}
              <span className="text-muted-foreground/60">
                — temperature, max_tokens, request_timeout_seconds, …
              </span>
            </label>
            <textarea
              value={form.optionsJson}
              onChange={(e) => set("optionsJson", e.target.value)}
              rows={4}
              className={`${inputCls} font-mono text-xs`}
            />
          </div>
        </div>

        {testResult && (
          <div
            className={`rounded-md border px-3 py-2 text-sm ${
              testResult.ok
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                : "border-destructive/30 bg-destructive/10 text-destructive"
            }`}
          >
            <div className="flex items-center gap-2">
              {testResult.ok ? (
                <CheckCircle2 className="h-4 w-4" />
              ) : (
                <XCircle className="h-4 w-4" />
              )}
              <span className="font-medium">{testResult.detail}</span>
              {testResult.latency_ms !== null && (
                <span className="text-xs opacity-70">
                  · {testResult.latency_ms} ms
                </span>
              )}
            </div>
            {testResult.sample_models.length > 0 && (
              <div className="mt-2 text-xs break-all">
                Sample models:{" "}
                <span className="font-mono">
                  {testResult.sample_models.join(", ")}
                </span>
              </div>
            )}
          </div>
        )}

        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <button
            disabled={testing || !form.name.trim()}
            onClick={() => onTest(form)}
            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
          >
            {testing ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <PlugZap className="h-4 w-4" />
            )}
            {testing ? "Testing…" : "Test connection"}
          </button>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
            >
              Cancel
            </button>
            <button
              disabled={saving || !form.name.trim()}
              onClick={() => onSave(form)}
              className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
            >
              {saving ? "Saving…" : mode === "create" ? "Create" : "Save"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

function ModelPickerModal({
  provider,
  onClose,
  onPick,
}: {
  provider: AIProvider;
  onClose: () => void;
  onPick: (model: string) => void;
}) {
  const modelsQ = useQuery({
    queryKey: ["ai-models", provider.id],
    queryFn: () => aiApi.listModels(provider.id),
  });

  return (
    <Modal title={`Models — ${provider.name}`} onClose={onClose}>
      <div className="space-y-3">
        {modelsQ.isLoading && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        )}
        {modelsQ.error && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            Failed to fetch models —{" "}
            {(modelsQ.error as Error)?.message ?? "unknown error"}
          </div>
        )}
        {modelsQ.data && modelsQ.data.length === 0 && (
          <div className="text-sm text-muted-foreground">
            No models available. For local providers (Ollama, LM Studio), pull a
            model first.
          </div>
        )}
        {modelsQ.data && modelsQ.data.length > 0 && (
          <div className="max-h-[60vh] overflow-y-auto rounded-md border">
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/30 text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">Model</th>
                  <th className="px-3 py-2">Owner</th>
                  <th className="px-3 py-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {modelsQ.data.map((m) => (
                  <tr key={m.id} className="border-b last:border-b-0">
                    <td className="px-3 py-2 font-mono text-xs">{m.id}</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {m.owned_by || "—"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={() => onPick(m.id)}
                        className="rounded-md border px-2 py-1 text-xs hover:bg-accent"
                      >
                        Set as default
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex justify-end border-t pt-3">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

export function AIProvidersPage() {
  const qc = useQueryClient();
  const providersQ = useQuery({
    queryKey: ["ai-providers"],
    queryFn: aiApi.listProviders,
  });

  const [editor, setEditor] = useState<
    | null
    | { mode: "create"; initial: ProviderForm }
    | { mode: "edit"; provider: AIProvider; initial: ProviderForm }
  >(null);
  const [editorErr, setEditorErr] = useState<string>("");
  const [testResult, setTestResult] = useState<AITestConnectionResult | null>(
    null,
  );
  const [modelPickerFor, setModelPickerFor] = useState<AIProvider | null>(null);
  const [rowTestId, setRowTestId] = useState<string | null>(null);

  const createMut = useMutation({
    mutationFn: (body: AIProviderCreate) => aiApi.createProvider(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-providers"] });
      setEditor(null);
      setEditorErr("");
      setTestResult(null);
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? String(err);
      setEditorErr(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: AIProviderUpdate }) =>
      aiApi.updateProvider(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-providers"] });
      setEditor(null);
      setEditorErr("");
      setTestResult(null);
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? String(err);
      setEditorErr(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => aiApi.deleteProvider(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-providers"] }),
  });

  const testEditorMut = useMutation({
    mutationFn: async (form: ProviderForm) => {
      const payload = toCreatePayload(form);
      return aiApi.testUnsaved({
        kind: payload.kind,
        base_url: payload.base_url,
        api_key: payload.api_key ?? null,
        default_model: payload.default_model,
        options: payload.options ?? {},
      });
    },
    onSuccess: (r) => setTestResult(r),
    onError: (err: unknown) =>
      setTestResult({
        ok: false,
        detail: String(err),
        latency_ms: null,
        sample_models: [],
      }),
  });

  const testRowMut = useMutation({
    mutationFn: async (id: string) => {
      setRowTestId(id);
      try {
        return await aiApi.testProvider(id);
      } finally {
        setRowTestId(null);
      }
    },
    onSuccess: (r) => setTestResult(r),
    onError: (err: unknown) =>
      setTestResult({
        ok: false,
        detail: String(err),
        latency_ms: null,
        sample_models: [],
      }),
  });

  function handleSave(form: ProviderForm) {
    setEditorErr("");
    if (editor?.mode === "edit") {
      try {
        updateMut.mutate({
          id: editor.provider.id,
          body: toUpdatePayload(form),
        });
      } catch (e) {
        setEditorErr(e instanceof Error ? e.message : String(e));
      }
      return;
    }
    try {
      createMut.mutate(toCreatePayload(form));
    } catch (e) {
      setEditorErr(e instanceof Error ? e.message : String(e));
    }
  }

  function handleTest(form: ProviderForm) {
    setTestResult(null);
    try {
      testEditorMut.mutate(form);
    } catch (e) {
      setTestResult({
        ok: false,
        detail: e instanceof Error ? e.message : String(e),
        latency_ms: null,
        sample_models: [],
      });
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <Sparkles className="h-5 w-5" /> AI Providers
          </h1>
          <p className="text-sm text-muted-foreground">
            Configure LLM providers for the Operator Copilot. Wave 1 ships the
            OpenAI-compatible driver — works with OpenAI, Ollama, OpenWebUI,
            vLLM, LM Studio, and most local model servers. Anthropic / Gemini /
            Azure drivers ship in Phase 2.
          </p>
        </div>
        <button
          onClick={() => {
            setEditorErr("");
            setTestResult(null);
            setEditor({ mode: "create", initial: { ...EMPTY } });
          }}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
        >
          <Plus className="h-4 w-4" /> New provider
        </button>
      </div>

      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="border-b bg-muted/30 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Kind</th>
              <th className="px-3 py-2">Base URL</th>
              <th className="px-3 py-2">Default model</th>
              <th className="px-3 py-2">Enabled</th>
              <th className="px-3 py-2">Priority</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {providersQ.isLoading && (
              <tr>
                <td
                  colSpan={7}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  Loading…
                </td>
              </tr>
            )}
            {!providersQ.isLoading && (providersQ.data ?? []).length === 0 && (
              <tr>
                <td
                  colSpan={7}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  No providers configured. Click <strong>New provider</strong>{" "}
                  to add one.
                </td>
              </tr>
            )}
            {(providersQ.data ?? []).map((p) => (
              <tr key={p.id} className="border-b last:border-b-0">
                <td className="px-3 py-2 align-top">
                  <div className="font-medium break-words">{p.name}</div>
                  {p.has_api_key && (
                    <div className="text-xs text-muted-foreground">
                      🔒 key stored
                    </div>
                  )}
                </td>
                <td
                  className="px-3 py-2 align-top text-xs"
                  title={AI_PROVIDER_KIND_LABELS[p.kind] ?? p.kind}
                >
                  {AI_PROVIDER_KIND_SHORT[p.kind] ?? p.kind}
                </td>
                <td className="px-3 py-2 align-top font-mono text-xs break-all">
                  {p.base_url || "—"}
                </td>
                <td className="px-3 py-2 align-top font-mono text-xs break-all">
                  {p.default_model || "—"}
                </td>
                <td className="px-3 py-2 align-top text-xs">
                  {p.is_enabled ? (
                    <span className="rounded bg-emerald-500/15 px-2 py-0.5 text-emerald-700 dark:text-emerald-400">
                      enabled
                    </span>
                  ) : (
                    <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground">
                      disabled
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 align-top text-xs">{p.priority}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    onClick={() => testRowMut.mutate(p.id)}
                    disabled={rowTestId === p.id}
                    className="mr-1 inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent disabled:opacity-50"
                    title="Test connection"
                  >
                    {rowTestId === p.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <PlugZap className="h-3 w-3" />
                    )}
                  </button>
                  <button
                    onClick={() => setModelPickerFor(p)}
                    className="mr-1 inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
                    title="List models"
                  >
                    <RefreshCw className="h-3 w-3" />
                  </button>
                  <button
                    onClick={() => {
                      setEditorErr("");
                      setTestResult(null);
                      setEditor({
                        mode: "edit",
                        provider: p,
                        initial: formFromProvider(p),
                      });
                    }}
                    className="mr-1 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Delete provider "${p.name}"?`))
                        deleteMut.mutate(p.id);
                    }}
                    className="rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-destructive"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {testResult && !editor && (
        <div
          className={`rounded-md border px-3 py-2 text-sm ${
            testResult.ok
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : "border-destructive/30 bg-destructive/10 text-destructive"
          }`}
        >
          <div className="flex items-center gap-2">
            {testResult.ok ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : (
              <XCircle className="h-4 w-4" />
            )}
            <span className="font-medium">{testResult.detail}</span>
            {testResult.latency_ms !== null && (
              <span className="text-xs opacity-70">
                · {testResult.latency_ms} ms
              </span>
            )}
            <button
              onClick={() => setTestResult(null)}
              className="ml-auto text-xs opacity-70 hover:opacity-100"
            >
              dismiss
            </button>
          </div>
          {testResult.sample_models.length > 0 && (
            <div className="mt-1 text-xs break-all">
              Sample models:{" "}
              <span className="font-mono">
                {testResult.sample_models.join(", ")}
              </span>
            </div>
          )}
        </div>
      )}

      {editor && (
        <ProviderEditor
          mode={editor.mode}
          initial={editor.initial}
          error={editorErr}
          saving={createMut.isPending || updateMut.isPending}
          onClose={() => {
            setEditor(null);
            setEditorErr("");
            setTestResult(null);
          }}
          onSave={handleSave}
          testResult={testResult}
          testing={testEditorMut.isPending}
          onTest={handleTest}
        />
      )}

      {modelPickerFor && (
        <ModelPickerModal
          provider={modelPickerFor}
          onClose={() => setModelPickerFor(null)}
          onPick={(model) => {
            updateMut.mutate({
              id: modelPickerFor.id,
              body: { default_model: model },
            });
            setModelPickerFor(null);
          }}
        />
      )}
    </div>
  );
}
