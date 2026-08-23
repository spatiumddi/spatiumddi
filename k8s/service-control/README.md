# Service control (opt-in) — issue #890

Lets a superadmin restart SpatiumDDI's own workloads from
**Settings → Services** in the web UI, as a Kubernetes rollout restart
(a `kubectl.kubernetes.io/restartedAt` bump on the pod template).

**Off by default, in two independent halves**, because they fail
differently and both failures are worth naming:

| Half | What it does | Symptom when missing |
|---|---|---|
| `SERVICE_CONTROL_ENABLED=true` on the api | flips the capability the UI reads | Services screen renders read-only and says exactly this |
| the `Role` + `RoleBinding` here | lets the api list + patch workloads | kubeapi 403 → the API reports "enable the service-control RBAC", not an empty list |

Apply both, or the screen tells you which one you skipped.

```bash
kubectl apply -f k8s/service-control/rbac.yaml
kubectl -n spatiumddi patch deployment api \
  --type=strategic --patch-file k8s/service-control/api-patch.yaml
kubectl -n spatiumddi rollout status deployment/api
```

## Why the Role has no `resourceNames`

Workload names carry the release/deployment name, and a `resourceNames`
list cannot express a prefix — it would need regenerating whenever a
workload is added, and would silently omit the new one. The narrowing
that matters is enforced in the API instead: `list_workloads()` returns
only objects labelled `app.kubernetes.io/name=spatiumddi` (or
`app.kubernetes.io/part-of=spatiumddi`), and an action resolves its
target against that listing — so an operator's own Deployment in this
namespace is neither listed nor restartable.

`start` / `stop` are not offered on Kubernetes. They would mean scaling
a workload to zero and back, and restoring the previous replica count
needs somewhere durable to remember it; a control plane that forgets how
many replicas a workload had is worse than one that never offered.

## Removing it

```bash
kubectl delete -f k8s/service-control/rbac.yaml
kubectl -n spatiumddi set env deployment/api SERVICE_CONTROL_ENABLED-
```

The Helm equivalents are `api.serviceControl.enabled` and
`api.serviceControlRBAC.enabled` (both default `false`); appliance
installs get the RBAC half from `spatiumddi-firstboot` and the gate from
`appliance_mode`.
