// nodegraph designer — a standalone playground for the nodegraph.js library.
//
// This bundle carries an UNMODIFIED copy of vendor/nodegraph.js and boots it on
// an empty canvas with every editing feature enabled: create/drag/resize/delete
// nodes, connect ports, groups, params, edge labels, undo/redo, theming, and
// the library's own context menu. There is no Uptracker data, no API usage, and
// no server persistence — work stays in the browser tab. The context menu is
// the whole interface: double-click the canvas for Export/Import JSON (and PDF),
// fit/reset view, and theming.
import { createGraph } from '../vendor/nodegraph.js';

const EMPTY_MODEL = { version: 1, nodes: [], edges: [], groups: [] };

const canvas = document.getElementById('canvas');
const graph = createGraph(canvas);
canvas.dataset.ready = '1';
graph.importJSON(EMPTY_MODEL);
