const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {setImmediate} = require('node:timers/promises');
const {documentFrom} = require('../studio/ui_fixture.cjs');

// Run the complete wizard script with deterministic DOM, HTTP and timers.
// Reuse the Studio DOM double, adding template and button behavior.
const prototype = Object.getPrototypeOf(documentFrom('<div></div>').children[0]);
const queryAll = prototype.querySelectorAll;
prototype.querySelectorAll = function(selector) {
  if (selector === '[data-enter]:not([disabled])')
    return queryAll.call(this, '[data-enter]').filter(node => !node.disabled);
  return queryAll.call(this, selector);
};
prototype.click = function() { if (!this.disabled) return this.onclick?.(); };
prototype.insertBefore = function(child, before) {
  child.parentElement = this;
  const index = this.children.indexOf(before);
  this.children.splice(index < 0 ? this.children.length : index, 0, child);
  return child;
};
prototype.replaceWith = function(child) {
  const parent = this.parentElement;
  child.parentElement = parent;
  parent.children.splice(parent.children.indexOf(this), 1, child);
};
Object.defineProperties(prototype, {
  firstChild: {get() { return this.children[0]; }},
  offsetParent: {get() { return this.parentElement; }},
  innerHTML: {
    get() { return this._html || this._text || ''; },
    set(html) {
      this._html = html; this._text = ''; this.children = [];
      if (html) this.append(...documentFrom(html).children);
    },
  },
});

async function createSetup({saved = true, language = 'en', initial = {}, responses = {}} = {}) {
  const filename = path.join(__dirname, '../../src/physiclaw/core/static/setup-hardware.html');
  const html = fs.readFileSync(filename, 'utf8');
  const document = documentFrom(html);
  document.body = document.querySelector('body');
  document.documentElement = document.querySelector('html');
  const createElement = document.createElement;
  document.createElement = tag => tag !== 'template' ? createElement(tag) : {
    set innerHTML(value) {
      const root = documentFrom(value).children[0];
      if (/^[^>]*\bdata-enter(?:\s|>)/.test(value)) root.dataset.enter = '';
      if (/^[^>]*\bdisabled(?:\s|>)/.test(value)) root.disabled = true;
      this.content = {firstChild: root};
    },
  };
  const requests = [], overrides = new Map(Object.entries(responses)), timers = new Map();
  const status = {bridge: true, arm: false, camera: false, steps: {}, ready: false, ...initial};
  const routes = new Map([
    ['/api/status', () => structuredClone(status)],
    ['/api/bridge/switch', () => ({ok: true})],
    ['/api/connect-arm', () => { status.arm = true; return {status: 'ok'}; }],
    ['/api/connect-camera', () => { status.camera = true; return {status: 'ok', index: 0}; }],
    ['/api/calibrate/viewport-shift', () => { status.steps.viewport_shift = 'OK'; return {status: 'ok'}; }],
    ['/api/calibrate/arm', request => {
      if (!saved && request.body?.from_park) return {status: 'needs_input', message: 'No saved arm calibration'};
      status.steps.mapping_a = 'OK';
      return {status: 'ok', pairs: 18, aligned: true};
    }],
    ['/api/calibrate/camera', () => ({status: 'ok', rotation_name: 'none', coverage: 0.9})],
    ['/api/calibrate/camera-mapping', () => { status.steps.mapping_b = 'OK'; return {status: 'ok', dots: 15}; }],
    ['/api/calibrate/validate', () => { status.steps.validated = true; return {status: 'ok', calibrated: true, passed: 5, total: 5}; }],
    ['/api/calibrate/assistive-touch/show', () => ({status: 'ok'})],
    ['/api/calibrate/assistive-touch/verify', () => { status.steps.assistive_touch = 'OK'; return {status: 'ok', passed: true, clipboard: {}}; }],
    ['/api/phone/home', () => ({status: 'ok'})],
    ['/api/ready', () => { status.ready = true; return {ok: true}; }],
  ]);
  let now = Date.now();
  const context = vm.createContext({
    document, window: {}, navigator: {language}, console, AbortController,
    localStorage: {getItem: () => null, setItem() {}},
    Date: class extends Date {static now() { return now; }},
    setTimeout: (callback, delay = 0) => {
      const id = Symbol(); timers.set(id, {callback, at: now + delay}); return id;
    },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      const request = {url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null};
      requests.push(request);
      const route = overrides.get(url) || routes.get(url);
      if (!route) throw new Error('Unexpected setup request: ' + url);
      let abort;
      const aborted = new Promise((_, reject) => {
        abort = () => reject(new DOMException('Aborted', 'AbortError'));
        options.signal?.addEventListener('abort', abort, {once: true});
      });
      try {
        const body = await Promise.race([Promise.resolve().then(() => route(request)), aborted]);
        const code = body.status === 'needs_input' ? 409 : body.status === 'error' || body.error ? 500 : 200;
        return {ok: code < 400, status: code, text: async () => JSON.stringify(body)};
      } finally {
        options.signal?.removeEventListener('abort', abort);
      }
    },
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context, {filename});
  const flush = async () => { await setImmediate(); await setImmediate(); };
  await flush();
  return {
    document, requests, overrides, status, flush,
    el: id => document.getElementById(id),
    state: () => vm.runInContext('({current: CURRENT, auto: AUTO, armed: AUTO_ARMED, working: WORKING})', context),
    current: () => document.getElementById('panel').querySelector('[data-enter]:not([disabled])'),
    click: id => document.getElementById(id).click(),
    async advance(ms) {
      const end = now + ms;
      while (true) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break;
        now = next[1].at;
        timers.delete(next[0]); next[1].callback();
        await flush();
      }
      now = end;
      await flush();
    },
    async drain() {
      for (let i = 0; i < 200; i++) {
        await flush();
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at - now > 5000) return;
        now = next[1].at;
        timers.delete(next[0]); next[1].callback();
      }
      throw new Error('Setup did not settle');
    },
    async startAuto() {
      document.getElementById('autoBtn').click();
      await document.getElementById('nextBtn').click();
      await this.drain();
    },
  };
}

module.exports = {createSetup};
