const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function focusRenderer(width, height) {
  const html = fs.readFileSync(path.join(__dirname, '../../src/physiclaw/core/setup/static/bridge.html'), 'utf8');
  const start = html.indexOf('function renderCalibrate(data)');
  const end = html.indexOf('function fitTextSize()', start);
  class Element {
    constructor(name) { this.name = name; this.attributes = {}; this.children = []; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    appendChild(child) { this.children.push(child); }
  }
  const svg = new Element('svg');
  svg.namespaceURI = 'http://www.w3.org/2000/svg';
  const context = {
    W: width, H: height, svg, lastCalKey: null, phaseLabel: {}, clears: 0,
    document: {createElementNS(namespace, name) {
      assert.equal(namespace, svg.namespaceURI);
      return new Element(name);
    }},
    clearSvg() { context.clears++; svg.children = []; },
    rect(x, y, w, h, fill) {
      const rectangle = new Element('rect');
      for (const [name, value] of Object.entries({x, y, width: w, height: h, fill}))
        rectangle.setAttribute(name, value);
      svg.appendChild(rectangle);
    },
    circle(cx, cy, r, fill) {
      const dot = new Element('circle');
      for (const [name, value] of Object.entries({cx, cy, r, fill}))
        dot.setAttribute(name, value);
      svg.appendChild(dot);
    },
  };
  vm.createContext(context);
  vm.runInContext(html.slice(start, end), context);
  return context;
}

test('focus checker and tinted wordmark share the same reusable tile geometry', () => {
  const renderer = focusRenderer(430, 775);
  renderer.renderCalibrate({phase: 'focus'});
  const [defs, rectangle, brand] = renderer.svg.children;
  const [pattern, brandPattern] = defs.children;
  assert.equal(defs.name, 'defs');
  assert.equal(pattern.name, 'pattern');
  assert.equal(pattern.attributes.patternUnits, 'userSpaceOnUse');
  assert.equal(Number(pattern.attributes.width), 8.6);
  assert.equal(Number(pattern.attributes.height), 8.6);
  assert.equal(pattern.children.length, 1);
  assert.equal(pattern.children[0].name, 'path');
  assert.equal(rectangle.attributes.fill, `url(#${pattern.attributes.id})`);
  assert.equal(Number(rectangle.attributes.width), 430);
  assert.equal(Number(rectangle.attributes.height), 775);
  assert.equal(brandPattern.attributes.width, pattern.attributes.width);
  assert.equal(brandPattern.attributes.height, pattern.attributes.height);
  assert.equal(brandPattern.attributes.patternUnits, pattern.attributes.patternUnits);
  assert.equal(brandPattern.children[0].attributes.d, pattern.children[0].attributes.d);
  assert.equal(brandPattern.children[0].attributes.fill, '#0055ff');
  assert.equal(brand.name, 'path');
  assert.equal(brand.attributes.fill, `url(#${brandPattern.attributes.id})`);
});

test('leaving focus removes the blue wordmark before dot-grid calibration', () => {
  const renderer = focusRenderer(430, 775);
  renderer.renderCalibrate({phase: 'focus'});
  assert.equal(renderer.svg.children.length, 3);

  renderer.renderCalibrate({phase: 'grid'});
  assert.equal(renderer.svg.children.length, 15);
  for (const dot of renderer.svg.children) {
    assert.equal(dot.name, 'circle');
    assert.equal(dot.attributes.fill, '#ef4444');
  }

  renderer.renderCalibrate({phase: 'focus'});
  assert.equal(renderer.svg.children.length, 3);
  assert.equal(renderer.svg.children[0].children[1].children[0].attributes.fill, '#0055ff');
});

test('unchanged polls reuse the target and viewport changes rebuild it', () => {
  const renderer = focusRenderer(430, 775);
  for (let index = 0; index < 20; index++) renderer.renderCalibrate({phase: 'focus'});
  assert.equal(renderer.clears, 1);
  renderer.W = 600;
  renderer.renderCalibrate({phase: 'focus'});
  assert.equal(renderer.clears, 2);
  assert.equal(renderer.svg.children.length, 3);
  assert.equal(Number(renderer.svg.children[0].children[0].attributes.width), 12);
});
