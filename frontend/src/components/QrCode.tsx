import { useMemo } from "react";

import { buildQrMatrix, type QrLevel } from "@/lib/qr";

/**
 * QR code rendered as inline SVG (issue #906).
 *
 * SVG rather than a canvas or a data-URI `<img>`: it stays sharp at any size
 * and on any DPI, which matters because the thing reading it is a handheld
 * camera at an arbitrary distance and angle. It also needs no `img-src data:`
 * in the CSP.
 *
 * The matrix construction lives in `@/lib/qr` so it can be tested.
 */

export interface QrCodeProps {
  value: string;
  /** Rendered edge length in px. The matrix scales to fit. */
  size?: number;
  /**
   * Error-correction level. `M` (~15% recoverable) is the default because
   * these are read off a screen rather than a scuffed sticker, and a higher
   * level costs modules — which makes an already-long enrolment URI denser
   * and *harder* to scan, the opposite of the intent.
   */
  level?: QrLevel;
  className?: string;
  title?: string;
}

export function QrCode({
  value,
  size = 224,
  level = "M",
  className,
  title = "QR code",
}: QrCodeProps) {
  const matrix = useMemo(() => buildQrMatrix(value, level), [value, level]);

  return (
    <svg
      // The viewBox is in MODULE units with the quiet zone inside it, so the
      // margin scales with the code rather than being a CSS afterthought a
      // parent's padding could eat.
      viewBox={`0 0 ${matrix.extent} ${matrix.extent}`}
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={title}
      shapeRendering="crispEdges"
    >
      <title>{title}</title>
      {/* Explicit white ground. A QR code inheriting a dark-mode background
          is unscannable, so these render light-on-white whatever theme the
          operator is using. */}
      <rect width={matrix.extent} height={matrix.extent} fill="#ffffff" />
      <path d={matrix.d} fill="#000000" />
    </svg>
  );
}
