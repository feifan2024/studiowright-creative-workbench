# Notices

Gemini PoolDesk, Copyright (C) 2026 Gemini PoolDesk contributors.
Licensed under GNU Affero General Public License version 3; see LICENSE.
Provided without warranty. Complete local source is included in this directory.

Uses gemini-webapi 2.1.1 by HanaokaYuzu and contributors:
https://github.com/HanaokaYuzu/Gemini-API
Licensed under AGPL-3.0; upstream license text retained as LICENSE.
Reference client source retained unmodified in upstream/client.reference.py.

Python dependencies retain their own licenses and metadata in the isolated
environment. requirements.lock.txt records exact installed package versions.

Interface uses original implementation. Layout decisions reuse the design
skill's previously inspected workbench/error-recovery research: stable tools,
contextual row actions, preserved form values, explicit error/unknown states.
No third-party dashboard code, fonts, imagery or CDN runtime is embedded.
