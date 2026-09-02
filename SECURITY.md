# Security Policy

## Supported version

Security fixes are expected to target the latest public branch.

## Reporting

Do not open a public issue containing credentials, private manuscript text, personal data, or an exploitable proof of concept. Contact the repository owner privately and include the affected version, impact, and minimal reproduction steps.

## Secret handling

- Windows secrets are stored under `runtime/` using current-user DPAPI.
- Linux and macOS secrets must be supplied through environment variables.
- `config.json`, `runtime/`, logs, databases, generated chapters, and archives must remain untracked.
- A credential that has entered Git history must be revoked and rotated even after the file is deleted.

## Deployment scope

NovelAgent is designed as a local authoring tool. It listens on `127.0.0.1` by default. Internet exposure requires an independently secured reverse proxy, HTTPS, network filtering, backups, and operational monitoring.
