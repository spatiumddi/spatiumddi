import { useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { Header } from "./Header";
import { DemoBanner } from "./DemoBanner";
import { SetupBanner } from "./SetupBanner";
import { CopilotButton } from "@/components/copilot/CopilotButton";
import { useFeatureModules } from "@/hooks/useFeatureModules";

export function AppLayout() {
  // Mobile drawer state. On desktop (md+) the sidebar is always visible and
  // this flag is ignored by the Sidebar's responsive classes.
  const [mobileOpen, setMobileOpen] = useState(false);

  // ai.copilot feature-module gate. Disabled hides the floating
  // button + chat drawer entirely; the /ai/* REST endpoints 404 from
  // the same gate on the backend.
  const { enabled } = useFeatureModules();
  const copilotEnabled = enabled("ai.copilot");

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <Sidebar
        mobileOpen={mobileOpen}
        onMobileClose={() => setMobileOpen(false)}
      />
      <div className="flex flex-1 flex-col overflow-hidden">
        <DemoBanner />
        <SetupBanner />
        <Header onMobileMenu={() => setMobileOpen(true)} />
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
      {/* Issue #90 — Operator Copilot floating button. Hidden when no
          AI provider is enabled, opens the chat drawer otherwise. */}
      {copilotEnabled && <CopilotButton />}
    </div>
  );
}
