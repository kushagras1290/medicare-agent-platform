# Release Gates

Production startup is fail-closed. It requires OIDC reviewer authentication, MFA and scope claims, HTTPS enforcement, explicit hosts, PostgreSQL, file-mounted secrets, and a SHA-256-pinned approval manifest.

Copy `config/release-approvals.example.json` outside the repository, fill it with real change records, and have the named owners approve their areas. Required gates are:

- clinical policy
- privacy and legal review
- security review
- identity and access
- backup and restore
- monitoring and alert routing
- incident response
- consent governance
- provider contracts

Every entry needs an owner, approval ID, timestamp and evidence URI. Pending, expired, duplicated, missing, future-dated or digest-mismatched evidence blocks startup.

Validate before deployment:

```powershell
$digest = (Get-FileHash -Algorithm SHA256 approvals.json).Hash.ToLowerInvariant()
uv run medicare release-gate --manifest approvals.json --environment production --sha256 $digest
```

Set the manifest path and digest through `RELEASE_APPROVALS_PATH` and `RELEASE_APPROVALS_SHA256`. Approval files should live in the organization’s controlled change-management store, not this repository.

The gate validates evidence structure and integrity. It does not replace clinical, legal, security or procurement judgment.
