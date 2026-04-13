# Contributing to SpatiumDDI

Thank you for your interest in contributing! SpatiumDDI is an open project and we welcome contributions of all kinds — code, documentation, bug reports, and feature ideas.

## Before You Start

- Read the [CLAUDE.md](CLAUDE.md) file — it is the canonical spec for the project and defines all architectural decisions
- Check [open issues](https://github.com/spatiumddi/spatiumddi/issues) to avoid duplicate work
- For significant changes, open a discussion or issue first so we can align before you invest time coding

## Development Setup

```bash
git clone https://github.com/spatiumddi/spatiumddi.git
cd spatiumddi
cp .env.example .env
docker compose up -d
```

## Code Standards

- Python: `ruff`, `black`, `mypy` — all enforced in CI
- TypeScript: `eslint`, `prettier` — all enforced in CI
- All new API endpoints need tests: success, unauthorized, and validation error cases
- All mutations must write to the audit log before returning a response

## Pull Request Process

1. Fork the repo and create a branch from `main`
2. Make your changes, including tests
3. Run `make lint` and `make test` locally before pushing
4. Open a PR with a clear description of what changed and why
5. Link any related issues

## Reporting Bugs

Use the [GitHub Issues](https://github.com/spatiumddi/spatiumddi/issues) tracker. Include:
- SpatiumDDI version / commit hash
- Deployment method (Docker, Kubernetes, bare metal)
- Steps to reproduce
- Expected vs. actual behaviour

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
