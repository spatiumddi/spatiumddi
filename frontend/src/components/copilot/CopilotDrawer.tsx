import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  BookOpen,
  Loader2,
  MessageSquarePlus,
  Pencil,
  Send,
  Sparkles,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import {
  aiApi,
  streamChatTurn,
  type AIChatMessage,
  type AIChatSessionSummary,
  type AIPrompt,
} from "@/lib/api";

/**
 * Chat drawer (issue #90 Wave 3). Slides in from the right when the
 * floating button (or Cmd/Ctrl+K) opens it.
 *
 * Layout:
 *   ┌─────────────────────────────────────┐
 *   │ ✨ Operator Copilot   [history ▾] x │
 *   │ model: llama3.1:8b on local-ollama  │
 *   ├─────────────────────────────────────┤
 *   │ <message stream>                    │
 *   │   user message    →                 │
 *   │   ← assistant response              │
 *   │   ← [tool: list_subnets] ✓          │
 *   ├─────────────────────────────────────┤
 *   │ [textarea]                  [send]  │
 *   └─────────────────────────────────────┘
 */
export function CopilotDrawer({
  onClose,
  pendingContext,
  onContextConsumed,
  pendingPrompt,
  onPromptConsumed,
}: {
  onClose: () => void;
  /** Set when the drawer was opened via ``askAI({ context })``. Forwarded
   *  to the chat endpoint as ``initial_context`` on the first new-session
   *  turn so it pre-seeds the system prompt. */
  pendingContext?: string | null;
  /** Called by the drawer once ``pendingContext`` has been consumed
   *  (i.e. the first chat turn fired with it included), so the parent
   *  can clear its state and avoid re-injecting on subsequent opens. */
  onContextConsumed?: () => void;
  /** Set when the drawer was opened via ``askAI({ prompt })``
   *  (typically from the Cmd-K palette). Pre-fills the chat composer
   *  textarea so the operator can review or edit before sending. */
  pendingPrompt?: string | null;
  /** Called once ``pendingPrompt`` has been written into the composer
   *  state, so the parent doesn't keep re-injecting it on re-renders. */
  onPromptConsumed?: () => void;
}) {
  const qc = useQueryClient();
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  // Local in-flight stream — mirror of what's also being persisted on
  // the backend. Reset whenever a stream ends (success or failure).
  const [streamingContent, setStreamingContent] = useState<string>("");
  const [streamingTools, setStreamingTools] = useState<
    {
      id: string;
      name: string;
      arguments: string;
      preview?: string;
      is_error?: boolean;
    }[]
  >([]);
  const [streamingError, setStreamingError] = useState<string | null>(null);
  // Failover notices emitted by the backend orchestrator when the
  // primary provider failed transiently and the request was retried
  // on a fallback. ``null`` when no failover happened on this turn.
  const [streamingFailover, setStreamingFailover] = useState<{
    from_provider: string;
    to_provider: string;
    to_model: string;
    reason: string;
  } | null>(null);
  // Echo of the just-sent user message — kept in component state until
  // the backend stream ends and detailQ refetches the persisted version.
  // Without this, second-and-later turns appear to "swallow" the user
  // message: the textbox clears but nothing renders until the LLM
  // finishes responding (which may be 5–30 s on a slow local model).
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(
    null,
  );
  const [showHistory, setShowHistory] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const sessionsQ = useQuery({
    queryKey: ["ai-sessions"],
    queryFn: () => aiApi.listSessions(false),
    staleTime: 30 * 1000,
  });

  // Wave 4 — operator's own today-so-far totals + caps. Refetches
  // after every chat turn so the drawer's progress bar stays current.
  const usageQ = useQuery({
    queryKey: ["ai-usage-me"],
    queryFn: aiApi.myUsage,
    staleTime: 10 * 1000,
    refetchOnWindowFocus: false,
  });

  const detailQ = useQuery({
    queryKey: ["ai-session", activeSessionId],
    queryFn: () =>
      activeSessionId
        ? aiApi.getSession(activeSessionId)
        : Promise.resolve(null),
    enabled: activeSessionId !== null,
  });

  // ``pendingContext`` arrives when the drawer was opened via
  // ``askAI({ context })`` (right-click "Ask AI about this"). Whenever
  // a context is present, force a fresh session — the operator's
  // intent is "ask the model about this resource I'm looking at",
  // not "tack this onto whatever conversation I had open." Without
  // this reset the context would be silently dropped on existing
  // sessions because the system prompt is snapshotted at session
  // create.
  useEffect(() => {
    if (pendingContext) {
      setActiveSessionId(null);
    }
  }, [pendingContext]);

  // Esc closes the drawer.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Auto-scroll the message list as new content arrives.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [
    detailQ.data,
    streamingContent,
    streamingTools.length,
    streamingError,
    pendingUserMessage,
  ]);

  const sendMut = useMutation({
    mutationFn: async (message: string) => {
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setStreamingContent("");
      setStreamingTools([]);
      setStreamingError(null);
      setStreamingFailover(null);
      setPendingUserMessage(message);
      let resolvedSessionId = activeSessionId;
      // ``initial_context`` only matters on a *new* session — the
      // backend ignores it once a session has its system prompt
      // snapshotted. Notify the parent the moment we send it so it
      // doesn't reinject on a follow-up "Ask AI" without resetting.
      const includeContext =
        activeSessionId === null && Boolean(pendingContext);
      try {
        for await (const ev of streamChatTurn(
          {
            message,
            session_id: activeSessionId ?? undefined,
            initial_context: includeContext
              ? (pendingContext ?? undefined)
              : undefined,
          },
          ctrl.signal,
        )) {
          if (ev.event === "session") {
            const sid = ev.data.session_id as string;
            if (!resolvedSessionId) {
              resolvedSessionId = sid;
              setActiveSessionId(sid);
            }
          } else if (ev.event === "content") {
            setStreamingContent((prev) => prev + (ev.data.delta as string));
          } else if (ev.event === "tool_call") {
            setStreamingTools((prev) => [
              ...prev,
              {
                id: ev.data.id as string,
                name: ev.data.name as string,
                arguments: ev.data.arguments as string,
              },
            ]);
          } else if (ev.event === "tool_result") {
            setStreamingTools((prev) =>
              prev.map((t) =>
                t.id === ev.data.tool_call_id
                  ? {
                      ...t,
                      preview: ev.data.preview as string,
                      is_error: ev.data.is_error as boolean,
                    }
                  : t,
              ),
            );
          } else if (ev.event === "info") {
            // Phase 2 failover notice — primary provider failed
            // transiently and the orchestrator retried on a fallback.
            if (ev.data.kind === "failover") {
              setStreamingFailover({
                from_provider: ev.data.from_provider as string,
                to_provider: ev.data.to_provider as string,
                to_model: ev.data.to_model as string,
                reason: ev.data.reason as string,
              });
            }
          } else if (ev.event === "error") {
            setStreamingError(ev.data.message as string);
          }
        }
      } finally {
        abortRef.current = null;
        if (includeContext) {
          // Tell the parent we consumed the initial context — it will
          // clear its ``pendingContext`` state so subsequent opens
          // (without an ``askAI`` event) don't re-inject anything.
          onContextConsumed?.();
        }
      }
      return resolvedSessionId;
    },
    onSettled: (sid) => {
      // Refetch the session detail + session list so the persisted
      // assistant message appears + the in-flight buffer drops.
      qc.invalidateQueries({ queryKey: ["ai-sessions"] });
      if (sid) {
        qc.invalidateQueries({ queryKey: ["ai-session", sid] });
      }
      // Refresh the usage panel — message count + cost will have
      // ticked up after the persisted assistant message.
      qc.invalidateQueries({ queryKey: ["ai-usage-me"] });
      setStreamingContent("");
      setStreamingTools([]);
      setPendingUserMessage(null);
    },
  });

  const renameMut = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      aiApi.updateSession(id, { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-sessions"] }),
  });

  const archiveMut = useMutation({
    mutationFn: (id: string) => aiApi.updateSession(id, { archived: true }),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["ai-sessions"] });
      if (id === activeSessionId) {
        setActiveSessionId(null);
      }
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => aiApi.deleteSession(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["ai-sessions"] });
      if (id === activeSessionId) {
        setActiveSessionId(null);
      }
    },
  });

  const messages = useMemo<AIChatMessage[]>(() => {
    if (!detailQ.data) return [];
    return detailQ.data.messages.filter((m) => m.role !== "system");
  }, [detailQ.data]);

  // The most recent user message in the persisted history. Used to
  // suppress the optimistic ``pendingUserMessage`` bubble once the
  // canonical version has shown up — without this, the first turn
  // of a new session double-renders the user message: once from
  // ``pendingUserMessage`` (still set until onSettled), once from
  // detailQ (refetched the moment ``session_id`` arrives mid-stream).
  const lastUserMessageContent = useMemo<string | null>(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "user") return messages[i].content;
    }
    return null;
  }, [messages]);
  const showPendingUserMessage =
    pendingUserMessage !== null &&
    pendingUserMessage !== lastUserMessageContent;

  const sessionLabel = useMemo(() => {
    if (!detailQ.data) return null;
    return `${detailQ.data.model}${detailQ.data.provider_id ? "" : " (provider missing)"}`;
  }, [detailQ.data]);

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/20"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex h-full w-full max-w-2xl flex-col border-l bg-card shadow-2xl"
      >
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              <h2 className="text-sm font-semibold">Operator Copilot</h2>
            </div>
            {sessionLabel && (
              <div className="mt-0.5 truncate text-xs text-muted-foreground font-mono">
                {sessionLabel}
              </div>
            )}
            {usageQ.data && <UsageChip usage={usageQ.data} />}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => setActiveSessionId(null)}
              title="New chat"
              className="rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
            >
              <MessageSquarePlus className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => setShowHistory((v) => !v)}
              title="Recent chats"
              className={`rounded-md border px-2 py-1 text-xs ${
                showHistory
                  ? "bg-accent text-foreground"
                  : "text-muted-foreground hover:bg-accent"
              }`}
            >
              History
            </button>
            <button
              type="button"
              onClick={onClose}
              title="Close (Esc)"
              className="rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        {/* History dropdown */}
        {showHistory && (
          <div className="max-h-[40vh] overflow-y-auto border-b bg-muted/30">
            {(sessionsQ.data ?? []).length === 0 && (
              <div className="px-4 py-6 text-center text-xs text-muted-foreground">
                No chat history yet.
              </div>
            )}
            {(sessionsQ.data ?? []).map((s) => (
              <SessionRow
                key={s.id}
                session={s}
                active={s.id === activeSessionId}
                onPick={() => {
                  setActiveSessionId(s.id);
                  setShowHistory(false);
                }}
                onRename={(name) => renameMut.mutate({ id: s.id, name })}
                onArchive={() => archiveMut.mutate(s.id)}
                onDelete={() => deleteMut.mutate(s.id)}
              />
            ))}
          </div>
        )}

        {/* Message stream */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
          {!activeSessionId && !sendMut.isPending && (
            <EmptyState onPick={(q) => sendMut.mutate(q)} />
          )}
          {messages.map((m) => (
            <MessageBubble key={m.id} message={m} />
          ))}
          {/* Optimistic echo of the just-sent user message. Stays
              visible from the moment the operator hits Send until
              detailQ refetches with the persisted version after the
              stream closes — without this, second-and-later turns
              appear to swallow the user's message during the LLM's
              think time. */}
          {showPendingUserMessage && (
            <div className="mb-3 flex justify-end">
              <div className="max-w-[85%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
                <pre className="whitespace-pre-wrap break-words font-sans">
                  {pendingUserMessage}
                </pre>
              </div>
            </div>
          )}
          {/* In-flight streaming */}
          {sendMut.isPending && (
            <StreamingBubble
              content={streamingContent}
              tools={streamingTools}
              error={streamingError}
              failover={streamingFailover}
            />
          )}
        </div>

        {/* Composer */}
        <ChatComposer
          disabled={sendMut.isPending}
          onSend={(text) => sendMut.mutate(text)}
          onCancel={() => abortRef.current?.abort()}
          isStreaming={sendMut.isPending}
          pendingPrompt={pendingPrompt ?? null}
          onPromptConsumed={onPromptConsumed}
        />
      </div>
    </div>
  );
}

function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  const examples = [
    "How many subnets do I have?",
    "Find the IP 192.168.0.1",
    "List my DNS zones",
    "Who changed something in the last hour?",
    "What DHCP scopes are configured?",
  ];
  return (
    <div className="mx-auto max-w-md py-8 text-center text-sm">
      <Sparkles className="mx-auto mb-2 h-6 w-6 text-primary" />
      <p className="font-medium">
        Ask the copilot anything about your infrastructure.
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        Read-only for now — questions about IPAM, DNS, DHCP, alerts, audit logs.
        Click an example to try it, or type your own.
      </p>
      <div className="mt-4 space-y-1.5">
        {examples.map((q) => (
          <button
            key={q}
            type="button"
            onClick={() => onPick(q)}
            className="block w-full rounded-md border bg-muted/30 px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:bg-primary/5 hover:text-foreground"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: AIChatMessage }) {
  if (message.role === "tool") {
    // Detect proposal-shape tool results — render the Apply / Discard
    // card instead of the raw JSON tool envelope. Pattern matches the
    // contract from ``app/services/ai/tools/proposals.py``.
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(message.content);
    } catch {
      // Not JSON — fall through to plain ToolCard.
    }
    if (
      parsed != null &&
      typeof parsed === "object" &&
      (parsed as { kind?: string }).kind === "proposal" &&
      typeof (parsed as { proposal_id?: string }).proposal_id === "string"
    ) {
      return (
        <ProposalCard
          proposalId={(parsed as { proposal_id: string }).proposal_id}
          operation={(parsed as { operation?: string }).operation ?? ""}
          previewText={(parsed as { preview?: string }).preview ?? ""}
        />
      );
    }
    return <ToolCard message={message} />;
  }
  const isUser = message.role === "user";
  return (
    <div className={`mb-3 flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-muted text-foreground"
        }`}
      >
        {message.content && (
          <pre className="whitespace-pre-wrap break-words font-sans">
            {message.content}
          </pre>
        )}
        {message.tool_calls && message.tool_calls.length > 0 && (
          <div className="mt-2 space-y-1">
            {message.tool_calls.map((tc) => (
              <div
                key={tc.id}
                className="flex items-center gap-1.5 rounded border border-foreground/10 bg-background/50 px-2 py-1 text-xs"
              >
                <Wrench className="h-3 w-3" />
                <code className="font-mono">{tc.name}</code>
                <span className="truncate text-muted-foreground">
                  {tc.arguments && tc.arguments !== "{}" ? tc.arguments : ""}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Apply / Discard card rendered in place of a raw tool result for
 *  proposal-shape payloads. Lazy-fetches the canonical proposal row
 *  on mount so it can show the latest terminal state when the chat
 *  history is replayed (e.g. the operator already applied or
 *  discarded earlier).
 */
function ProposalCard({
  proposalId,
  operation,
  previewText,
}: {
  proposalId: string;
  operation: string;
  previewText: string;
}) {
  const qc = useQueryClient();
  const proposalQ = useQuery({
    queryKey: ["ai-proposal", proposalId],
    queryFn: () => aiApi.getProposal(proposalId),
    // Refresh on every mount so a session replay shows the right
    // applied / discarded / expired state.
    staleTime: 0,
  });

  const applyMut = useMutation({
    mutationFn: () => aiApi.applyProposal(proposalId),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["ai-proposal", proposalId] });
    },
  });
  const discardMut = useMutation({
    mutationFn: () => aiApi.discardProposal(proposalId),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["ai-proposal", proposalId] });
    },
  });

  const proposal = proposalQ.data;
  const applied = !!proposal?.applied_at;
  const discarded = !!proposal?.discarded_at;
  const expired =
    !!proposal?.expires_at &&
    !applied &&
    !discarded &&
    new Date(proposal.expires_at).getTime() < Date.now();
  const pending = !applied && !discarded && !expired;

  let badge: { text: string; cls: string };
  if (applied) {
    badge = {
      text: "applied",
      cls: "bg-emerald-500/10 text-emerald-600 border-emerald-500/30",
    };
  } else if (discarded) {
    badge = {
      text: "discarded",
      cls: "bg-zinc-500/10 text-zinc-600 border-zinc-500/30",
    };
  } else if (expired) {
    badge = {
      text: "expired",
      cls: "bg-amber-500/10 text-amber-600 border-amber-500/30",
    };
  } else {
    badge = {
      text: "pending",
      cls: "bg-primary/10 text-primary border-primary/30",
    };
  }

  return (
    <div className="mb-3 flex justify-start">
      <div className="max-w-[85%] rounded-lg border border-primary/30 bg-primary/5 p-3 text-sm">
        <div className="flex items-center gap-2">
          <Sparkles className="h-3.5 w-3.5 text-primary" />
          <span className="font-medium">Proposed: {operation}</span>
          <span
            className={`ml-auto rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider ${badge.cls}`}
          >
            {badge.text}
          </span>
        </div>
        <div className="mt-2 whitespace-pre-wrap break-words font-sans text-xs text-foreground/90">
          {proposal?.preview_text || previewText || "(no preview)"}
        </div>
        {applied && proposal?.result && (
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer text-muted-foreground">
              Result
            </summary>
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px]">
              {JSON.stringify(proposal.result, null, 2)}
            </pre>
          </details>
        )}
        {proposal?.error && (
          <div className="mt-2 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">
            {proposal.error}
          </div>
        )}
        {pending && (
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={() => applyMut.mutate()}
              disabled={applyMut.isPending || discardMut.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {applyMut.isPending ? "Applying…" : "Apply"}
            </button>
            <button
              type="button"
              onClick={() => discardMut.mutate()}
              disabled={applyMut.isPending || discardMut.isPending}
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1 text-xs hover:bg-accent disabled:opacity-50"
            >
              {discardMut.isPending ? "Discarding…" : "Discard"}
            </button>
            {proposal?.expires_at && (
              <span className="ml-auto text-[10px] text-muted-foreground">
                expires {new Date(proposal.expires_at).toLocaleTimeString()}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ToolCard({ message }: { message: AIChatMessage }) {
  let parsed: unknown = null;
  try {
    parsed = JSON.parse(message.content);
  } catch {
    parsed = message.content;
  }
  const isError =
    parsed != null &&
    typeof parsed === "object" &&
    "error" in (parsed as Record<string, unknown>);
  return (
    <div className="mb-3 flex justify-start">
      <details
        className={`max-w-[85%] rounded-lg border px-3 py-2 text-xs ${
          isError
            ? "border-destructive/30 bg-destructive/5 text-destructive"
            : "border-foreground/10 bg-muted/40 text-muted-foreground"
        }`}
      >
        <summary className="flex cursor-pointer items-center gap-1.5">
          <Wrench className="h-3 w-3" />
          <code className="font-mono">{message.name ?? "tool"}</code>
          <span className="ml-1 text-foreground/60">
            {isError ? "↯ failed" : "✓"}
          </span>
        </summary>
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] leading-relaxed">
          {JSON.stringify(parsed, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function StreamingBubble({
  content,
  tools,
  error,
  failover,
}: {
  content: string;
  tools: {
    id: string;
    name: string;
    arguments: string;
    preview?: string;
    is_error?: boolean;
  }[];
  error: string | null;
  failover: {
    from_provider: string;
    to_provider: string;
    to_model: string;
    reason: string;
  } | null;
}) {
  return (
    <div className="mb-3 flex justify-start">
      <div className="max-w-[85%] space-y-2">
        {failover && (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-1.5 text-xs text-amber-700 dark:text-amber-300">
            <span className="font-medium">Failed over</span>{" "}
            <span className="opacity-80">
              from <code className="font-mono">{failover.from_provider}</code>{" "}
              to <code className="font-mono">{failover.to_provider}</code> ·
              model <code className="font-mono">{failover.to_model}</code> ·
              reason <code className="font-mono">{failover.reason}</code>
            </span>
          </div>
        )}
        {tools.map((t) => (
          <div
            key={t.id}
            className={`flex items-center gap-1.5 rounded border px-2 py-1 text-xs ${
              t.is_error
                ? "border-destructive/30 bg-destructive/5 text-destructive"
                : "border-foreground/10 bg-muted/40 text-muted-foreground"
            }`}
          >
            <Wrench className="h-3 w-3" />
            <code className="font-mono">{t.name}</code>
            {t.preview === undefined ? (
              <Loader2 className="ml-auto h-3 w-3 animate-spin" />
            ) : (
              <span className="ml-auto">{t.is_error ? "↯ failed" : "✓"}</span>
            )}
          </div>
        ))}
        {(content || (!content && tools.length === 0 && !error)) && (
          <div className="rounded-lg bg-muted px-3 py-2 text-sm">
            {content ? (
              <pre className="whitespace-pre-wrap break-words font-sans">
                {content}
                <span className="ml-1 inline-block h-3 w-1 animate-pulse bg-foreground/50" />
              </pre>
            ) : (
              <Loader2 className="h-4 w-4 animate-spin" />
            )}
          </div>
        )}
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}

function SessionRow({
  session,
  active,
  onPick,
  onRename,
  onArchive,
  onDelete,
}: {
  session: AIChatSessionSummary;
  active: boolean;
  onPick: () => void;
  onRename: (name: string) => void;
  onArchive: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      className={`group flex items-center gap-2 px-4 py-2 text-sm hover:bg-accent ${
        active ? "bg-accent" : ""
      }`}
    >
      <button
        type="button"
        onClick={onPick}
        className="min-w-0 flex-1 text-left"
      >
        <div className="truncate font-medium">{session.name}</div>
        <div className="text-xs text-muted-foreground">
          {session.message_count} msg
          {" · "}
          <span className="font-mono">{session.model}</span>
        </div>
      </button>
      <button
        type="button"
        onClick={() => {
          const next = prompt("Rename session", session.name);
          if (next && next.trim()) onRename(next.trim());
        }}
        title="Rename"
        className="opacity-0 group-hover:opacity-100"
      >
        <Pencil className="h-3 w-3 text-muted-foreground hover:text-foreground" />
      </button>
      <button
        type="button"
        onClick={onArchive}
        title="Archive"
        className="opacity-0 group-hover:opacity-100"
      >
        <Archive className="h-3 w-3 text-muted-foreground hover:text-foreground" />
      </button>
      <button
        type="button"
        onClick={() => {
          if (confirm(`Delete chat "${session.name}"?`)) onDelete();
        }}
        title="Delete"
        className="opacity-0 group-hover:opacity-100"
      >
        <Trash2 className="h-3 w-3 text-muted-foreground hover:text-destructive" />
      </button>
    </div>
  );
}

function ChatComposer({
  disabled,
  onSend,
  onCancel,
  isStreaming,
  pendingPrompt,
  onPromptConsumed,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
  onCancel: () => void;
  isStreaming: boolean;
  pendingPrompt?: string | null;
  onPromptConsumed?: () => void;
}) {
  const [text, setText] = useState("");
  const [showPrompts, setShowPrompts] = useState(false);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // Pull the prompt library lazily — only fires when the operator
  // clicks the "Prompts ▾" button to open the picker.
  const promptsQ = useQuery({
    queryKey: ["ai-prompts"],
    queryFn: aiApi.listPrompts,
    enabled: showPrompts,
    staleTime: 60_000,
  });

  // Drain ``pendingPrompt`` into the textarea once. Notify the parent
  // via ``onPromptConsumed`` so it clears its state and we don't
  // overwrite operator edits on the next re-render.
  useEffect(() => {
    if (pendingPrompt) {
      setText(pendingPrompt);
      onPromptConsumed?.();
      setTimeout(() => taRef.current?.focus(), 0);
    }
  }, [pendingPrompt, onPromptConsumed]);

  function submit() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
    taRef.current?.focus();
  }

  function loadPrompt(p: AIPrompt) {
    setText(p.prompt_text);
    setShowPrompts(false);
    // Defer focus until after the popover unmounts so React's
    // synchronous reconciliation doesn't steal it back.
    setTimeout(() => taRef.current?.focus(), 0);
  }

  return (
    <form
      className="relative flex flex-col gap-1.5 border-t p-3"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="flex gap-2">
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          placeholder="Ask about IPAM / DNS / DHCP / alerts / audit log…"
          className="flex-1 resize-none rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
        {isStreaming ? (
          <button
            type="button"
            onClick={onCancel}
            className="self-end rounded-md border px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!text.trim() || disabled}
            className="self-end rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Send className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setShowPrompts((v) => !v)}
          className="inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent"
          title="Load a saved prompt"
        >
          <BookOpen className="h-3 w-3" />
          Prompts ▾
        </button>
        <span className="text-[11px] text-muted-foreground/60">
          Enter to send · Shift+Enter for newline
        </span>
      </div>
      {showPrompts && (
        <PromptsPopover
          prompts={promptsQ.data ?? []}
          isLoading={promptsQ.isLoading}
          onClose={() => setShowPrompts(false)}
          onPick={loadPrompt}
        />
      )}
    </form>
  );
}

/** Inline popover anchored above the prompts button. Closes on click
 *  outside or Escape. Two sections — shared prompts (curated) and the
 *  user's own private prompts.
 */
function PromptsPopover({
  prompts,
  isLoading,
  onClose,
  onPick,
}: {
  prompts: AIPrompt[];
  isLoading: boolean;
  onClose: () => void;
  onPick: (p: AIPrompt) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [onClose]);

  const shared = prompts.filter((p) => p.is_shared);
  const mine = prompts.filter((p) => !p.is_shared);

  return (
    <div
      ref={ref}
      className="absolute bottom-[calc(100%-0.25rem)] left-3 z-50 max-h-72 w-80 overflow-auto rounded-md border bg-popover p-1 shadow-lg"
    >
      {isLoading ? (
        <div className="px-3 py-4 text-center text-xs text-muted-foreground">
          Loading…
        </div>
      ) : prompts.length === 0 ? (
        <div className="px-3 py-4 text-center text-xs text-muted-foreground">
          No saved prompts. Curate them at{" "}
          <a className="underline" href="/admin/ai/prompts">
            Admin → AI Prompts
          </a>
          .
        </div>
      ) : (
        <>
          {shared.length > 0 && (
            <div>
              <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                Shared
              </div>
              {shared.map((p) => (
                <PromptRow key={p.id} prompt={p} onPick={onPick} />
              ))}
            </div>
          )}
          {mine.length > 0 && (
            <div>
              <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                Your prompts
              </div>
              {mine.map((p) => (
                <PromptRow key={p.id} prompt={p} onPick={onPick} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function PromptRow({
  prompt,
  onPick,
}: {
  prompt: AIPrompt;
  onPick: (p: AIPrompt) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onPick(prompt)}
      className="block w-full rounded px-2 py-1.5 text-left text-xs hover:bg-accent"
    >
      <div className="font-medium">{prompt.name}</div>
      {prompt.description && (
        <div className="mt-0.5 text-[11px] text-muted-foreground line-clamp-2">
          {prompt.description}
        </div>
      )}
    </button>
  );
}

/**
 * Today-so-far usage indicator (Wave 4). Shows three chips when caps
 * are configured: messages today, tokens today, cost today. When a
 * cap is set, the relevant chip becomes a progress bar that fills as
 * the operator approaches the cap; over-cap chips render in
 * destructive tint.
 */
function UsageChip({ usage }: { usage: import("@/lib/api").AIUsageSnapshot }) {
  const tokens = usage.tokens_in + usage.tokens_out;
  const costNum = parseFloat(usage.cost_usd);
  const capCostNum = usage.cap_cost_usd ? parseFloat(usage.cap_cost_usd) : null;

  // Token cap progress
  const tokenPct =
    usage.cap_token != null && usage.cap_token > 0
      ? Math.min(100, (tokens / usage.cap_token) * 100)
      : null;
  // Cost cap progress
  const costPct =
    capCostNum != null && capCostNum > 0
      ? Math.min(100, (costNum / capCostNum) * 100)
      : null;

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
      <span>
        {usage.messages} msg
        {usage.messages === 1 ? "" : "s"} today
      </span>
      <span aria-hidden>·</span>
      {tokenPct !== null ? (
        <ProgressChip
          label={`${tokens.toLocaleString()} / ${usage.cap_token!.toLocaleString()} tokens`}
          pct={tokenPct}
        />
      ) : (
        <span>{tokens.toLocaleString()} tokens</span>
      )}
      {(costNum > 0 || capCostNum != null) && (
        <>
          <span aria-hidden>·</span>
          {costPct !== null ? (
            <ProgressChip
              label={`$${costNum.toFixed(2)} / $${capCostNum!.toFixed(2)}`}
              pct={costPct}
            />
          ) : (
            <span>${costNum.toFixed(2)}</span>
          )}
        </>
      )}
    </div>
  );
}

function ProgressChip({ label, pct }: { label: string; pct: number }) {
  const over = pct >= 100;
  return (
    <span
      className={`relative inline-flex items-center overflow-hidden rounded border px-1.5 py-0.5 ${
        over
          ? "border-destructive/40 bg-destructive/5 text-destructive"
          : "border-foreground/15"
      }`}
    >
      <span
        aria-hidden
        className={`absolute inset-y-0 left-0 ${
          over ? "bg-destructive/20" : "bg-primary/15"
        }`}
        style={{ width: `${pct}%` }}
      />
      <span className="relative">{label}</span>
    </span>
  );
}
