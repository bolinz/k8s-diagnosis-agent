# Contributing

## Development Setup

```bash
cd k8s-diagnosis-agent
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
```

## Branching

- `main` is the stable branch
- use `feat/<topic>` for new work
- use `fix/<topic>` for bug fixes

## Pull Requests

- keep changes scoped
- include or update tests
- call out any RBAC, CRD, or model-behavior changes in the PR description
- document deployment or release impact if the change affects manifests, image tags, or ingress behavior

## Testing

Run both checks before opening a PR:

```bash
python3 -m pytest -q
PYTHONPYCACHEPREFIX=/tmp/pythoncache python3 -m compileall agent tests
```

## Change Types Requiring Extra Care

- CRD schema changes
- RBAC scope changes
- network exposure changes such as Service, Ingress, or NetworkPolicy edits
- OpenAI model behavior changes
- release workflow or image-tagging changes

## Release

- merge to `main`
- tag with `v*`
- GitHub Actions builds and publishes the container image
- GitHub Release notes should include GHCR image references and deployment guidance
