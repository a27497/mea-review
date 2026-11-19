import {zhCN} from './messages-zh.js';

const preference = 'mea.language';
const bindings = new WeakMap();
const supported = new Set(['en', 'zh-CN']);

function initialLanguage() {
  try {
    const saved = localStorage.getItem(preference);
    if (supported.has(saved)) return saved;
  } catch { /* Locale defaults still work when storage is unavailable. */ }
  return /^zh(?:-|$)/i.test(navigator.language) ? 'zh-CN' : 'en';
}

let language = initialLanguage();

export function m(key, values = {}) {
  return {key, values};
}

function format(value) {
  if (typeof value !== 'object' || value === null || !Object.hasOwn(value, 'key')) return String(value ?? '');
  const {key, values} = value;
  const template = language === 'zh-CN' && Object.hasOwn(zhCN, key) ? zhCN[key] : key;
  return template.replace(/\{(\w+)\}/g, (all, token) => Object.hasOwn(values, token) ? format(values[token]) : all);
}

function bind(node, property, value, write) {
  if (value && typeof value === 'object' && Object.hasOwn(value, 'key')) {
    if (!bindings.has(node)) bindings.set(node, new Map());
    bindings.get(node).set(property, () => write(format(value)));
  } else {
    bindings.get(node)?.delete(property);
  }
  write(format(value));
}

export function setText(node, value) {
  bind(node, 'textContent', value, rendered => {node.textContent = rendered;});
}

export function setAttr(node, name, value) {
  bind(node, name, value, rendered => node.setAttribute(name, rendered));
}

export function copy(value) {
  const node = document.createTextNode('');
  setText(node, value);
  return node;
}

export function setTitle(value) {
  setText(document.querySelector('title'), value);
}

function applyLanguage() {
  document.documentElement.lang = language;
  // Visit live bindings only. Replaced nodes can be collected and editing
  // controls, selections, checked values and disclosures keep their identity.
  const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    for (const apply of bindings.get(walker.currentNode)?.values() ?? []) apply();
  }
  const toggle = document.getElementById('language-toggle');
  const chinese = language === 'zh-CN';
  toggle.textContent = chinese ? 'English' : '简体中文';
  toggle.lang = chinese ? 'en' : 'zh-CN';
  toggle.setAttribute('aria-label', chinese ? 'Switch to English' : '切换到简体中文');
}

export function mountLanguage() {
  for (const node of document.querySelectorAll('[data-i18n]')) setText(node, m(node.textContent.trim()));
  for (const name of ['aria-label', 'placeholder']) {
    for (const node of document.querySelectorAll('[data-i18n-' + name + ']')) setAttr(node, name, m(node.getAttribute(name)));
  }
  const toggle = document.getElementById('language-toggle');
  toggle.addEventListener('click', () => {
    language = language === 'zh-CN' ? 'en' : 'zh-CN';
    try { localStorage.setItem(preference, language); } catch { /* The current page still updates. */ }
    applyLanguage();
  });
  window.addEventListener('storage', event => {
    if (event.key !== preference) return;
    language = initialLanguage();
    applyLanguage();
  });
  applyLanguage();
}

export function option(label, value) {
  const node = new Option('', value);
  setText(node, label);
  return node;
}

export function diagnostic(value) {
  return typeof value === 'string' && Object.hasOwn(zhCN, value) ? m(value) : value;
}
