import { useAuth } from "@/hooks/useAuth";
import { useNavigate } from "react-router-dom";
import { LogOut } from "lucide-react";

export function Header() {
  const { logout } = useAuth();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <header className="flex h-14 items-center justify-end border-b bg-card px-6">
      <button
        onClick={handleLogout}
        className="flex items-center gap-2 rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground"
      >
        <LogOut className="h-4 w-4" />
        Sign out
      </button>
    </header>
  );
}
