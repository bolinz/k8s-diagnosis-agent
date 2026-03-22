# Security Policy

## Reporting

Do not post cluster credentials, API tokens, kubeconfigs, or sensitive incident data in public issues.

If you believe you found a security issue in this project, open a GitHub issue with minimal reproduction details and explicitly redact secrets and internal hostnames. If the issue cannot be described safely in public, contact the maintainer before sharing details.

## Scope

This project is a Kubernetes diagnostics tool and may process:

- workload names
- namespaces
- event messages
- container logs
- deployment metadata

Review your deployment configuration before enabling it in production environments with sensitive workloads.

## Response Expectations

This project does not currently provide a formal vulnerability response SLA. Security issues will be triaged in the repository and fixed through normal pull request and release workflows.
