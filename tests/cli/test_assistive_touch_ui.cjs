const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {setImmediate} = require('node:timers/promises');
const {documentFrom} = require('../studio/ui_fixture.cjs');

async function createTestPage(initialSequence = 0, sharedState = null, reducedMotion = false) {
  const filename = path.join(__dirname, '../../src/physiclaw/cli/static/test-assistive-touch.html');
  const html = fs.readFileSync(filename, 'utf8');
  const document = documentFrom(html), timers = new Map(), events = new Map();
  document.body = document.querySelector('body');
  const image = document.getElementById('screenshot');
  image.removeAttribute = name => { delete image[name]; };
  const animations = [];
  for (const id of ['code-panel', 'screenshot-panel']) {
    const panel = document.getElementById(id);
    let active;
    panel.getAnimations = () => active ? [active] : [];
    panel.animate = (frames, options) => {
      active = {id, frames, options, cancelled: false, cancel() { this.cancelled = true; }};
      animations.push(active);
      return active;
    };
  }
  const state = sharedState || {
    instance_id: 'first', screenshot: {sequence: initialSequence, received_at: 1234},
    clipboard: {text_id: 0, text: 'PhysiClaw-A01-B02', read_sequence: 0},
    connection: {hostname: 'test-mac.local', ip: '192.168.1.42', port: 18124},
  };
  const network = {failed: false, clipboardFailed: false, responseLost: false, holdRead: null, holdWrite: null};
  const codeRequests = [];
  const clipboard = {text: '', denied: false};
  const context = vm.createContext({
    document, window: {
      addEventListener: (name, fn) => events.set(name, fn),
      matchMedia: () => ({matches: reducedMotion}),
    },
    navigator: {clipboard: {writeText: async text => {
      if (clipboard.denied) throw new Error('clipboard denied');
      clipboard.text = text;
    }}},
    localStorage: {getItem: () => null, setItem() {}}, console, AbortSignal,
    setTimeout: callback => { const id = Symbol(); timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      if (network.failed) throw new Error('offline');
      if (url.endsWith('/clipboard')) {
        assert.equal(options.method, 'POST');
        if (network.clipboardFailed) throw new Error('clipboard unavailable');
        const body = JSON.parse(options.body);
        assert.deepEqual(body, {});
        codeRequests.push(body);
        state.clipboard.text_id++;
        state.clipboard.text = `PhysiClaw-000-${state.clipboard.text_id.toString(16).toUpperCase().padStart(3, '0')}`;
        const snapshot = structuredClone(state);
        if (network.holdWrite) await network.holdWrite;
        if (network.responseLost) throw new Error('response lost after commit');
        return {ok: true, json: async () => snapshot};
      }
      assert.equal(url, '/api/test-assistive-touch');
      assert.equal(options.method ?? 'GET', 'GET');
      assert.equal(options.body, undefined);
      const snapshot = structuredClone(state);
      if (network.holdRead) {
        const pending = network.holdRead;
        network.holdRead = null;
        await pending;
      }
      return {ok: true, json: async () => snapshot};
    },
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context, {filename});
  await setImmediate();
  return {
    state, network, image, events, timers, codeRequests, clipboard, animations,
    el: id => document.getElementById(id),
    poll: async () => {
      const callbacks = [...timers.values()]; timers.clear();
      for (const callback of callbacks) await callback();
    },
    newCode: () => document.getElementById('test-code').onclick(),
  };
}

test('ignores old uploads on open and displays the next portrait image', async () => {
  const ui = await createTestPage(7);
  assert.equal(ui.image.src, undefined);
  assert.equal(ui.image.hidden, true);
  await ui.poll();
  assert.equal(ui.image.src, undefined);
  ui.state.screenshot.sequence = 8;
  await ui.poll();
  assert.match(ui.image.src, /screenshot\/8\?/);
  assert.equal(ui.animations.length, 0);
  ui.image.naturalWidth = 1170; ui.image.naturalHeight = 2532;
  ui.image.onload();
  assert.equal(ui.image.hidden, false);
  assert.equal(ui.el('empty').hidden, true);
  assert.equal(ui.el('image-meta').hidden, true);
  assert.deepEqual(ui.animations.map(animation => animation.id), ['screenshot-panel']);
  await ui.poll();
  assert.equal(ui.animations.length, 1);
  ui.state.screenshot.sequence++;
  await ui.poll();
  ui.image.onerror();
  assert.equal(ui.animations.length, 1);
});

test('highlights each phone clipboard fetch without replaying old activity', async () => {
  const ui = await createTestPage();
  await ui.newCode();
  assert.equal(ui.animations.length, 0);
  ui.state.clipboard.read_sequence++;
  await ui.poll();
  assert.deepEqual(ui.animations.map(animation => animation.id), ['code-panel']);
  await ui.poll();
  assert.equal(ui.animations.length, 1);
  ui.state.clipboard.read_sequence++;
  await ui.poll();
  assert.equal(ui.animations.length, 2);
  assert.equal(ui.animations[0].cancelled, true);
  const reopened = await createTestPage(0, ui.state);
  assert.equal(reopened.animations.length, 0);
  ui.state.instance_id = 'restarted';
  ui.state.clipboard.read_sequence = 0;
  await ui.poll();
  assert.equal(ui.animations.length, 2);
  ui.state.clipboard.read_sequence++;
  await ui.poll();
  assert.equal(ui.animations.length, 3);
});

test('reduced motion uses a steady highlight instead of a pulse', async () => {
  const ui = await createTestPage(0, null, true);
  ui.state.clipboard.read_sequence++;
  await ui.poll();
  const animation = ui.animations[0];
  assert.deepEqual(animation.frames[0], animation.frames[1]);
  assert.equal(animation.options.duration, 700);
});

test('reads the existing server code on open and requests a new code only on click', async () => {
  const ui = await createTestPage();
  const first = ui.el('test-code').textContent;
  assert.match(first, /^PhysiClaw-[0-9A-F]{3}-[0-9A-F]{3}$/);
  assert.equal(first, ui.state.clipboard.text);
  assert.equal(ui.codeRequests.length, 0);
  await ui.poll();
  assert.equal(ui.codeRequests.length, 0);
  assert.equal(ui.el('test-code').textContent, first);
  await ui.newCode();
  await ui.poll();
  assert.notEqual(ui.el('test-code').textContent, first);
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(ui.codeRequests.length, 1);
  const reopened = await createTestPage(0, ui.state);
  assert.equal(reopened.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(reopened.codeRequests.length, 0);
});

test('automatically follows code changes from another tab without a refresh reminder', async () => {
  const ui = await createTestPage();
  const other = await createTestPage(0, ui.state);
  await other.newCode();
  await ui.poll();
  assert.equal(ui.el('test-code').textContent, other.el('test-code').textContent);
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(ui.el('code-error'), null);
  assert.equal(ui.codeRequests.length, 0);
});

test('server restart resets the preview and displays the new server code', async () => {
  const ui = await createTestPage(7);
  const first = ui.el('test-code').textContent;
  ui.state.instance_id = 'second';
  ui.state.screenshot.sequence = 0;
  ui.state.clipboard = {text_id: 0, text: 'PhysiClaw-ABC-DEF', read_sequence: 0};
  await ui.poll();
  assert.equal(ui.image.hidden, true);
  assert.notEqual(ui.el('test-code').textContent, first);
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(ui.codeRequests.length, 0);
  ui.state.screenshot.sequence = 1;
  await ui.poll();
  assert.match(ui.image.src, /screenshot\/1\?instance=second/);
});

test('network failure is visible and recovers; closing the page stops polling', async () => {
  const ui = await createTestPage();
  ui.network.failed = true;
  await ui.poll();
  assert.equal(ui.el('offline').hidden, false);
  assert.equal(ui.el('test-code').disabled, true);
  assert.equal(ui.el('test-code').textContent, 'PhysiClaw-···-···');
  ui.network.failed = false;
  ui.state.clipboard = {text_id: 1, text: 'PhysiClaw-CCC-DDD', read_sequence: 0};
  await ui.poll();
  assert.equal(ui.el('offline').hidden, true);
  assert.equal(ui.el('test-code').disabled, false);
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  ui.events.get('pagehide')();
  assert.equal(ui.timers.size, 0);
});

test('failed code changes automatically recover the server value', async () => {
  const ui = await createTestPage();
  const previous = ui.el('test-code').textContent;
  ui.network.clipboardFailed = true;
  await ui.newCode();
  assert.equal(ui.el('offline').hidden, false);
  assert.equal(ui.el('test-code').textContent, 'PhysiClaw-···-···');
  ui.network.clipboardFailed = false;
  await ui.poll();
  assert.equal(ui.el('test-code').textContent, previous);
  await ui.newCode();
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(ui.el('offline').hidden, true);
});

test('a committed code change with a lost response is recovered without another write', async () => {
  const ui = await createTestPage();
  ui.network.responseLost = true;
  await ui.newCode();
  assert.equal(ui.el('test-code').textContent, 'PhysiClaw-···-···');
  await ui.poll();
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(ui.codeRequests.length, 1);
});

test('the old code is hidden while a new server value is pending', async () => {
  const ui = await createTestPage();
  let release;
  ui.network.holdWrite = new Promise(resolve => { release = resolve; });
  const pending = ui.newCode();
  assert.equal(ui.el('test-code').textContent, 'PhysiClaw-···-···');
  assert.equal(ui.el('test-code').disabled, true);
  release();
  await pending;
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
});

test('restoring a page resumes polling while a previous code change is pending', async () => {
  const ui = await createTestPage();
  let releasePrevious, releaseCurrent;
  ui.network.holdWrite = new Promise(resolve => { releasePrevious = resolve; });
  const previous = ui.newCode();
  ui.events.get('pagehide')();
  ui.events.get('pageshow')({persisted: true});
  await setImmediate();
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(ui.el('test-code').disabled, false);
  assert.equal(ui.timers.size, 1);

  ui.network.holdWrite = new Promise(resolve => { releaseCurrent = resolve; });
  const current = ui.newCode();
  releasePrevious();
  await previous;
  assert.equal(ui.el('test-code').disabled, true);
  assert.equal(ui.el('test-code').textContent, 'PhysiClaw-···-···');
  assert.equal(ui.timers.size, 0);
  releaseCurrent();
  await current;
  assert.equal(ui.el('test-code').textContent, ui.state.clipboard.text);
  assert.equal(ui.el('test-code').disabled, false);
  assert.equal(ui.timers.size, 1);
});

for (const restart of [false, true]) {
  test(`a delayed poll cannot undo a confirmed code change${restart ? ' across restart' : ''}`, async () => {
    const ui = await createTestPage();
    let release;
    ui.network.holdRead = new Promise(resolve => { release = resolve; });
    const pending = ui.poll();
    await setImmediate();
    if (restart) ui.state.instance_id = 'second';
    await ui.newCode();
    const confirmed = ui.el('test-code').textContent;
    release();
    await pending;
    assert.equal(ui.el('test-code').textContent, confirmed);
    assert.equal(confirmed, ui.state.clipboard.text);
    assert.equal(ui.timers.size, 1);
  });
}

test('shows both shortcut URLs using the phone port and updates after restart', async () => {
  const ui = await createTestPage();
  assert.equal(ui.el('shortcut-addresses').hidden, false);
  assert.equal(ui.el('lan-address').textContent, '192.168.1.42');
  assert.equal(ui.el('lan-port').textContent, '18124');
  assert.equal(ui.el('upload-hostname').textContent, 'http://test-mac.local:18124/api/bridge/screenshot');
  assert.equal(ui.el('clipboard-hostname').textContent, 'http://test-mac.local:18124/api/bridge/clipboard');
  ui.state.instance_id = 'second';
  ui.state.connection = {hostname: 'new-mac.local', ip: '192.168.1.43', port: 8049};
  await ui.poll();
  assert.equal(ui.el('lan-address').textContent, '192.168.1.43');
  assert.equal(ui.el('lan-port').textContent, '8049');
  assert.equal(ui.el('clipboard-hostname').textContent, 'http://new-mac.local:8049/api/bridge/clipboard');
});

test('unavailable addresses are not replaced with a misleading local URL', async () => {
  const ui = await createTestPage();
  ui.state.instance_id = 'second';
  ui.state.connection = {hostname: null, ip: null, port: 8049};
  await ui.poll();
  assert.equal(ui.el('upload-hostname').textContent, 'Unavailable');
  assert.equal(ui.el('lan-address').textContent, 'Unavailable');
  assert.equal(ui.el('upload-copy').disabled, true);
  assert.equal(ui.el('clipboard-copy').disabled, true);
  ui.state.instance_id = 'third';
  ui.state.connection = null;
  await ui.poll();
  assert.equal(ui.el('shortcut-addresses').hidden, true);
});

test('copy icons copy complete URLs and reset their confirmation', async () => {
  const ui = await createTestPage();
  for (const [kind, endpoint] of [['upload', 'screenshot'], ['clipboard', 'clipboard']]) {
    const button = ui.el(kind + '-copy');
    assert.equal(button.disabled, false);
    await button.onclick();
    assert.equal(ui.clipboard.text, `http://test-mac.local:18124/api/bridge/${endpoint}`);
    assert.equal(button.querySelector('.copy-feedback').textContent, 'Copied');
    assert.equal(button.classList.contains('copied'), true);
    await ui.poll();
    assert.equal(button.querySelector('.copy-feedback').textContent, '');
    assert.equal(button.classList.contains('copied'), false);
  }
});

test('copy failure does not claim success and allows retry', async () => {
  const ui = await createTestPage(), button = ui.el('upload-copy');
  ui.clipboard.denied = true;
  await button.onclick();
  assert.equal(button.querySelector('.copy-feedback').textContent, 'Copy failed');
  assert.equal(button.classList.contains('copied'), false);
  assert.equal(ui.clipboard.text, '');
  ui.clipboard.denied = false;
  await button.onclick();
  assert.equal(button.querySelector('.copy-feedback').textContent, 'Copied');
});
