const assert = require('node:assert/strict');
const {test} = require('node:test');
const {createViewer} = require('./viewer_ui_fixture.cjs');

const row = (rec, src = 'events', extra = {}) => ({src, line: 1, rec, ...extra});

test('test_selected_recording_can_navigate_to_internal_session_with_same_name', () => {
  const ui = createViewer([], {sid: 'same name', review_url: '/review/', sessions: [{sid: 'same name', outcome: {}}]});
  const pick = ui.document.querySelector('#hdr select');
  const initialOptions = ui.document.querySelectorAll('#hdr option').map(o => [o.value, !!o.selected]);

  pick.value = '/sessions/same%20name/';
  pick.listeners.get('change')();

  assert.equal(ui.href, '/sessions/same%20name/');
  assert.deepEqual(initialOptions, [['/review/', true], ['/sessions/same%20name/', false]]);
});

test('test_normal_session_picker_preserves_encoded_navigation', () => {
  const ui = createViewer([], {sid: 'one', sessions: [{sid: 'one', outcome: {}}, {sid: '记录 #?', outcome: {}}]});
  const options = ui.document.querySelectorAll('#hdr option');
  const pick = ui.document.querySelector('#hdr select');

  pick.value = options[1].value;
  pick.listeners.get('change')();

  assert.equal(options[0].selected, true);
  assert.equal(ui.href, `/sessions/${encodeURIComponent('记录 #?')}/`);
});

for (const rec of [
  {event: 'wake', triggers: [null]},
  {event: 'wake', triggers: 1},
  {event: 'tool_result', images: 'images/broken.jpg', text: 'screen'},
  {event: 'other', t: 12, elapsed_ms: {toString: null}},
]) {
  test(`test_regression_bad_record_keeps_page_and_raw_tab_${JSON.stringify(rec)}`, () => {
    const following = {src: 'events', line: 2, rec: {event: 'done', sentinel: 'DONE', recap: 'still visible'}};

    const ui = createViewer([row(rec), following]);
    ui.select('events-1');
    ui.tab('raw');

    assert.ok(ui.detail.includes(JSON.stringify(rec, null, 2)));
    ui.select('events-2');
    assert.match(ui.detail, /still visible/);
  });
}

for (const messages of [1, 'broken', [null], [{role: 'assistant', tool_calls: [null]}]]) {
  test(`test_regression_bad_request_can_still_show_raw_messages_${JSON.stringify(messages)}`, () => {
    const rec = {kind: 'request', messages};
    const ui = createViewer([row(rec, 'wire')]);

    ui.select('wire-1');
    ui.tab('raw');

    assert.ok(ui.detail.includes(JSON.stringify(rec, null, 2)));
  });
}

test('test_unformatted_server_record_shows_raw_fallback_immediately', () => {
  const rec = {event: 'tool_result', arguments: {bbox: 1}};
  const ui = createViewer([row(rec, 'events', {unformatted: true, warn: true})]);

  ui.select('events-1');

  assert.match(ui.detail, /Could not format/);
  assert.ok(ui.detail.includes(JSON.stringify(rec, null, 2)));
});

test('test_regression_damaged_summary_does_not_block_session_rows', () => {
  const ui = createViewer([row({event: 'done', recap: 'available'})], {
    summary: {started_at: 42, duration_s: {toString: null}, usage: {cache_hit_pct: {toString: null}}, outcome: {sentinel: {toString: null}, recap: {toString: null}}, walks: [null, 7, {app: {toString: null}, playbook: {toString: null}, outcome: {toString: null}, node: {toString: null}, reason: {toString: null}}], turns: {toString: null}},
    sessions: [{sid: 'other', outcome: {recap: 42}}],
  });

  ui.select('events-1');

  assert.match(ui.detail, /available/);
});

test('test_valid_request_renders_messages_and_raw_tab', () => {
  const rec = {kind: 'request', messages: [{role: 'user', content: 'a real message'}]};
  const ui = createViewer([row(rec, 'wire', {new_from: 0, carried: 0})]);

  ui.select('wire-1');

  assert.match(ui.detail, /a real message/);
  assert.doesNotMatch(ui.detail, /Could not format/);
});

test('test_regression_show_carried_messages_preserves_damaged_message', () => {
  const rec = {kind: 'request', messages: [null, {role: 'user', content: 'new message'}]};
  const ui = createViewer([row(rec, 'wire', {new_from: 1, carried: 1})]);
  ui.select('wire-1');

  ui.document.querySelector('#detail .more').listeners.get('click')();

  assert.match(ui.detail, /Could not format/);
  assert.match(ui.detail, /null/);
  assert.match(ui.detail, /new message/);
});

test('test_keyboard_navigation_moves_past_damaged_record', () => {
  const records = [row({event: 'wake', triggers: 1}), {src: 'events', line: 2, rec: {event: 'done', recap: 'next row'}}];
  const ui = createViewer(records);
  ui.select('events-1');

  ui.document.listeners.get('keydown')({target: {tagName: 'DIV'}, key: 'ArrowDown', preventDefault() {}});

  assert.match(ui.detail, /next row/);
});

test('test_regression_summary_data_cannot_impersonate_a_dom_node', () => {
  const summary = {outcome: {recap: {nodeType: 1, toString: null}}};

  const ui = createViewer([], {summary});

  assert.match(ui.document.getElementById('hdr').textContent, /"nodeType":1/);
});
