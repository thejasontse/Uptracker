# Docked-Panel UI Cleanup — Implementation Plan

## M1 — Remove `[Name] tokens are saved…` hints (UI only)
- [x] `admin/js/editor.js:58` remove muted span from Parameters card head
- [x] `admin/js/metrics.js:78` same
- [x] `admin/js/rules.js:134` same

## M2 — Rename "Network-Path Reference" → "Network Path"
- [ ] UI label in `admin/js/panel.js:112`
- [ ] Collapse `NETWORK_PATH_HINTS` to single placeholder in `admin/js/util.js`; update panel.js reference
- [ ] Param token rename `[Network-Path Reference]` → `[Network Path]` in `app/templates.py`, `admin/js/params.js`, `app/db.py` `_KNOWN_PARAM_KEYS`
- [ ] Migration `migrate_rename_network_path_token()` in `app/db.py` rewriting scripts, alert bodies, template payloads, and saved param_values keys
- [ ] Update tests in `test_logic.py` and `test_api.py`

## M3 — Drop per-node "Tracking enabled"
- [ ] Remove checkbox + handler from `admin/js/panel.js`
- [ ] Remove `tracking` from `NodePatch`, patch UPDATE, snapshot builder, scheduling kick in `app/main.py`
- [ ] Migration `migrate_drop_node_tracking()` (ALTER TABLE DROP COLUMN) in `app/db.py`; update `_seed_demo`
- [ ] Drop `tracking` from `nodes_wide` in `app/query.py` and docstring
- [ ] Update tests: rename `test_node_patch_and_tracking`, fix column list at line 662
- [ ] Reword comment in `admin/js/app.js:388`

## M4 — Full removal of "Network Resource" + clear bundled templates
- [ ] UI: remove dropdown + handler from `panel.js`; delete label map/hints from `util.js`; drop networkResources fetch from `app.js`; remove `_network_resources` preservation in `state.js`; remove API client method
- [ ] Backend: delete `BASE_NETWORK_RESOURCES`, `/api/network-resources` endpoint, snapshot/NodePatch fields in `main.py`
- [ ] Remove `"network resource"` param key from `templates.py`, `params.js`, `db.py` `_KNOWN_PARAM_KEYS`
- [ ] Migration `migrate_drop_network_resource_and_clear_templates()`: DELETE dataset templates; strip tokens/mustaches from scripts+alerts+payloads; remove key from saved param_values; ALTER TABLE DROP COLUMN network_resource
- [ ] Drop `network_resource` from `nodes_wide` in `app/query.py`
- [ ] Tests: delete resource tests, fix INSERT statements in test_logic.py and test_api.py

## M5 — Fixed terminal section at bottom of docked panel
- [ ] Add `<div id="dockTerminal">` sibling to `#dockBody` in `admin/index.html`
- [ ] CSS in `app.css`: 180px fixed, reuse `pre.out` palette
- [ ] New module `admin/js/dockTerminal.js` exporting `showTestResult`, `clearTerminal`, `setVisible`
- [ ] Wire into `panel.js:render()` for visibility + per-page clear
- [ ] Replace inline output in `editor.js:testRun()`, remove `#testResult` div and `#testOut` span
- [ ] Replace inline raw box in `metrics.js:testRun()`, remove `#rawBox` card and `paintRaw`/`initialRaw` helpers
- [ ] Replace inline status in `rules.js:testRules()`

## Verification Checklist
- [ ] `pytest tests/` passes after each milestone
- [ ] Manual smoke: node view has no Tracking checkbox, Network Path label correct
- [ ] Manual smoke: Parameters card has no token hint
- [ ] Manual smoke: Test run populates terminal with timestamped header + raw + parsed
- [ ] Manual smoke: switching pages clears terminal; leaving editor hides it
