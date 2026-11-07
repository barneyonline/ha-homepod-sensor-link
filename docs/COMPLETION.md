# Final scope and completion boundary

Final name/domain/repository: **HomePod Sensor Link**, `homepod_sensor_link`, `barneyonline/ha-homepod-sensor-link`. The local folder has also been renamed to `ha-homepod-sensor-link`.

| Agreed requirement | Implemented local behaviour |
|---|---|
| Newcomer onboarding | Candidate extraction guide, local credential input, discovery and bounded read-only matching; newly owned sensors avoid unsupported native import |
| Input | Selected HAP CSV, JSON or explicit controller fields; derive public keys and validate supplied pins; missing/binary/ambiguous data fails safely |
| Transport | Local HA administrator only; HTTP allowed; no extra acknowledgement; reject Cloud/remote/wrong-user/expired import sessions |
| Existing entries | Adopt native measurement entries unchanged; no native entity-ID/history mutation or duplication |
| New entries | Dedicated persisted User controllers; integration-owned temperature/humidity and diagnostic entities |
| Recovery defaults | On by default per device, active only with verified administrator credentials; normal optional-feature label |
| Secret lifetime | Keep administrator secret while setup is unfinished or recovery needs it; discard after completed monitoring-only publication |
| Device management | One entry, per-device add/import, Resume, Cancel, policy change and automatic local cleanup |
| Partial/restart | Keep ready and pending results separately; reuse saved keys; inspect accepted pairings after restart; another onboarding write requires Resume |
| Mutation limits | Persist before writes; preserve every old pairing row; never silently escalate User to Admin; re-import/Resume cannot bypass recovery gates on commissioned devices |
| Safeguards | Five-minute fresh checks, ten-minute startup grace, three confirmed failures spanning ten minutes, one attempt/hour and three/day, bounded persisted reloads |
| Cleanup | Automatically delete owned credentials, journal and recovery history; remove owned device/entities; do not revoke accessory pairings or change native storage/history |
| Fresh start | No compatibility package, transition or migration; no old storage access; conflict guard remains; removal/re-add intentionally resets repair history |
| Quality gates | Exact 100% Python statements/branches and credential-panel Node coverage; strict mypy, upstream hassfest and 54-rule evidence/readiness gates |
| Privacy/testing | Generated identities, documentation endpoints, anonymous diagnostics, controlled errors, offline HA tests, exact-byte package scanning |

## Remaining evidence, approvals and hard boundaries

1. **Extraction proof:** the macOS encrypted-backup GUI is source-researched, not real-backup verified. The guide cannot be advertised as a tested supported route until the exact HomeKit records export intact on a recorded OS/application combination.
2. **Hardware support:** User pairing/read access, Apple Home preservation and full device lifecycle must be verified under separate live authorisation and the existing 1Password workflow before supported-release claims.
3. **Git provenance:** publication preserves the existing GitHub initial commit. Current files, index and reachable history are scanned; the earlier development baseline `2d5e069` is still unavailable and unauthenticated. No replacement baseline or fabricated ancestry is claimed.
4. **Distribution:** source and GitHub Wiki publication are authorised. Hosted CI and Codecov reporting have passed. A release, HACS/branding acceptance and private reporting are distinct remaining steps; no live deployment is authorised.
5. **Deliberate exclusions:** no implicit extraction, SIP/AMFI changes, keychain/backup access by the integration, native storage edits, credential rotation, automatic controller revocation or legacy migration.

No live HA installation, accessory, backup, keychain or credential was accessed. The browser preview used only a synthetic local harness. See `VALIDATION.md` for observed checks.
