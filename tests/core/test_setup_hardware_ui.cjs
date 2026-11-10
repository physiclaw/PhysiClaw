const assert = require('node:assert/strict');
const {test} = require('node:test');
const {createSetup} = require('./setup_ui_fixture.cjs');

const posts = ui => ui.requests.filter(request => request.method === 'POST');
const calls = (ui, url) => posts(ui).filter(request => request.url === url);

for (const language of ['en', 'zh']) {
  for (const noRuntime of [false, true]) {
    test(`setup completion shows the next step for the active mode (${language}, noRuntime=${noRuntime})`, async () => {
      const ui = await createSetup({language, noRuntime});
      await ui.startAuto();

      assert.equal(ui.status.ready, true);
      const text = ui.el('panel').textContent;
      const mcp = /MCP:/;
      const learning = language === 'en' ? /learn your chat app and keyboard layout/ : /熟悉聊天应用和键盘布局/;
      assert.match(text, noRuntime ? mcp : learning);
      assert.doesNotMatch(text, noRuntime ? learning : mcp);
    });
  }
}

test('MCP completion shows the current server address, including a custom port', async () => {
  const ui = await createSetup({noRuntime: true, initial: {ready: true},
    origin: 'http://[::1]:9123'});

  assert.equal(ui.el('mcpAddress').textContent, 'http://[::1]:9123/mcp');
  assert.equal(posts(ui).length, 0);
});

test('MCP indicator waits for a live response before showing connected', async () => {
  const pending = Promise.withResolvers();
  let probes = 0;
  const ui = await createSetup({noRuntime: true, responses: {
    '/api/status': () => ++probes === 1 ? {ready: true} : pending.promise,
  }});
  const indicator = ui.el('mcpStatus').closest('.mcp-connect');

  assert.equal(indicator.dataset.state, 'checking');
  assert.equal(ui.el('mcpStatus').textContent, 'Checking…');
  pending.resolve({ready: true});
  await ui.flush();

  assert.equal(indicator.dataset.state, 'online');
  assert.equal(ui.el('mcpStatus').textContent, 'Online');
  assert.ok(!indicator.title);
  assert.ok(!indicator.querySelector('.mcp-label').title);
  assert.ok(!ui.el('mcpAddress').title);
  assert.ok(ui.requests.every(request => request.cache === 'no-store'));
  assert.equal(posts(ui).length, 0);
});

for (const [failure, response] of [
  ['network error', () => { throw new TypeError('Failed to fetch'); }],
  ['HTTP error', () => ({error: 'Service unavailable'})],
  ['unexpected response', () => ({message: 'A different service'})],
]) {
  test(`MCP indicator recovers after ${failure}, including a server awaiting setup`, async () => {
    const ui = await createSetup({noRuntime: true, initial: {ready: true}, language: 'zh'});
    const indicator = ui.el('mcpStatus').closest('.mcp-connect');
    ui.overrides.set('/api/status', response);

    await ui.advance(3000);
    assert.equal(indicator.dataset.state, 'offline');
    assert.equal(ui.el('mcpStatus').textContent, '离线');

    ui.overrides.delete('/api/status');
    ui.status.ready = false;
    await ui.advance(3000);
    assert.equal(indicator.dataset.state, 'waiting');
    assert.equal(ui.el('mcpStatus').textContent, '待设置');

    ui.status.ready = true;
    await ui.advance(3000);
    assert.equal(indicator.dataset.state, 'online');
    assert.equal(posts(ui).length, 0);
  });
}

test('a stalled MCP probe times out without overlapping requests and then recovers', async () => {
  const ui = await createSetup({noRuntime: true, initial: {ready: true}});
  const indicator = ui.el('mcpStatus').closest('.mcp-connect');
  ui.overrides.set('/api/status', () => new Promise(() => {}));

  await ui.advance(3000);
  const count = ui.requests.length;
  await ui.advance(1999);
  assert.equal(ui.requests.length, count);
  assert.equal(indicator.dataset.state, 'online');
  await ui.advance(1);
  assert.equal(indicator.dataset.state, 'offline');

  ui.overrides.delete('/api/status');
  await ui.advance(3000);
  assert.equal(indicator.dataset.state, 'online');
});

test('MCP polling ignores stale responses after a language change and stops when leaving Finish', async () => {
  const ui = await createSetup({noRuntime: true, initial: {ready: true}});
  const oldStatus = ui.el('mcpStatus');
  const pending = Promise.withResolvers();
  ui.overrides.set('/api/status', () => pending.promise);
  await ui.advance(3000);

  ui.overrides.delete('/api/status');
  ui.click('langToggle');
  await ui.flush();
  pending.resolve({ready: false});
  await ui.flush();
  assert.equal(oldStatus.textContent, 'Online');
  assert.equal(ui.el('mcpStatus').textContent, '在线');
  const count = ui.requests.length;
  await ui.advance(3000);
  assert.equal(ui.requests.length, count + 1);

  await ui.el('stepList').children[1].onclick();
  const afterLeaving = ui.requests.length;
  await ui.advance(10000);
  assert.equal(ui.requests.length, afterLeaving);
  assert.equal(posts(ui).length, 0);
});

test('camera coverage advice appears in setup without blocking calibration', async () => {
  const ui = await createSetup();
  const issue = 'Move camera closer — phone covers only 23% of image (need ≥25%)';
  const pending = Promise.withResolvers();
  ui.overrides.set('/api/calibrate/camera', () => ({
    status: 'ok', setup_ok: false, rotation_name: '90°', coverage: 0.23, issues: [issue],
  }));
  ui.overrides.set('/api/calibrate/camera-mapping', () => pending.promise);

  await ui.startAuto();

  assert.equal(ui.state().current, 6);
  assert.ok(ui.el('panel').querySelector('.note.warn').textContent.includes(issue));
  assert.equal(ui.el('panel').querySelectorAll('.note.err').length, 0);
  assert.equal(calls(ui, '/api/calibrate/camera-mapping').length, 1);

  ui.status.steps.mapping_b = 'OK';
  pending.resolve({status: 'ok', dots: 15});
  await ui.drain();
  assert.equal(ui.status.ready, true);
});

test('camera mapping can finish exposure recovery after thirty seconds', async () => {
  const ui = await createSetup();
  const pending = Promise.withResolvers();
  ui.overrides.set('/api/calibrate/camera-mapping', () => pending.promise);
  await ui.startAuto();

  await ui.advance(45000);

  assert.equal(ui.state().current, 6);
  assert.equal(ui.state().working, true);
  assert.equal(calls(ui, '/api/calibrate/camera-mapping').length, 1);
  assert.equal(ui.el('panel').querySelectorAll('.note.err').length, 0);
  ui.status.steps.mapping_b = 'OK';
  pending.resolve({status: 'ok', dots: 15});
  await ui.drain();
  assert.equal(ui.status.ready, true);
});

test('stalled camera mapping times out without retrying or advancing', async () => {
  const ui = await createSetup();
  ui.overrides.set('/api/calibrate/camera-mapping', () => new Promise(() => {}));
  await ui.startAuto();

  await ui.advance(60000);
  await ui.drain();

  assert.equal(ui.state().current, 6);
  assert.equal(ui.state().auto, false);
  assert.equal(ui.state().working, false);
  assert.equal(calls(ui, '/api/calibrate/camera-mapping').length, 1);
  assert.equal(calls(ui, '/api/calibrate/validate').length, 0);
  assert.match(ui.el('panel').querySelector('.note.err').textContent, /timed out/);
});

test('saved calibration completes the automatic setup in order, once per action', async () => {
  const ui = await createSetup();

  await ui.startAuto();

  assert.equal(ui.state().current, 9);
  assert.equal(ui.status.ready, true);
  assert.deepEqual(posts(ui).map(request => request.url), [
    '/api/connect-arm', '/api/connect-camera', '/api/bridge/switch',
    '/api/calibrate/viewport-shift', '/api/bridge/switch', '/api/calibrate/arm',
    '/api/calibrate/camera', '/api/calibrate/camera-mapping', '/api/calibrate/validate',
    '/api/calibrate/assistive-touch/show', '/api/calibrate/assistive-touch/verify',
    '/api/phone/home', '/api/ready',
  ]);
  assert.equal(calls(ui, '/api/calibrate/arm')[0].body.from_park, true);
  assert.match(ui.el('panel').textContent, /PhysiClaw is ready/);
});

for (const language of ['en', 'zh']) {
  test(`first calibration pauses for positioning and resumes after confirmation (${language})`, async () => {
    const ui = await createSetup({saved: false, language});

    await ui.startAuto();

    assert.equal(ui.state().current, 5);
    assert.equal(ui.state().auto, false);
    assert.equal(calls(ui, '/api/calibrate/arm').length, 1);
    assert.equal(ui.el('panel').querySelectorAll('.note.err').length, 0);
    assert.match(ui.el('panel').textContent, language === 'en' ? /No saved arm calibration.*click Start/ : /没有找到之前的机械臂校准记录.*开始/);
    assert.equal(ui.current().textContent, language === 'en' ? 'Start' : '开始');

    await ui.current().click();
    assert.equal(calls(ui, '/api/calibrate/arm').length, 2);
    assert.equal(calls(ui, '/api/calibrate/arm')[1].body, null);
    assert.equal(ui.current().textContent, language === 'en' ? 'Continue' : '继续');
    await ui.current().click();
    await ui.drain();

    assert.equal(ui.status.ready, true);
    assert.equal(calls(ui, '/api/calibrate/arm').length, 2);
  });
}

for (const [url, step] of [
  ['/api/connect-arm', 2], ['/api/connect-camera', 3], ['/api/calibrate/viewport-shift', 4],
  ['/api/calibrate/arm', 5], ['/api/calibrate/camera', 6], ['/api/calibrate/camera-mapping', 6],
  ['/api/calibrate/validate', 7], ['/api/calibrate/assistive-touch/verify', 8],
]) {
  test(`failure at ${url} stops without retrying or advancing`, async () => {
    const ui = await createSetup();
    ui.overrides.set(url, () => ({status: 'error', message: 'Fixture failure'}));

    await ui.startAuto();

    assert.equal(ui.state().current, step);
    assert.equal(ui.state().auto, false);
    assert.equal(ui.state().working, false);
    assert.equal(calls(ui, url).length, 1);
    assert.equal(ui.el('panel').querySelectorAll('.note.err').length, 1);
    assert.equal(calls(ui, '/api/ready').length, 0);

    ui.overrides.delete(url);
    const retry = ui.current().click();
    await ui.drain();
    await retry;
    if (ui.current()?.textContent === 'Continue') await ui.current().click();
    await ui.drain();
    assert.equal(ui.status.ready, true, 'Successful manual retry should resume Auto mode');
    assert.equal(calls(ui, url).length, 2);
  });
}

test('slow action stays locked and ignores repeated clicks and back navigation', async () => {
  const ui = await createSetup();
  const pending = Promise.withResolvers();
  ui.overrides.set('/api/calibrate/arm', () => pending.promise);

  await ui.startAuto();

  assert.equal(ui.state().current, 5);
  assert.equal(ui.state().working, true);
  const button = ui.el('go');
  button.click(); button.onclick();
  ui.el('panel').querySelector('.ghost').click();
  ui.el('stepList').children[0].onclick();
  ui.click('langToggle');
  await ui.drain();
  assert.equal(ui.state().current, 5);
  assert.equal(ui.state().working, true);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 1);

  ui.status.steps.mapping_a = 'OK';
  pending.resolve({status: 'ok', pairs: 18, aligned: true});
  await ui.drain();
  assert.equal(ui.status.ready, true);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 1);
});

test('calibration waits for the phone target before starting any probes', async () => {
  const ui = await createSetup();
  const pending = Promise.withResolvers();
  ui.overrides.set('/api/bridge/switch', request => request.body.phase === 'center'
    ? pending.promise : {ok: true});

  await ui.startAuto();

  assert.equal(ui.state().current, 5);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 0);
  pending.resolve({ok: true});
  await ui.drain();
  assert.equal(ui.status.ready, true);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 1);
});

test('hardware failure displays the cause without unrelated positioning advice', async () => {
  const ui = await createSetup();
  ui.overrides.set('/api/calibrate/arm', () => ({status: 'error', message: 'Serial connection lost'}));

  await ui.startAuto();

  assert.match(ui.el('panel').querySelector('.note.err').textContent, /Arm calibration failed: Serial connection lost/);
  assert.doesNotMatch(ui.el('panel').querySelector('.note.err').textContent, /center circle/);
});

test('a slow status refresh cannot repeat a completed arm action', async () => {
  const ui = await createSetup();
  const pending = Promise.withResolvers();
  let released = false;
  ui.overrides.set('/api/status', () => !released && ui.status.steps.mapping_a
    ? pending.promise : structuredClone(ui.status));

  await ui.startAuto();

  assert.equal(ui.state().current, 5);
  assert.equal(ui.state().working, true);
  ui.el('go').onclick();
  await ui.drain();
  assert.equal(calls(ui, '/api/calibrate/arm').length, 1);
  released = true;
  pending.resolve(structuredClone(ui.status));
  await ui.drain();
  assert.equal(ui.status.ready, true);
});

for (const [url, step, body, downstream] of [
  ['/api/bridge/switch', 4, {error: 'Target unavailable'}, '/api/calibrate/viewport-shift'],
  ['/api/calibrate/assistive-touch/show', 8, {status: 'error', message: 'Target unavailable'}, '/api/calibrate/assistive-touch/verify'],
  ['/api/calibrate/validate', 7, {status: 'ok', calibrated: false, passed: 2, total: 5}, '/api/ready'],
  ['/api/calibrate/assistive-touch/verify', 8, {status: 'ok', passed: false}, '/api/ready'],
]) {
  test(`unsuccessful result at ${url} blocks downstream actions`, async () => {
    const ui = await createSetup();
    ui.overrides.set(url, () => body);

    await ui.startAuto();

    assert.equal(ui.state().current, step);
    assert.equal(ui.state().auto, false);
    assert.equal(ui.el('panel').querySelectorAll('.note.err').length, 1);
    assert.equal(calls(ui, downstream).length, 0);
  });
}

test('a failed target preparation can be retried before manual positioning', async () => {
  const ui = await createSetup();
  ui.overrides.set('/api/bridge/switch', request => request.body.phase === 'center'
    ? {error: 'Center target unavailable'} : {ok: true});
  await ui.startAuto();
  assert.equal(calls(ui, '/api/calibrate/arm').length, 0);
  assert.match(ui.el('panel').querySelector('.note.err').textContent, /Center target unavailable/);

  ui.overrides.delete('/api/bridge/switch');
  await ui.current().click();
  assert.equal(calls(ui, '/api/calibrate/arm')[0].body, null);
  await ui.current().click();
  await ui.drain();
  assert.equal(ui.status.ready, true);
});

test('Back leaves Auto mode and cancels automatic continuation', async () => {
  const ui = await createSetup({saved: false});
  await ui.startAuto();

  await ui.el('panel').querySelector('.ghost').click();
  assert.equal(ui.state().current, 4);
  assert.equal(ui.state().armed, false);
  const measure = ui.current().click();
  await ui.drain();
  await measure;
  assert.equal(ui.state().current, 5);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 1);
});

test('manual setup advances only on its own confirmations', async () => {
  const ui = await createSetup({saved: false});
  for (let i = 0; i < 15 && !ui.status.ready; i++) {
    const confirm = ui.current().click();
    await ui.drain();
    await confirm;
    assert.equal(ui.state().auto, false);
  }
  assert.equal(ui.status.ready, true);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 1);
  assert.equal(calls(ui, '/api/calibrate/arm')[0].body, null);
});

test('failure to return home prevents readiness and displays its cause', async () => {
  const ui = await createSetup();
  ui.overrides.set('/api/phone/home', () => ({error: 'Arm jammed'}));

  await ui.startAuto();

  assert.equal(calls(ui, '/api/ready').length, 0);
  assert.equal(ui.status.ready, false);
  assert.match(ui.el('panel').querySelector('.note.err').textContent, /Arm jammed/);
});

test('readiness acknowledgement alone does not show success', async () => {
  const ui = await createSetup();
  ui.overrides.set('/api/ready', () => ({ok: true}));

  await ui.startAuto();

  assert.equal(ui.status.ready, false);
  assert.match(ui.el('panel').querySelector('.note.err').textContent, /Couldn't confirm that PhysiClaw is ready/);
});

test('an arm request timeout stops Auto mode without another probe', async () => {
  const ui = await createSetup();
  ui.overrides.set('/api/calibrate/arm', () => new Promise(() => {}));
  await ui.startAuto();

  await ui.advance(130000);
  await ui.drain();

  assert.equal(ui.state().current, 5);
  assert.equal(ui.state().auto, false);
  assert.equal(ui.state().working, false);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 1);
  assert.match(ui.el('panel').querySelector('.note.err').textContent, /timed out/);
});

test('Back waits for the previous target request and ignores stale Start handlers', async () => {
  const pending = Promise.withResolvers();
  const ui = await createSetup({
    initial: {arm: true, camera: true, steps: {viewport_shift: 'OK'}},
    responses: {'/api/bridge/switch': request => request.body.phase === 'center' ? pending.promise : {ok: true}},
  });
  const oldStart = ui.el('go').onclick;

  const back = ui.el('panel').querySelector('.ghost').click();
  await ui.flush();
  await oldStart();
  assert.deepEqual(calls(ui, '/api/bridge/switch').map(request => request.body.phase), ['center']);
  pending.resolve({ok: true});
  await back;
  await ui.flush();

  assert.equal(ui.state().current, 4);
  assert.equal(calls(ui, '/api/calibrate/arm').length, 0);
  assert.deepEqual(calls(ui, '/api/bridge/switch').map(request => request.body.phase), ['center', 'screenshot_cal']);
});

test('leaving Finish during the home request cancels its readiness request', async () => {
  const ui = await createSetup();
  const pending = Promise.withResolvers();
  ui.overrides.set('/api/phone/home', () => pending.promise);
  await ui.startAuto();
  assert.equal(ui.state().current, 9);

  await ui.el('stepList').children[0].onclick();
  pending.resolve({ok: true});
  await ui.drain();

  assert.equal(ui.state().current, 0);
  assert.equal(calls(ui, '/api/ready').length, 0);
});

test('opening completed setup does not repeat any hardware action', async () => {
  const ui = await createSetup({initial: {ready: true}});
  await ui.drain();

  assert.equal(ui.state().current, 9);
  assert.match(ui.el('panel').textContent, /PhysiClaw is ready/);
  assert.equal(posts(ui).length, 0);
});
