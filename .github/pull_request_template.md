<!--
Thanks for contributing to SpatiumDDI!

Title format: `<type>(<scope>): <short summary>`
  types: feat, fix, docs, refactor, perf, test, build, ci, chore
  scope: ipam, dns, dhcp, auth, rbac, audit, ui, api, k8s, compose, agent-dns, agent-dhcp

See CLAUDE.md + docs/DEVELOPMENT.md for conventions.
-->

## Summary

<!-- What does this PR change and why? -->

## Area

<!-- Check all that apply -->

- [ ] IPAM
- [ ] DNS
- [ ] DHCP
- [ ] Auth / Providers
- [ ] Permissions / RBAC
- [ ] Audit / Observability
- [ ] Frontend / UI
- [ ] API
- [ ] Deployment (Compose / K8s / Appliance)
- [ ] Docs

## Screenshots / API examples

<!-- UI changes: before/after screenshots. API changes: example request + response. -->

## Test plan

<!-- What did you run to verify this works? -->

- [ ] `make lint` passes
- [ ] `make test` passes
- [ ] Manually verified in the UI / via curl (describe below)
- [ ] New/changed behavior is covered by a test

## Migration / deployment notes

- [ ] Includes an Alembic migration (if DB models changed)
- [ ] Updated `k8s/base/` manifests + `k8s/README.md` (if services/env changed)
- [ ] Breaking change — users must take action to upgrade

## Related issues

<!-- "Closes #123", "Refs #456" -->
