import { Workflow } from "lucide-react";

/**
 * Stub page for the upcoming VRFs surface. Tracking issue:
 * https://github.com/spatiumddi/spatiumddi/issues/86
 *
 * Lives under the Network sidebar section alongside Devices / VLANs /
 * ASNs. Rendered as a centred "Coming soon" panel until the data
 * model + CRUD ships.
 */
export function VRFsStubPage() {
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="border-b bg-card px-6 py-4">
        <div className="flex items-center gap-2">
          <Workflow className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">VRFs</h1>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-muted-foreground">
          Virtual Routing and Forwarding instances — first-class entities for
          overlapping address spaces.
        </p>
      </div>
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="max-w-md rounded-lg border border-dashed bg-card px-6 py-8 text-center">
          <Workflow className="mx-auto mb-3 h-8 w-8 text-muted-foreground" />
          <p className="text-sm font-medium">Coming soon</p>
          <p className="mt-2 text-xs text-muted-foreground">
            See{" "}
            <a
              href="https://github.com/spatiumddi/spatiumddi/issues/86"
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-primary hover:underline"
            >
              issue #86
            </a>{" "}
            for design and progress.
          </p>
        </div>
      </div>
    </div>
  );
}
