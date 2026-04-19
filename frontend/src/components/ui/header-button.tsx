import { forwardRef, type ReactNode, type ButtonHTMLAttributes } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

// Standard action button used in the top-of-panel toolbar on every
// detail page (IPAM, DNS, DHCP). Before this, each page drifted in
// size (``text-xs`` vs ``text-sm``), gap (``gap-1`` vs ``gap-1.5``),
// and variant styling. This component locks those in.
//
// Standard order on a detail page is:
//   [Refresh] [Sync …] [Import] [Export] [misc reads] …
//   [Edit] [Resize] [Delete] …
//   [+ Primary action]
//
// Primary creates live at the far right. Destructive ("Delete") sits
// right before the primary action, separated visually only by spacing.
// Matching the order conventions documented in the page header comments.

type Variant = "secondary" | "primary" | "destructive";

const VARIANT_CLS: Record<Variant, string> = {
  secondary: "border hover:bg-muted",
  primary: "bg-primary text-primary-foreground hover:bg-primary/90 font-medium",
  destructive:
    "border border-destructive/40 text-destructive hover:bg-destructive/10",
};

const BASE_CLS =
  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed";

type HeaderButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  icon?: LucideIcon;
  /** Extra classes for the icon (spin state, color overrides). */
  iconClassName?: string;
  children?: ReactNode;
};

export const HeaderButton = forwardRef<HTMLButtonElement, HeaderButtonProps>(
  function HeaderButton(
    {
      variant = "secondary",
      icon: Icon,
      iconClassName,
      children,
      className,
      ...rest
    },
    ref,
  ) {
    return (
      <button
        ref={ref}
        type="button"
        className={cn(BASE_CLS, VARIANT_CLS[variant], className)}
        {...rest}
      >
        {Icon && <Icon className={cn("h-3.5 w-3.5", iconClassName)} />}
        {children}
      </button>
    );
  },
);
