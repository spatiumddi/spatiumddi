// Shared page-navigation control for server-side paginated tables (#455).
// Mirrors the local Pager the network device-detail tabs use; lifted into a
// shared primitive so the DNS records + DHCP lease tables (and future
// paginated lists) render identical Prev / "Page X of Y" / Next controls.
//
// Renders nothing when there's only a single page so it stays out of the way
// on small result sets.

export function Pager({
  page,
  total,
  pageSize,
  onChange,
}: {
  page: number;
  total: number;
  pageSize: number;
  onChange: (p: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (totalPages <= 1) return null;
  return (
    <div className="flex items-center justify-end gap-2 text-xs">
      <button
        type="button"
        onClick={() => onChange(Math.max(1, page - 1))}
        disabled={page <= 1}
        className="rounded-md border px-2 py-1 hover:bg-muted disabled:opacity-40"
      >
        Prev
      </button>
      <span className="text-muted-foreground">
        Page {page} of {totalPages}
      </span>
      <button
        type="button"
        onClick={() => onChange(Math.min(totalPages, page + 1))}
        disabled={page >= totalPages}
        className="rounded-md border px-2 py-1 hover:bg-muted disabled:opacity-40"
      >
        Next
      </button>
    </div>
  );
}
