import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { cn } from "@/lib/utils";

export type SortDir = "asc" | "desc";

export type SortState<K extends string> = {
  key: K | null;
  dir: SortDir;
};

type Getter<T, K extends string> = (row: T, key: K) => unknown;

const defaultGet = <T, K extends string>(row: T, key: K): unknown =>
  (row as Record<string, unknown>)[key];

// Case-insensitive, null-last comparator that handles strings, numbers,
// booleans, and Date/ISO-string timestamps. Stable across renders because
// it's pure.
function compareValues(a: unknown, b: unknown): number {
  const aNull = a === null || a === undefined || a === "";
  const bNull = b === null || b === undefined || b === "";
  if (aNull && bNull) return 0;
  if (aNull) return 1; // nulls sort last (flipped on desc below)
  if (bNull) return -1;

  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean")
    return a === b ? 0 : a ? 1 : -1;

  // Try numeric compare when both are numeric strings (IP-ish content still
  // falls through to string compare; address tables pre-sort server-side).
  const na = Number(a);
  const nb = Number(b);
  if (!Number.isNaN(na) && !Number.isNaN(nb) && a !== "" && b !== "") {
    if (String(na) === String(a) && String(nb) === String(b)) return na - nb;
  }

  const sa = String(a).toLowerCase();
  const sb = String(b).toLowerCase();
  if (sa < sb) return -1;
  if (sa > sb) return 1;
  return 0;
}

/**
 * Click-to-sort state for tables. Returns the sorted array, current sort
 * state, and a toggle function to bind to column headers.
 *
 *   const { sorted, sort, toggle } = useTableSort(rows, { key: "name", dir: "asc" });
 *
 * For columns that don't map 1:1 to a field, pass `getValue` to compute the
 * sort key dynamically:
 *
 *   useTableSort(rows, null, (row, key) =>
 *     key === "pool" ? ipPoolInfo(row)?.name ?? "" : row[key],
 *   );
 *
 * Click behaviour: first click → asc, second → desc, third → unsorted (null).
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useTableSort<T, K extends string = string>(
  data: T[] | undefined | null,
  initial: SortState<K> | null = null,
  getValue: Getter<T, K> = defaultGet,
) {
  const [sort, setSort] = useState<SortState<K>>(
    initial ?? { key: null, dir: "asc" },
  );

  const toggle = (key: K) => {
    setSort((prev) => {
      if (prev.key !== key) return { key, dir: "asc" };
      if (prev.dir === "asc") return { key, dir: "desc" };
      return { key: null, dir: "asc" };
    });
  };

  const sorted = useMemo(() => {
    const rows = data ?? [];
    if (!sort.key) return rows;
    const key = sort.key;
    const copy = [...rows];
    copy.sort((a, b) => {
      const cmp = compareValues(getValue(a, key), getValue(b, key));
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [data, sort, getValue]);

  return { sorted, sort, toggle };
}

/**
 * Click-to-sort table header cell. Renders a button that toggles sort on
 * click and shows an up/down/neutral arrow icon.
 *
 *   <SortableTh sortKey="name" sort={sort} onSort={toggle}>
 *     Name
 *   </SortableTh>
 */
export function SortableTh<K extends string>({
  sortKey,
  sort,
  onSort,
  children,
  className,
  align = "left",
}: {
  sortKey: K;
  sort: SortState<K>;
  onSort: (key: K) => void;
  children: React.ReactNode;
  className?: string;
  align?: "left" | "right" | "center";
}) {
  const active = sort.key === sortKey;
  const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <th
      className={cn(
        "px-4 py-2 font-medium",
        align === "right"
          ? "text-right"
          : align === "center"
            ? "text-center"
            : "text-left",
        className,
      )}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          "group/sort inline-flex items-center gap-1 rounded hover:text-foreground",
          active ? "text-foreground" : "text-muted-foreground",
        )}
      >
        <span>{children}</span>
        <Icon
          className={cn(
            "h-3 w-3 transition-opacity",
            active ? "opacity-100" : "opacity-30 group-hover/sort:opacity-60",
          )}
        />
      </button>
    </th>
  );
}
