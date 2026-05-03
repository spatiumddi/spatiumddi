import { Hash } from "lucide-react";

/**
 * Stub page for the upcoming ASNs surface. Tracking issue:
 * https://github.com/spatiumddi/spatiumddi/issues/85
 *
 * Lives under the Network sidebar section alongside Devices / VLANs /
 * VRFs. Rendered as a centred "Coming soon" panel until the data
 * model + CRUD ships.
 */
export function ASNsStubPage() {
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="border-b bg-card px-6 py-4">
        <div className="flex items-center gap-2">
          <Hash className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">ASNs</h1>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-muted-foreground">
          Autonomous System Number management — track public + private ASNs
          alongside the prefixes they originate.
        </p>
      </div>
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="max-w-md rounded-lg border border-dashed bg-card px-6 py-8 text-center">
          <Hash className="mx-auto mb-3 h-8 w-8 text-muted-foreground" />
          <p className="text-sm font-medium">Coming soon</p>
          <p className="mt-2 text-xs text-muted-foreground">
            See{" "}
            <a
              href="https://github.com/spatiumddi/spatiumddi/issues/85"
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-primary hover:underline"
            >
              issue #85
            </a>{" "}
            for design and progress.
          </p>
        </div>
      </div>
    </div>
  );
}
