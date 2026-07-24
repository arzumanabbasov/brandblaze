# Contributing to BrandBlaze

Thank you for helping improve BrandBlaze.

## Before you start

- Search existing issues before opening a new one.
- Use an issue to discuss large features or architectural changes first.
- Never include API keys, presigned URLs, customer assets, or generated media
  you do not have permission to share.
- Provider-backed tests may consume credits. Keep automated tests offline and
  mocked unless a maintainer explicitly approves a live integration test.

## Local setup

Follow the installation and configuration instructions in `README.md`. Copy
`.env.example` to `.env`; never edit the example file with real credentials.

## Development workflow

1. Create a focused branch.
2. Make the smallest coherent change.
3. Add or update tests.
4. Run the complete local quality gate:

   ```powershell
   python -m unittest discover -s api/tests -v
   npm.cmd run lint
   npm.cmd test
   ```

5. Confirm `git status` contains no secrets, runtime files, or generated assets.
6. Open a pull request explaining the behavior change and how it was tested.

## Pull request expectations

- Keep UI status and provider claims truthful.
- Do not replace real results with demo placeholders.
- Preserve the Genblaze and Backblaze B2 provenance path.
- Validate user-controlled input before starting paid provider work.
- Treat private B2 URLs and provider error responses as sensitive.
- Document new environment variables in both `.env.example` and `README.md`.

## Reporting bugs

Include reproduction steps, expected behavior, actual behavior, operating
system, relevant package versions, and sanitized logs. Remove credentials,
presigned query strings, bucket names, and private asset URLs.
