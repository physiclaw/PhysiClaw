// Exercise the complete session viewer script through its DOM events.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {documentFrom} = require('../studio/ui_fixture.cjs');

function createViewer(records, overrides = {}) {
  const filename = path.join(__dirname, '../../src/physiclaw/agent/trace/viewer.html');
  const html = fs.readFileSync(filename, 'utf8');
  const model = {
    sid: 'fixture-session', generated_at: '2026-10-14T12:00:00',
    groups: [{turn: 0, t: '2026-10-14T12:00:00', t_end: '2026-10-14T12:00:01', runtime: [], records}],
    summary: null, files: [], narrative: {}, sessions: null,
    words: {calls: {}, sentinels: {}, tokens: {}}, ...overrides,
  };
  const document = documentFrom(html);
  const data = document.createElement('script');
  data.id = 'session-data';
  data.textContent = JSON.stringify(model);
  document.append(data);
  const context = vm.createContext({
    document, Node: document.constructor, console, location: {hash: ''}, history: {replaceState() {}},
    IntersectionObserver: class { observe() {} },
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(script, context, {filename});
  return {
    document,
    select(id) { document.getElementById(id).listeners.get('click')(); },
    tab(name) {
      document.querySelectorAll('#detail button').find(b => b.dataset.tab === name).listeners.get('click')();
    },
    get detail() { return document.getElementById('detail').textContent; },
  };
}

module.exports = {createViewer};
