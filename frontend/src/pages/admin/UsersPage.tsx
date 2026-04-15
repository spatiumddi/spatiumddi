import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  Pencil,
  Trash2,
  KeyRound,
  X,
  ShieldCheck,
  ShieldOff,
} from "lucide-react";
import { usersApi, type AppUser } from "@/lib/api";
import { cn } from "@/lib/utils";

// ── Helpers ───────────────────────────────────────────────────────────────────

const inputCls =
  "w-full rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
    </div>
  );
}

function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ── Create User Modal ─────────────────────────────────────────────────────────

function CreateUserModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [isSuperadmin, setIsSuperadmin] = useState(false);
  const [forceChange, setForceChange] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      usersApi.create({
        username,
        email,
        display_name: displayName,
        password,
        is_superadmin: isSuperadmin,
        force_password_change: forceChange,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create user";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title="New User" onClose={onClose}>
      <div className="space-y-3">
        <Field label="Username">
          <input
            className={inputCls}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
        </Field>
        <Field label="Display Name">
          <input
            className={inputCls}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </Field>
        <Field label="Email">
          <input
            className={inputCls}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password">
          <input
            className={inputCls}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <div className="flex flex-col gap-2">
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={isSuperadmin}
              onChange={(e) => setIsSuperadmin(e.target.checked)}
            />
            Superadmin
          </label>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={forceChange}
              onChange={(e) => setForceChange(e.target.checked)}
            />
            Require password change on first login
          </label>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={!username || !email || !password || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Edit User Modal ───────────────────────────────────────────────────────────

function EditUserModal({
  user,
  onClose,
}: {
  user: AppUser;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [displayName, setDisplayName] = useState(user.display_name);
  const [email, setEmail] = useState(user.email);
  const [isSuperadmin, setIsSuperadmin] = useState(user.is_superadmin);
  const [isActive, setIsActive] = useState(user.is_active);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      usersApi.update(user.id, {
        display_name: displayName,
        email,
        is_superadmin: isSuperadmin,
        is_active: isActive,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to update";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  return (
    <Modal title={`Edit ${user.username}`} onClose={onClose}>
      <div className="space-y-3">
        <Field label="Display Name">
          <input
            className={inputCls}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            autoFocus
          />
        </Field>
        <Field label="Email">
          <input
            className={inputCls}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <div className="flex flex-col gap-2">
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={isSuperadmin}
              onChange={(e) => setIsSuperadmin(e.target.checked)}
            />
            Superadmin
          </label>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={isActive}
              onChange={(e) => setIsActive(e.target.checked)}
            />
            Active
          </label>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Reset Password Modal ──────────────────────────────────────────────────────

function ResetPasswordModal({
  user,
  onClose,
}: {
  user: AppUser;
  onClose: () => void;
}) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => usersApi.resetPassword(user.id, password),
    onSuccess: onClose,
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to reset password";
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    },
  });

  const mismatch = confirm.length > 0 && password !== confirm;

  return (
    <Modal title={`Reset password — ${user.username}`} onClose={onClose}>
      <div className="space-y-3">
        <Field label="New Password">
          <input
            className={inputCls}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
          />
        </Field>
        <Field label="Confirm Password">
          <input
            className={cn(inputCls, mismatch && "border-destructive")}
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
          {mismatch && (
            <p className="text-xs text-destructive">Passwords do not match</p>
          )}
        </Field>
        <p className="text-xs text-muted-foreground">
          The user will be required to change their password on next login.
        </p>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              setError(null);
              mutation.mutate();
            }}
            disabled={!password || mismatch || mutation.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Resetting…" : "Reset Password"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Delete Confirm Modal ──────────────────────────────────────────────────────

function DeleteUserModal({
  user,
  onClose,
}: {
  user: AppUser;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => usersApi.delete(user.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
  });

  return (
    <Modal title="Delete User" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Delete user{" "}
          <strong className="text-foreground">{user.username}</strong>? This
          cannot be undone.
        </p>
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
            className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {mutation.isPending ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export function UsersPage() {
  const [showCreate, setShowCreate] = useState(false);
  const [editUser, setEditUser] = useState<AppUser | null>(null);
  const [resetUser, setResetUser] = useState<AppUser | null>(null);
  const [deleteUser, setDeleteUser] = useState<AppUser | null>(null);

  const { data: users, isLoading } = useQuery({
    queryKey: ["users"],
    queryFn: usersApi.list,
  });

  return (
    <div className="h-full overflow-auto p-6">
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold tracking-tight">Users</h1>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90"
          >
            <Plus className="h-3.5 w-3.5" />
            New User
          </button>
        </div>

        <div className="rounded-lg border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-xs">
                <th className="px-4 py-3 text-left font-medium">Username</th>
                <th className="px-4 py-3 text-left font-medium">
                  Display Name
                </th>
                <th className="px-4 py-3 text-left font-medium">Email</th>
                <th className="px-4 py-3 text-left font-medium">Source</th>
                <th className="px-4 py-3 text-left font-medium">Role</th>
                <th className="px-4 py-3 text-left font-medium">Status</th>
                <th className="px-4 py-3 text-left font-medium">Last Login</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {isLoading && (
                <tr>
                  <td
                    colSpan={8}
                    className="px-4 py-6 text-center text-muted-foreground"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {users?.map((user) => (
                <tr
                  key={user.id}
                  className="border-b last:border-0 hover:bg-muted/20"
                >
                  <td className="px-4 py-3 font-mono font-medium">
                    {user.username}
                  </td>
                  <td className="px-4 py-3">{user.display_name}</td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {user.email}
                  </td>
                  <td className="px-4 py-3">
                    <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                      {user.auth_source}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {user.is_superadmin ? (
                      <span className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
                        <ShieldCheck className="h-3.5 w-3.5" /> superadmin
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-xs text-muted-foreground">
                        <ShieldOff className="h-3.5 w-3.5" /> user
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={cn(
                        "rounded-full px-2 py-0.5 text-xs font-medium",
                        user.is_active
                          ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
                          : "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
                      )}
                    >
                      {user.is_active ? "active" : "disabled"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">
                    {user.last_login_at ? (
                      new Date(user.last_login_at).toLocaleString()
                    ) : (
                      <span className="text-muted-foreground/40">never</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => setEditUser(user)}
                        className="rounded p-1 text-muted-foreground hover:text-foreground"
                        title="Edit"
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => setResetUser(user)}
                        className="rounded p-1 text-muted-foreground hover:text-foreground"
                        title="Reset password"
                      >
                        <KeyRound className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => setDeleteUser(user)}
                        className="rounded p-1 text-muted-foreground hover:text-destructive"
                        title="Delete"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {showCreate && <CreateUserModal onClose={() => setShowCreate(false)} />}
      {editUser && (
        <EditUserModal user={editUser} onClose={() => setEditUser(null)} />
      )}
      {resetUser && (
        <ResetPasswordModal
          user={resetUser}
          onClose={() => setResetUser(null)}
        />
      )}
      {deleteUser && (
        <DeleteUserModal
          user={deleteUser}
          onClose={() => setDeleteUser(null)}
        />
      )}
    </div>
  );
}
