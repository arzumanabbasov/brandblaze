# Security Policy

## Supported versions

BrandBlaze is currently pre-1.0. Security fixes are applied to the latest
revision of the default branch.

## Reporting a vulnerability

Do not publish security vulnerabilities, credentials, private object URLs, or
customer media in a public issue.

Before the repository becomes public, report vulnerabilities directly to the
repository owner through GitHub. Once GitHub private vulnerability reporting is
enabled, use the repository's **Security > Report a vulnerability** workflow.

Please include:

- the affected component and revision;
- reproduction steps or a minimal proof of concept;
- potential impact;
- suggested remediation, if known.

## Secrets and media

- Store credentials only in ignored `.env` files or a secret manager.
- Use restricted Backblaze application keys with the minimum required bucket
  access.
- Do not commit presigned URLs; their query strings contain temporary access
  credentials.
- Treat uploaded and generated product imagery as potentially confidential.
- Rotate any credential immediately if it appears in a commit, log, screenshot,
  issue, or pull request.

## Deployment note

The development server is not a hardened public deployment. A production
deployment should add authentication, durable job workers, rate limits, request
size enforcement at the edge, centralized secret management, HTTPS, monitoring,
and a retention policy for source and generated media.
