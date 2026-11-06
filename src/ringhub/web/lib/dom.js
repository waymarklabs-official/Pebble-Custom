// Tiny DOM helpers. All user/API data goes through text nodes; nothing here uses innerHTML.

/**
 * h('button', {class: 'btn', onClick: fn, disabled: true}, 'Save')
 * - `class`, `style` (object), `dataset` (object), `on<Event>` handlers
 * - DOM properties (value, checked, selected) are set as properties, everything else as attributes
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'style' && typeof value === 'object') {
      for (const [prop, v] of Object.entries(value)) {
        if (prop.startsWith('--')) el.style.setProperty(prop, v); else el.style[prop] = v;
      }
    }
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'value' || key === 'checked' || key === 'selected' || key === 'indeterminate') el[key] = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export const clear = (el) => { el.replaceChildren(); return el; };

/** Replace an element's children. Null/false children are skipped (native append() would print "null"). */
export const mount = (el, ...children) => append(clear(el), children);

/** Null-safe variadic append. */
export const add = (el, ...children) => append(el, children);

/** Stroke icons (24px grid) for the navigation. */
const ICONS = {
  now: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-5a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
  modes: 'M4 6h16M4 12h16M4 18h10',
  skills: 'M12 3l2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2Z',
  memory: 'M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h5',
  setup: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM19.4 13a7.5 7.5 0 0 0 0-2l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-1.7-1L15 3.5h-4l-.4 2.5a7.6 7.6 0 0 0-1.7 1l-2.4-1-2 3.4L6.6 11a7.5 7.5 0 0 0 0 2l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 1.7 1l.4 2.5h4l.4-2.5a7.6 7.6 0 0 0 1.7-1l2.4 1 2-3.4Z',
};
export function icon(name) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'svg-icon');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', ICONS[name] || ICONS.now);
  svg.append(path);
  return svg;
}

let toastTimer = null;
export function toast(message, kind = 'ok') {
  let box = document.getElementById('toast');
  if (!box) {
    box = h('div', { id: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(box);
  }
  box.className = `toast show ${kind}`;
  box.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.className = 'toast'; }, kind === 'error' ? 6000 : 2600);
}

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
export function relTime(ts) {
  if (!ts) return '';
  const diff = ts * 1000 - Date.now();
  const abs = Math.abs(diff);
  const units = [['day', 86400000], ['hour', 3600000], ['minute', 60000]];
  for (const [unit, ms] of units) {
    if (abs >= ms) return rtf.format(Math.round(diff / ms), unit);
  }
  return diff < 0 ? 'just now' : 'in a moment';
}

export function clock(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (sameDay) return time;
  return `${d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })} ${time}`;
}

/** A labelled form row. */
export function field(label, control, help) {
  return h('label', { class: 'field' },
    h('span', { class: 'field-label' }, label),
    control,
    help ? h('span', { class: 'field-help' }, help) : null,
  );
}

/** Pill toggles for multi-select. `selected` is a Set that is mutated in place. */
export function chips(options, selected, { onChange } = {}) {
  const wrap = h('div', { class: 'chips' });
  for (const opt of options) {
    const value = typeof opt === 'string' ? opt : opt.value;
    const labelText = typeof opt === 'string' ? opt : opt.label;
    const btn = h('button', {
      type: 'button', class: `chip${selected.has(value) ? ' on' : ''}`, 'aria-pressed': String(selected.has(value)),
      onClick: () => {
        if (selected.has(value)) selected.delete(value); else selected.add(value);
        btn.classList.toggle('on', selected.has(value));
        btn.setAttribute('aria-pressed', String(selected.has(value)));
        if (onChange) onChange(selected);
      },
    }, labelText);
    wrap.append(btn);
  }
  return wrap;
}

export function emptyState(title, text, action) {
  return h('div', { class: 'empty' }, h('p', { class: 'empty-title' }, title), h('p', {}, text), action || null);
}

export function slugify(name) {
  let s = name.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'item';
  if (/^[0-9]/.test(s)) s = `x_${s}`;
  return s.slice(0, 40);
}

export function uniqueId(base, taken) {
  let id = base;
  let n = 2;
  while (taken.has(id)) id = `${base.slice(0, 36)}_${n++}`;
  return id;
}

/** Confirm inline instead of window.confirm(): first click arms, second click acts. */
export function confirmButton(label, armedLabel, onConfirm, cls = 'btn danger') {
  let armed = false;
  let timer = null;
  const btn = h('button', {
    type: 'button', class: cls,
    onClick: async () => {
      if (!armed) {
        armed = true;
        btn.textContent = armedLabel;
        btn.classList.add('armed');
        timer = setTimeout(() => { armed = false; btn.textContent = label; btn.classList.remove('armed'); }, 3500);
        return;
      }
      clearTimeout(timer);
      btn.disabled = true;
      try { await onConfirm(); } finally { btn.disabled = false; }
    },
  }, label);
  return btn;
}
