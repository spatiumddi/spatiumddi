import { cn, SWATCH_COLORS } from "@/lib/utils";

/**
 * Curated color picker used for DNS zones and IP spaces. Free-form hex is
 * deliberately not supported — every choice must stay legible on both
 * light and dark themes.
 *
 *   <SwatchPicker value={color} onChange={setColor} />
 */
export function SwatchPicker({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (next: string | null) => void;
}) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap mt-1">
      <button
        type="button"
        onClick={() => onChange(null)}
        title="No color"
        className={cn(
          "h-5 w-5 rounded-full border border-border bg-transparent flex items-center justify-center",
          value === null && "ring-2 ring-ring ring-offset-1",
        )}
      >
        <span className="block h-0.5 w-3 bg-muted-foreground/60 rotate-45" />
      </button>
      {SWATCH_COLORS.map((c) => (
        <button
          key={c.key}
          type="button"
          onClick={() => onChange(c.key)}
          title={c.label}
          className={cn(
            "h-5 w-5 rounded-full border border-transparent",
            c.cls,
            value === c.key && "ring-2 ring-ring ring-offset-1",
          )}
        />
      ))}
    </div>
  );
}
