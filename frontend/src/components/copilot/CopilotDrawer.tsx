import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
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
export function CopilotDrawer({ onClose }: { onClose: () => void }) {
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

  const detailQ = useQuery({
    queryKey: ["ai-session", activeSessionId],
    queryFn: () =>
      activeSessionId
        ? aiApi.getSession(activeSessionId)
        : Promise.resolve(null),
    enabled: activeSessionId !== null,
  });

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
      setPendingUserMessage(message);
      let resolvedSessionId = activeSessionId;
      try {
        for await (const ev of streamChatTurn(
          {
            message,
            session_id: activeSessionId ?? undefined,
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
          } else if (ev.event === "error") {
            setStreamingError(ev.data.message as string);
          }
        }
      } finally {
        abortRef.current = null;
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
          {pendingUserMessage && (
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
            />
          )}
        </div>

        {/* Composer */}
        <ChatComposer
          disabled={sendMut.isPending}
          onSend={(text) => sendMut.mutate(text)}
          onCancel={() => abortRef.current?.abort()}
          isStreaming={sendMut.isPending}
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
}) {
  return (
    <div className="mb-3 flex justify-start">
      <div className="max-w-[85%] space-y-2">
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
}: {
  disabled: boolean;
  onSend: (text: string) => void;
  onCancel: () => void;
  isStreaming: boolean;
}) {
  const [text, setText] = useState("");
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  function submit() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
    taRef.current?.focus();
  }
  return (
    <form
      className="flex gap-2 border-t p-3"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
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
    </form>
  );
}
