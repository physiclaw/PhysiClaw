const assert = require('node:assert/strict');
const {test} = require('node:test');
const {createUI} = require('./ui_fixture.cjs');

for (const [panel, list, item] of [['pb', 'route', 'fixture/second'], ['mc', 'steps', 'second']]) {
  for (const value of ['operator text', '', '中文 <text>']) {
    test(`test_regression_${panel}_cursor_change_preserves_input_${JSON.stringify(value)}`, async () => {
      const ui = await createUI();
      ui.input(panel).value = value;

      ui.el(list).children[1].onclick();

      assert.equal(ui.input(panel).value, value);
    });
  }

  test(`test_${panel}_switching_item_uses_its_defaults`, async () => {
    const ui = await createUI();
    ui.input(panel).value = 'operator text';

    ui.choose(panel + '-select', item);

    assert.equal(ui.input(panel).value, 'second default');
  });
}

test('test_regression_successful_action_updates_reconnected_indicator', async () => {
  const ui = await createUI();
  ui.routes.set('/api/state', {status: 'ok', connected: false});
  ui.routes.set('/api/act', {status: 'error', message: 'connection lost'});
  await ui.header.onclick();
  assert.equal(ui.el('conn-label').textContent, 'not connected');
  ui.routes.set('/api/state', {status: 'ok', connected: true});
  ui.routes.set('/api/act', {status: 'ok'});

  await ui.header.onclick();

  assert.equal(ui.el('conn-label').textContent, 'connected');
});

for (const [button, endpoint, list] of [
  ['mc-step', '/api/macro', 'steps'],
  ['pb-step', '/api/step', 'route'],
  ['pb-reset', '/api/step/reset', 'route'],
]) {
  test(`test_regression_${button}_locks_controls_before_response`, async () => {
    const ui = await createUI();
    const pending = Promise.withResolvers();
    ui.routes.set(endpoint, () => pending.promise);

    const launch = ui.click(button);
    ui.click(button); // Even a queued duplicate event must be ignored.
    ui.el(list).children[1].onclick();

    assert.equal(ui.el(button).disabled, true);
    assert.equal(ui.el('pb-select').disabled, true);
    assert.equal(ui.el('mc-select').disabled, true);
    assert.equal(ui.input('pb').disabled, true);
    assert.equal(ui.input('mc').disabled, true);
    assert.equal(ui.requests.filter(r => r.url === endpoint && r.body).length, 1);
    assert.equal(ui.el(list).children[1].classList.contains(list === 'route' ? 'at' : 'cur'), false);
    pending.resolve({status: 'error', message: 'refused'});
    await launch;
    assert.equal(ui.el(button).disabled, false);
  });

  test(`test_${button}_network_error_unlocks_and_reports_failure`, async () => {
    const ui = await createUI();
    ui.routes.set(endpoint, () => { throw new Error('fixture offline'); });

    await ui.click(button);

    assert.equal(ui.el(button).disabled, false);
    assert.match(ui.el('banner').textContent, /fixture offline/);
  });
}

test('test_regression_macro_completion_keeps_inputs_and_advances_cursor', async () => {
  const ui = await createUI();
  ui.input('mc').value = 'operator text';
  ui.routes.set('/api/step', {status: 'ok', running: false, result: {ok: true, message: 'done'}});

  await ui.click('mc-step');
  await ui.flush();

  assert.equal(ui.requests.find(r => r.url === '/api/macro').body.inputs.query, 'operator text');
  assert.equal(ui.input('mc').value, 'operator text');
  assert.equal(ui.el('steps').children[1].classList.contains('cur'), true);
  assert.equal(ui.el('mc-step').disabled, false);
});

for (const [name, response] of [
  ['network', () => { throw new Error('fixture disconnected'); }],
  ['server', () => ({status: 'error', message: 'fixture server error'})],
  ['invalid JSON', () => null],
]) {
  test(`test_poll_failure_retains_lock_until_job_finishes_${name}`, async () => {
    const ui = await createUI();
    ui.routes.set('/api/step', response);

    await ui.click('mc-step');
    await ui.flush();

    assert.equal(ui.el('mc-step').disabled, true);
    assert.match(ui.el('banner').textContent, /retrying/);
    assert.equal(ui.timers.size, 1);

    ui.routes.set('/api/step', {status: 'ok', running: false, result: {ok: true, message: 'done'}});
    await ui.poll();

    assert.equal(ui.el('mc-step').disabled, false);
    assert.equal(ui.el('banner').textContent, '');
    assert.equal(ui.timers.size, 0);
  });
}

test('test_state_refresh_does_not_unlock_pending_action', async () => {
  const ui = await createUI();
  const pending = Promise.withResolvers();
  ui.routes.set('/api/state', () => pending.promise);

  const action = ui.header.onclick();
  await ui.flush();

  assert.equal(ui.header.disabled, true);
  pending.resolve({status: 'ok', connected: true, tools: ['peek']});
  await action;
  assert.equal(ui.header.disabled, false);
});

for (const data of [null, [], 'not an object']) {
  test(`test_invalid_launch_reply_reports_error_${JSON.stringify(data)}`, async () => {
    const ui = await createUI();
    ui.routes.set('/api/macro', data);

    await ui.click('mc-step');

    assert.equal(ui.el('banner').textContent, 'unreadable reply');
    assert.equal(ui.el('mc-step').disabled, false);
  });
}

for (const [response, message] of [
  [{status: 503}, 'HTTP 503'],
  [{status: 200, unreadable: true}, 'unreadable reply'],
]) {
  test(`test_failed_http_response_reports_${message}`, async () => {
    const ui = await createUI();
    ui.responses.set('/api/macro', response);

    await ui.click('mc-step');

    assert.equal(ui.el('banner').textContent, message);
    assert.equal(ui.el('mc-step').disabled, false);
  });
}

test('test_playbook_launch_submits_inputs_and_clears_one_shot_fields', async () => {
  const ui = await createUI();
  ui.input('pb').value = 'operator query';
  ui.el('pb-reply').value = 'confirmed';
  ui.el('route').children[1].onclick();
  ui.routes.set('/api/step', ({body}) => body ? {status: 'ok'} : {status: 'ok', running: false});

  await ui.click('pb-step');
  await ui.flush();

  assert.deepEqual(ui.requests.find(r => r.url === '/api/step' && r.body).body, {
    ref: 'fixture/first', inputs: {query: 'operator query'}, outputs: {}, at: 'finish', reply: 'confirmed',
  });
  assert.equal(ui.el('pb-reply').value, '');
  assert.equal(ui.input('pb').value, 'operator query');
  assert.equal(ui.el('pb-step').disabled, false);
});

test('test_reset_waits_for_catalog_before_unlocking', async () => {
  const ui = await createUI();
  const pending = Promise.withResolvers();
  ui.routes.set('/api/playbooks', () => pending.promise);

  const reset = ui.click('pb-reset');
  await ui.flush();

  assert.equal(ui.el('pb-reset').disabled, true);
  pending.resolve({status: 'error', message: 'fixture catalog unavailable'});
  await reset;
  assert.equal(ui.el('pb-reset').disabled, false);
  assert.equal(ui.el('banner').textContent, 'fixture catalog unavailable');
});

for (const button of ['pb-step', 'pb-end']) {
  test(`test_regression_invalid_position_blocks_${button}_until_reset`, async () => {
    const ui = await createUI();
    const entry = ui.routes.get('/api/playbooks').packs[0].playbooks[0];
    entry.position_error = 'Saved position is invalid: <bad>. Use Reset to start over.';
    await ui.click('pb-reset'); // Reload the catalog with the invalid position.

    await ui.click(button);

    assert.equal(ui.el('pb-pos').textContent, entry.position_error);
    assert.equal(ui.el('banner').textContent, entry.position_error);
    assert.equal(ui.el('pb-select').options[0].disabled, false);
    assert.equal(ui.el('pb-reset').disabled, false);
    assert.equal(ui.requests.filter(r => r.url === '/api/step' && r.body).length, 0);

    delete entry.position_error;
    ui.routes.set('/api/step', ({body}) => body ? {status: 'ok'} : {status: 'ok', running: false});
    await ui.click('pb-reset');
    await ui.click(button);
    await ui.flush();

    assert.equal(ui.requests.filter(r => r.url === '/api/step' && r.body).length, 1);
  });
}

for (const event of ['pointercancel', 'lostpointercapture']) {
  test(`test_regression_${event}_discards_drag_before_pointer_up`, async () => {
    const ui = await createUI();
    ui.pointer('cam', 'pointerdown', 40, 70);
    ui.pointer('cam', 'pointermove', 200, 350);

    ui.pointer('cam', event, 200, 350);
    ui.pointer('cam', 'pointerup', 200, 350);

    assert.equal(ui.el('cap-label').textContent, 'no selection');
    assert.equal(ui.el('cam-wrap').querySelector('.drag').style.display, 'none');
    assert.equal(ui.requests.filter(r => r.body?.tool === 'swipe').length, 0);
  });
}

test('test_regression_other_pointer_does_not_replace_or_finish_drag', async () => {
  const ui = await createUI();
  ui.pointer('cam', 'pointerdown', 40, 70);

  ui.pointer('cam', 'pointerdown', 300, 600, 2);
  ui.pointer('cam', 'pointermove', 350, 650, 2);
  ui.pointer('cam', 'pointerup', 350, 650, 2);
  ui.pointer('cam', 'pointermove', 200, 350);
  ui.pointer('cam', 'pointerup', 200, 350);

  assert.equal(ui.el('cap-geo').textContent, '[0.10, 0.10, 0.50, 0.50]');
});

test('test_macro_shortcut_does_not_change_selection_while_launching', async () => {
  const ui = await createUI();
  ui.choose('mc-select', 'second');
  const pending = Promise.withResolvers();
  ui.routes.set('/api/macro', () => pending.promise);
  const launch = ui.click('mc-step');

  ui.el('route').children[0].querySelector('.g').onclick({stopPropagation() {}});

  assert.equal(ui.el('mc-select').value, 'second');
  pending.resolve({status: 'error', message: 'refused'});
  await launch;
});

for (const event of ['pointercancel', 'lostpointercapture']) {
  test(`test_cancelled_swipe_never_sends_phone_gesture_${event}`, async () => {
    const ui = await createUI();
    ui.pointer('cam', 'pointerdown', 40, 70);
    ui.pointer('cam', 'pointermove', 200, 350);
    ui.pointer('cam', 'pointerup', 200, 350);
    ui.click('swipe-btn');
    ui.pointer('cam', 'pointerdown', 100, 200);
    ui.pointer('cam', 'pointermove', 100, 400);

    ui.pointer('cam', event, 100, 400);
    ui.pointer('cam', 'pointerup', 100, 400);
    await ui.flush();

    assert.equal(ui.requests.filter(r => r.body?.tool === 'swipe').length, 0);
    assert.equal(ui.el('cam-wrap').querySelector('.arrow').style.display, 'none');
  });
}

test('test_state_failure_marks_connection_offline', async () => {
  const ui = await createUI();
  ui.routes.set('/api/state', {status: 'error', message: 'state unavailable'});

  await ui.header.onclick();

  assert.equal(ui.el('conn-label').textContent, 'not connected');
});

test('test_launch_refusal_without_message_has_fallback', async () => {
  const ui = await createUI();
  ui.routes.set('/api/macro', {status: 'error'});

  await ui.click('mc-step');

  assert.equal(ui.el('banner').textContent, 'request refused');
  assert.equal(ui.el('mc-step').disabled, false);
});

test('test_capture_loss_after_completed_selection_keeps_selection', async () => {
  const ui = await createUI();
  ui.pointer('cam', 'pointerdown', 40, 70);
  ui.pointer('cam', 'pointermove', 200, 350);
  ui.pointer('cam', 'pointerup', 200, 350);

  ui.pointer('cam', 'lostpointercapture', 200, 350);

  assert.equal(ui.el('cap-geo').textContent, '[0.10, 0.10, 0.50, 0.50]');
});

test('test_macro_shortcut_opens_requested_macro_when_idle', async () => {
  const ui = await createUI();
  ui.choose('mc-select', 'second');

  ui.el('route').children[0].querySelector('.g').onclick({stopPropagation() {}});

  assert.equal(ui.el('mc-select').value, 'first');
  assert.equal(ui.input('mc').value, 'first default');
  assert.equal(ui.el('mc').classList.contains('on'), true);
});

test('test_regression_staged_reply_is_appended_as_plain_text', async () => {
  const ui = await createUI();
  const reply = '<img src=x onerror="document.title=\'injected\'"> & <b>literal</b>';
  ui.routes.get('/api/playbooks').packs[0].playbooks[0].position = {
    idx: 0, node: 'open (1/2)', quoted: null, consented: null, staged: [reply],
  };

  await ui.click('pb-reset');

  assert.equal(ui.el('pb-pos').lastChild.tagName, '#TEXT');
  assert.equal(ui.el('pb-pos').lastChild.textContent, ' · staged: ' + reply);
});
