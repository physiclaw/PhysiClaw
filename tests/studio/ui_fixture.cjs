// Run the complete inline script with deterministic DOM, HTTP and timer boundaries.
// This DOM double covers event/state tests; real-browser checks cover layout/input.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {setImmediate} = require('node:timers/promises');

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.value = '';
    this.disabled = false;
    this.listeners = new Map();
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      toggle: (name, on) => {
        const classes = new Set(this.className.split(' ').filter(Boolean));
        if (on ?? !classes.has(name)) classes.add(name); else classes.delete(name);
        this.className = [...classes].join(' ');
      },
      add: name => this.classList.toggle(name, true),
      remove: name => this.classList.toggle(name, false),
    };
  }
  set innerHTML(value) { this.children = []; this._text = value; }
  get innerHTML() { return this._text || ''; }
  set textContent(value) { this.children = []; this._text = value; }
  get textContent() { return (this._text || '') + this.children.map(c => c.textContent).join(''); }
  get lastChild() { return this.children.at(-1); }
  get options() { return this.children; }
  appendChild(child) {
    if (typeof child !== 'object') { const node = new Element('#text'); node.textContent = child; child = node; }
    child.parentElement = this;
    this.children.push(child);
    if (this.tagName === 'SELECT' && (this.children.length === 1 || child.selected)) this.value = child.value;
    return child;
  }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); }
  setAttribute(name, value) { this[name === 'class' ? 'className' : name] = String(value); }
  getAttribute(name) { return this[name === 'class' ? 'className' : name]; }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (matches(node, selector)) return node;
    return null;
  }
  scrollIntoView() {}
  prepend(child) { this.appendChild(child); this.children.unshift(this.children.pop()); }
  remove() { this.parentElement.children = this.parentElement.children.filter(c => c !== this); }
  addEventListener(name, handler) { this.listeners.set(name, handler); }
  setPointerCapture(id) { this.pointerId = id; }
  getBoundingClientRect() { return {left: 0, top: 0, width: 400, height: 700}; }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [child, ...child.descendants()]).filter(el => matches(el, selector));
  }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function matches(el, selector) {
  if (selector.includes(',')) return selector.split(',').some(s => matches(el, s.trim()));
  if (selector === 'button:not(.tabs button)')
    return el.tagName === 'BUTTON' && !el.parentElement.classList.contains('tabs');
  const parts = selector.split(' ');
  if (parts.length > 1) {
    if (!matches(el, parts.pop())) return false;
    for (let parent = el.parentElement; parent; parent = parent.parentElement)
      if (matches(parent, parts.join(' '))) return true;
    return false;
  }
  if (selector.startsWith('#')) return el.id === selector.slice(1);
  if (selector.endsWith(':not([hidden])')) return !el.hidden && matches(el, selector.slice(0, -':not([hidden])'.length));
  if (selector.startsWith('.')) return selector.slice(1).split('.').every(c => el.classList.contains(c));
  const match = selector.match(/^(\w+)?(?:\[data-(\w+)\])?$/);
  if (!match) throw new Error('Unsupported fixture selector: ' + selector);
  return (!match[1] || el.tagName === match[1].toUpperCase()) && (!match[2] || match[2] in el.dataset);
}

function documentFrom(html) {
  const root = new Element('document'), stack = [root];
  const markup = html.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/g, '').replace(/<!--[\s\S]*?-->/g, '');
  for (const token of markup.match(/<[^>]*>|[^<]+/g)) {
    if (token.startsWith('<!')) continue;
    if (token.startsWith('</')) { stack.pop(); continue; }
    if (!token.startsWith('<')) { stack.at(-1).append(token); continue; }
    const tag = token.match(/^<(\w+)/)[1];
    const element = new Element(tag);
    for (const [, name, value] of token.matchAll(/([\w-]+)="([^"]*)"/g)) {
      if (name.startsWith('data-')) element.dataset[name.slice(5)] = value;
      else element[name === 'class' ? 'className' : name] = value;
    }
    stack.at(-1).appendChild(element);
    if (!['input', 'img', 'br', 'meta', 'link', 'hr'].includes(tag)) stack.push(element);
  }
  root.getElementById = id => root.querySelector('#' + id);
  root.createElement = tag => new Element(tag);
  return root;
}

async function createUI() {
  const filename = path.join(__dirname, '../../src/physiclaw/studio/studio.html');
  const html = fs.readFileSync(filename, 'utf8');
  const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/studio_ui.json'), 'utf8'));
  const document = documentFrom(html), timers = new Map(), requests = [], responses = new Map();
  const routes = new Map([
    ['/api/state', {status: 'ok', connected: true, tools: ['peek'], mcp_url: 'fixture'}],
    ['/api/act', {status: 'ok', text: 'fixture action'}],
    ['/api/playbooks', {status: 'ok', packs: catalog.packs}],
    ['/api/macros', {status: 'ok', macros: catalog.macros}],
    ['/api/step', {status: 'ok', running: false}],
    ['/api/macro', {status: 'ok'}],
    ['/api/step/reset', {status: 'ok', message: 'reset'}],
  ]);
  const context = vm.createContext({
    document, window: new Element('window'), console,
    Option: function(text, value, _, selected) {
      const option = new Element('option');
      Object.assign(option, {textContent: text, value, selected});
      return option;
    },
    fetch: async (url, init) => {
      const request = {url, body: init ? JSON.parse(init.body) : undefined};
      requests.push(request);
      const route = routes.get(url.split('?')[0]);
      if (route === undefined) throw new Error('Unexpected fixture request: ' + url);
      const data = typeof route === 'function' ? await route(request) : route;
      const response = responses.get(url.split('?')[0]) || {status: 200};
      return {ok: response.status < 400, status: response.status, json: async () => {
        if (response.unreadable) throw new SyntaxError('invalid JSON');
        return structuredClone(data);
      }};
    },
    setInterval: callback => { const id = Symbol(); timers.set(id, callback); return id; },
    clearInterval: id => timers.delete(id),
    requestAnimationFrame: callback => callback(),
    confirm: () => true,
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/);
  const padding = '\n'.repeat(html.slice(0, script.index).split('\n').length - 1);
  vm.runInContext(padding + script[1], context, {filename});
  await setImmediate();
  return {
    el: id => document.getElementById(id),
    input: panel => document.getElementById(panel + '-inputs').querySelector('input[data-input]'),
    click: id => document.getElementById(id).onclick(),
    choose: (id, value) => { const el = document.getElementById(id); el.value = value; el.onchange(); },
    header: document.querySelector('header button[data-tool]'),
    routes, requests, responses,
    pointer: (pane, event, x, y, id = 1) => document.getElementById(pane + '-wrap').listeners.get(event)?.({
      clientX: x, clientY: y, pointerId: id, button: 0, isPrimary: true, preventDefault() {},
    }),
    poll: async () => { await Promise.all([...timers.values()].map(tick => tick())); await setImmediate(); },
    flush: setImmediate,
    timers,
  };
}

module.exports = {createUI, documentFrom};
