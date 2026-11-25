# OpenWorker web leaf provenance

MIT-licensed modules from the existing OpenWorker checkout, packaged because vendor checkouts are omitted from production images. No OpenLeads code is included.

Source: https://github.com/andrewyng/openworker/tree/9702c86c7f90425d93fb2de877833ead2f446686

Only `guard.py`, `providers.py`, and the upstream license were copied. The table records the original files. `providers.py` and `LICENSE` still match; Supymem modified `guard.py` to harden URL validation and address pinning. Runtime dependencies are Python standard library, existing httpx for callers, and existing ddgs for the keyless provider. Paid provider classes remain upstream code; hosted GTM selects duckduckgo explicitly and passes no API key.

| Original path | SHA-256 |
| --- | --- |
| coworker/web/guard.py | 5f4409dd5b17ff88b3aebcc95ec4f7d84b55b624f6b186903378c85d6ca2f5f5 |
| coworker/web/providers.py | d39d51b2a0dff506fce7272b62eb795c9e0decda58ae9bab99215413fd27b09f |
| LICENSE | b874c49d0408b72b87638e9cab1349c12d9525e924d2e625525095430b88f449 |
