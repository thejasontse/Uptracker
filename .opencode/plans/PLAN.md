# Plan: Node view — single Save button + relocated Delete button

## Goal & Scope
- **Objective**: In the admin docked panel's node-detail view, replace the two per-field
  "Save" buttons (Network Path, Tags) with one "Save" that PATCHes both fields, and move
  the "Delete" button from the Datasets section to the bottom of the node-info section —
  Save left-aligned, Delete right-aligned, on the same row.
- **Branch**: `fix/node-view-save-delete-buttons`
- **Out of Scope**: Backend changes (none needed — `PATCH api/nodes/{id}` already accepts
  `network_path` and `tags` together, `app/main.py:396-408`), dataset-card buttons,
  user-facing dashboard dock.

## Codebase Context
- **Key Files**:
  - `admin/js/panel.js` — dock view router; `nodeHtml()` (lines 129-149) renders the node
    detail view; `onDockClick()` (lines 191-245) handles Save/Delete clicks.
  - `admin/app.css` — global stylesheet; `.section` (143), `.row` flex helper (144),
    `button.danger` (194), `button.small` (196). No new CSS expected.
- **Patterns & Constraints**: vanilla ES modules, template-string HTML via `innerHTML`,
  delegated clicks on `#dockBody`; `api.patchNode()` (`admin/js/api.js:24`) already sends
  whatever object it's given; `refresh()` re-renders the node view after mutation.
- **Current state**:
  - Network Path row (panel.js:135): input + `#pathSave` button → handler at 211-215.
  - Tags row (panel.js:137): input + `#tagsSave` button → handler at 216-220.
  - Delete button lives in the Datasets section row (panel.js:145) with inline
    `style="margin-left:auto"`; handler `delNode` at 236-243 (confirm →
    `window.__graph.removeNode` → graph sync via `PUT api/graph`).

## Implementation Phases

### Phase 1: UI restructure (`admin/js/panel.js`)
- [x] **Task 1.1**: In `nodeHtml()` first section (lines 134-138), remove the
      `<button id="pathSave">` and `<button id="tagsSave">` from their `.row` divs so each
      row contains only its input (Tags keeps its `list="tagsList"`).
- [x] **Task 1.2**: At the bottom of that first section (after the `<datalist>`), add one
      action row: Save on the left, Delete on the right, e.g.
      `<div class="row"><button id="nodeSave" class="primary small">Save</button><button data-act="delNode" class="danger small" style="margin-left:auto">Delete</button></div>`
      (keeps existing `delNode` handler + inline margin pattern). *(Depends on 1.1)*
- [x] **Task 1.3**: Remove the Delete button from the Datasets section row (line 145),
      leaving "+ Dataset" and "Add dataset from templates". *(Depends on 1.2)*
- **Milestone Commit**: `fix(admin): single Save + right-aligned Delete in node view`

### Phase 2: Handler consolidation (`admin/js/panel.js`)
- [x] **Task 2.1**: Replace the two handlers (lines 211-220) with one for `#nodeSave`:
      `await api.patchNode(node.id, { network_path: pathIn.value, tags: tagsIn.value }); await refresh();`
      Keep the same guard style (`e.target.id === 'nodeSave' && node`). *(Depends on Phase 1)*
- [x] **Task 2.2**: Update the stale comment in `onDockChange()` (lines 247-251) that
      references "their explicit Save button (pathSave)".
- **Milestone Commit**: `fix(admin): save network path and tags in one patch`

## Risks & Edge Cases
- **Risk**: Delete inside the first section changes confirm/refresh flow expectations →
  **Mitigation**: reuse the existing untouched `delNode` case verbatim; only its position
  in the DOM moves.
- **Risk**: `.row` inputs are `width:100%` flex children — adding buttons inline could
  squeeze inputs → **Mitigation**: buttons leave the input rows entirely; action row has
  no input so no squeeze occurs.
- **Edge case**: empty Tags input saves as empty string (clears tags) — same behavior as
  today's `#tagsSave`.

## Verification Checklist
- [x] `pytest tests/` passes (no backend change expected).
- [ ] Manual smoke: select a node → first section shows Network Path + Tags inputs with
      no per-field Save; bottom row shows "Save" (left) and "Delete" (right), same row.
- [ ] Manual smoke: edit both fields, one click persists both after refresh.
- [ ] Manual smoke: Delete still confirms, removes node from graph, returns panel to home.
- [ ] Datasets section row now shows only "+ Dataset" / "Add dataset from templates".

## PR Summary Draft
- **Title**: `fix(admin): consolidate node Save button and move Delete beside it`
- **Summary**:
  - Node detail view now has a single Save (left) that PATCHes Network Path + Tags, and
    Delete (right) on the same row at the bottom of the node-info section.
  - Removed `#pathSave`/`#tagsSave` handlers in favor of one `#nodeSave` handler; no API
    or backend changes.
