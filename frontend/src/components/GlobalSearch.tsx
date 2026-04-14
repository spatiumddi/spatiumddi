import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Search, Network, Layers, Globe, MapPin, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { searchApi, type SearchResult } from "@/lib/api";
import { cn } from "@/lib/utils";

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

const TYPE_LABELS: Record<SearchResult["type"], string> = {
  ip_address: "IP Address",
  subnet: "Subnet",
  block: "Block",
  space: "Space",
};

const TYPE_ICONS: Record<SearchResult["type"], React.ElementType> = {
  ip_address: MapPin,
  subnet: Network,
  block: Layers,
  space: Globe,
};

const TYPE_COLORS: Record<SearchResult["type"], string> = {
  ip_address: "text-emerald-500",
  subnet: "text-blue-500",
  block: "text-violet-500",
  space: "text-orange-500",
};

function ResultRow({
  result,
  isActive,
  onSelect,
}: {
  result: SearchResult;
  isActive: boolean;
  onSelect: (r: SearchResult) => void;
}) {
  const Icon = TYPE_ICONS[result.type];
  return (
    <button
      className={cn(
        "flex w-full items-start gap-3 px-4 py-2.5 text-left transition-colors",
        isActive ? "bg-accent" : "hover:bg-accent/50"
      )}
      onMouseDown={(e) => {
        e.preventDefault();
        onSelect(result);
      }}
    >
      <Icon className={cn("mt-0.5 h-4 w-4 flex-shrink-0", TYPE_COLORS[result.type])} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-sm font-medium">{result.display}</span>
          {result.name && result.name !== result.display && (
            <span className="truncate text-xs text-muted-foreground">{result.name}</span>
          )}
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="rounded bg-muted px-1 py-0.5 text-[10px] font-medium">
            {TYPE_LABELS[result.type]}
          </span>
          {result.space_name && <span>{result.space_name}</span>}
          {result.subnet_network && result.type === "ip_address" && (
            <span>{result.subnet_network}</span>
          )}
          {result.hostname && result.type === "ip_address" && result.hostname !== result.display && (
            <span>{result.hostname}</span>
          )}
          {result.mac_address && <span className="font-mono">{result.mac_address}</span>}
          {result.status && (
            <span
              className={cn(
                "rounded px-1 py-0.5 text-[10px] font-medium",
                result.status === "allocated" && "bg-green-500/10 text-green-600",
                result.status === "reserved" && "bg-yellow-500/10 text-yellow-600",
                result.status === "orphan" && "bg-red-500/10 text-red-600"
              )}
            >
              {result.status}
            </span>
          )}
        </div>
      </div>
    </button>
  );
}

export function GlobalSearch() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const debouncedQuery = useDebounce(query.trim(), 250);

  const { data, isFetching } = useQuery({
    queryKey: ["search", debouncedQuery],
    queryFn: () => searchApi.search(debouncedQuery, undefined, 20),
    enabled: debouncedQuery.length >= 1,
    staleTime: 10_000,
  });

  const results = data?.results ?? [];

  // Reset active index when results change
  useEffect(() => {
    setActiveIdx(0);
  }, [debouncedQuery]);

  // Cmd+K / Ctrl+K to open
  useEffect(() => {
    function handler(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50);
    } else {
      setQuery("");
    }
  }, [open]);

  const handleSelect = useCallback(
    (result: SearchResult) => {
      setOpen(false);
      if (result.type === "ip_address") {
        navigate("/ipam", {
          state: { selectSubnet: result.subnet_id, highlightAddress: result.id },
        });
      } else if (result.type === "subnet") {
        navigate("/ipam", { state: { selectSubnet: result.id } });
      } else if (result.type === "block") {
        navigate("/ipam", { state: { selectBlock: result.id } });
      } else if (result.type === "space") {
        navigate("/ipam", { state: { selectSpace: result.id } });
      }
    },
    [navigate]
  );

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && results[activeIdx]) {
      handleSelect(results[activeIdx]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <>
      {/* Trigger button */}
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-md border border-border/50 bg-muted/30 px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
      >
        <Search className="h-3.5 w-3.5" />
        <span>Search…</span>
        <kbd className="ml-1 hidden rounded bg-muted px-1 py-0.5 text-[10px] font-mono sm:inline-block">
          ⌘K
        </kbd>
      </button>

      {/* Modal overlay */}
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]"
          onClick={() => setOpen(false)}
        >
          {/* Backdrop */}
          <div className="absolute inset-0 bg-black/50" />

          {/* Dialog */}
          <div
            className="relative z-10 w-full max-w-xl rounded-xl border bg-card shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Input */}
            <div className="flex items-center gap-3 border-b px-4 py-3">
              <Search className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Search IP, hostname, MAC, subnet, CIDR…"
                className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              />
              {query && (
                <button onClick={() => setQuery("")} className="text-muted-foreground hover:text-foreground">
                  <X className="h-4 w-4" />
                </button>
              )}
              <kbd className="rounded border bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
                ESC
              </kbd>
            </div>

            {/* Results */}
            <div className="max-h-96 overflow-y-auto">
              {!query && (
                <p className="px-4 py-6 text-center text-sm text-muted-foreground">
                  Type to search across IP addresses, subnets, blocks, and spaces.
                </p>
              )}
              {query && isFetching && results.length === 0 && (
                <p className="px-4 py-6 text-center text-sm text-muted-foreground">Searching…</p>
              )}
              {query && !isFetching && results.length === 0 && debouncedQuery.length > 0 && (
                <p className="px-4 py-6 text-center text-sm text-muted-foreground">
                  No results for <span className="font-mono font-medium">"{debouncedQuery}"</span>
                </p>
              )}
              {results.map((r, i) => (
                <ResultRow
                  key={`${r.type}:${r.id}`}
                  result={r}
                  isActive={i === activeIdx}
                  onSelect={handleSelect}
                />
              ))}
            </div>

            {results.length > 0 && (
              <div className="flex items-center justify-between border-t px-4 py-2 text-xs text-muted-foreground">
                <span>{data?.total ?? 0} result{(data?.total ?? 0) !== 1 ? "s" : ""}</span>
                <span className="flex items-center gap-2">
                  <span>↑↓ navigate</span>
                  <span>↵ select</span>
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
