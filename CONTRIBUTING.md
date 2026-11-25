# Contributing

Use the [README](README.md#start-from-source) to run the app from your checkout. On macOS or Linux, use `./start` for the desktop or `./start --web` for the browser. On Windows, use `.\start.ps1` or `.\start.ps1 --web`. Browser mode does not need the desktop build tools. Try sample mode if you do not want to use your own model key.

## Make a change

1. Read the `AGENTS.md` instructions that apply to the files you touch. Find affected callers, settings, tests, and fixtures before editing.
2. Fix the shared cause and keep the change small. Add a focused regression test for new behavior. Keep your own test data separate from your real workspace.
3. Run the tests that cover your change. For docs changes, check links, commands, and paths against the current checkout. Say what passed, what failed, and what you did not check.
4. In your pull request, explain what changed for the person using the app and how to verify it.

## Protect local users

- Preserve existing data, access controls, consent, and spending limits. Never start unrelated work just because an account is connected.
- Keep guessed email addresses off. Keep source disclosure, opt-out, suppression, and send caps on outreach.
- Do not share real keys, environment dumps, private logs, or customer data. Report vulnerabilities through [Security](SECURITY.md).

Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Check [LICENSE](LICENSE) and [NOTICE](NOTICE) before contributing third-party materials.
