// Shared page-navigation control for server-side paginated tables (#455).
// Renders Prev / numbered page "breadcrumbs" (with … ellipsis for large page
// counts) / Next, so operators can jump straight to a page instead of clicking
// Next repeatedly. Used at both the top and bottom of paginated tables.
//
// Renders nothing when there's only a single page so it stays out of the way
// on small result sets.

import { cn } from "@/lib/utils";

// Windowed list of page numbers around the current page: always the first and
// last page, plus current ±1, with "ellipsis" markers collapsing the gaps.
// e.g. page 5 of 47 → [1, "ellipsis", 4, 5, 6, "ellipsis", 47].
function pageWindow(
  current: number,
  totalPages: number,
): (number | "ellipsis")[] {
  const out: (number | "ellipsis")[] = [];
  for (let p = 1; p <= totalPages; p++) {
    if (p === 1 || p === totalPages || Math.abs(p - current) <= 1) {
      out.push(p);
    } else if (out[out.length - 1] !== "ellipsis") {
      out.push("ellipsis");
    }
  }
  return out;
}

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
  const btn =
    "rounded-md border px-2 py-1 hover:bg-muted disabled:opacity-40 disabled:hover:bg-transparent";
  return (
    <div className="flex items-center gap-1 text-xs">
      <button
        type="button"
        onClick={() => onChange(Math.max(1, page - 1))}
        disabled={page <= 1}
        className={btn}
      >
        Prev
      </button>
      {pageWindow(page, totalPages).map((p, i) =>
        p === "ellipsis" ? (
          <span key={`e${i}`} className="px-1 text-muted-foreground">
            …
          </span>
        ) : (
          <button
            key={p}
            type="button"
            onClick={() => onChange(p)}
            aria-current={p === page ? "page" : undefined}
            className={cn(
              btn,
              "min-w-[2rem] tabular-nums",
              p === page &&
                "border-primary bg-primary font-semibold text-primary-foreground hover:bg-primary",
            )}
          >
            {p}
          </button>
        ),
      )}
      <button
        type="button"
        onClick={() => onChange(Math.min(totalPages, page + 1))}
        disabled={page >= totalPages}
        className={btn}
      >
        Next
      </button>
    </div>
  );
}
