// Execute the actual capture function against explicit observed DOM/CSS state.
// This fixture has no layout engine: rects and computed style are the witnesses
// supplied by the test, so ARIA attributes must never override those witnesses.
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(tag, attrs = {}, parent = null, style = {}, rect = {width: 259, height: 50}) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs; this.parentElement = parent;
    this.styleWitness = {display: 'block', visibility: 'visible', opacity: '1', ...style};
    this.rectWitness = rect; this.id = attrs.id || ''; this.className = attrs.class || '';
    this.type = attrs.type || (tag === 'select' ? 'select-one' : ''); this.readOnly = false;
    this.multiple = false; this.hidden = Object.hasOwn(attrs, 'hidden'); this.labels = [];
    this.textContent = ''; this.value = 'PRIVATE-ENTERED-VALUE';
  }
  getAttribute(key) { return Object.hasOwn(this.attrs, key) ? this.attrs[key] : null; }
  hasAttribute(key) { return Object.hasOwn(this.attrs, key); }
  getClientRects() { return this.rectWitness ? [this.rectWitness] : []; }
  matches(selector) {
    return selector.split(',').some(part => {
      part = part.trim();
      if (part === ':disabled') return this.hasAttribute('disabled');
      if (part.startsWith('[id^=product-]')) return this.id.startsWith('product-');
      if (part.startsWith('[')) {
        const match = part.match(/^\[([^=\]]+)(?:=['"]?([^'"\]]+)['"]?)?\]$/);
        return !!match && this.hasAttribute(match[1]) && (match[2] === undefined || this.getAttribute(match[1]) === match[2]);
      }
      if (part.startsWith('#')) return this.id === part.slice(1);
      const match = part.match(/^(\w+)?(?:\.([\w-]+))?$/);
      return !!match && (!match[1] || this.tagName.toLowerCase() === match[1])
        && (!match[2] || this.className.split(/\s+/).includes(match[2]));
    });
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelector() { return null; }
  querySelectorAll(selector) {
    if (selector.startsWith('select,input')) return controls;
    return [];
  }
}
const body = new Element('body', {class:'single-product postid-6383'});
const main = new Element('main', {}, body);
const product = new Element('div', {id:'product-6383', class:'product post-6383'}, main);
const form = new Element('form', {class:'cart'}, product);
const attrs = {...input.legacy_attrs};
const row = new Element('div', input.on_control ? {} : attrs, form, input.style || {});
const legacy = new Element('select', {name:'print_option_quantity', id:'legacy-tier',
  class:'pigf-coroplast-legacy-quantity', ...(input.on_control ? attrs : {})}, row,
  input.control_style || {});
legacy.labels = [{textContent: 'Quantity'}];
const typed = new Element('input', {type:'number', 'data-pigf-coroplast-quantity':''}, form);
typed.labels = [{textContent:'Quantity'}];
const nativeWoo = new Element('input', {type:'number', name:'quantity'}, form, {}, null);
nativeWoo.labels = [{textContent:'Product quantity'}];
const controls = [legacy, typed, nativeWoo];
const document = {body, querySelectorAll: selector => selector.startsWith('form.cart') ? [form] : [], getElementById: () => null};
const context = {document, location:{href:input.url}, innerWidth:1280, innerHeight:720,
  getComputedStyle: node => node.styleWitness, Date, Object, Array, String};
const capture = vm.runInNewContext('(' + input.script + ')', context);
process.stdout.write(JSON.stringify(capture()));
