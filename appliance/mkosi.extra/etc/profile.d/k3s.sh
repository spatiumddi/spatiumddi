# Issue #183 Phase 1 — KUBECONFIG export for interactive shells.
#
# k3s writes its admin kubeconfig to /etc/rancher/k3s/k3s.yaml on
# first start. Setting KUBECONFIG in /etc/profile.d/ so admin / root
# login shells can ``kubectl get nodes`` immediately without any
# explicit flag.
#
# Only export when the file actually exists — on a fresh appliance
# before k3s.service has been enabled, the kubeconfig isn't there
# yet, and pointing KUBECONFIG at a non-existent path would surface
# as a confusing "stat: ..." error from kubectl's first run.
if [ -r /etc/rancher/k3s/k3s.yaml ]; then
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
fi
