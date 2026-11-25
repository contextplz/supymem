# Security policy

Supymem runs on your computer. Your workspace stays local, but a model, coding tool, or other service you use can receive data needed for the task. Keep the local browser interface private to your computer. Add keys in **Settings > Keys and connections**. The app encrypts saved keys. A `.env` file contains plaintext until you remove its secret entries. Never share or commit it.

See [backups and security](docs/self-hosting/backups-security.md) before moving your data or storing recovery material.

## Report a vulnerability privately

1. In this repository, open **Security > Advisories > Report a vulnerability**. Do not open a public issue with vulnerability details.
2. Include your app version or source revision, OS, impact, and how to reproduce it with sample data. Test only accounts and systems you have permission to use. Share only redacted logs. Leave out keys, private URLs, and workspace data.
3. If the report button is missing, ask a maintainer to enable private vulnerability reporting without sharing the details. Submit the report once the private form is available.

## If your app may be compromised

Stop the app. Revoke exposed keys and connected-account access with each provider. Keep a private backup of your data and relevant redacted logs. Update the app or resolve the reported issue before reopening it, then replace affected keys in **Settings > Keys and connections**. Use **Your data and recovery** on that screen to export or restore your data. Do not disable security checks or delete your workspace to get past a startup error.
