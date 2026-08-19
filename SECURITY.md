# Security Policy

## Reporting a vulnerability

Please do not disclose vulnerabilities, credentials, private addresses or other
sensitive data in a public issue.

Use GitHub's **Security** tab and select **Report a vulnerability** when private
vulnerability reporting is available. Otherwise, open an issue containing no
sensitive details and ask the maintainer for a private contact channel.

Include the affected version or commit, the impact, reproduction steps and any
suggested mitigation. Reports will be acknowledged as soon as practical.

## Supported versions

Until the first stable release, security fixes are made on the `main` branch.

## Leaked credentials

If a real credential reaches a commit, rotate or revoke it first. Removing the
value from a later commit or rewriting Git history does not invalidate a copy
that may already have been fetched.
