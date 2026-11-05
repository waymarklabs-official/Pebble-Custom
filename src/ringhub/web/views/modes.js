// Modes: when each mode applies (top wins) and what each gesture does in it.

import { del, get, post, put } from '../lib/api.js';
import { chips, confirmButton, emptyState, field, h, mount, slugify, toast, uniqueId } from '../lib/dom.js';
import { DAYS, FALLBACK, FOCUS_PRESETS, GESTURES, QUICK_GESTURES, gestureLabel, summarizeWhen } from '../lib/vocab.js';

export async function render(root, { params, go, refresh }) {
  const [modes, skills, places] = await Promise.all([get('/modes'), get('/skills'), get('/places')]);
  const placeName = (id) => (places.find((p) => p.id === id) || { name: id }).name;
  const skillName = (id) => {
    const s = skills.find((x) => x.id === id);
    return s ? `${s.icon} ${s.name}` : id;
  };

  if (params[0]) {
    const existing = params[0] === 'new' ? null : modes.find((m) => m.id === params[0]);
    if (params[0] !== 'new' && !existing) { go('modes'); return undefined; }
    return editor(root, { mode: existing, modes, skills, places, go });
  }

  async function move(index, delta) {
    const movable = modes.filter((m) => m.id !== FALLBACK);
    const target = index + delta;
    if (target < 0 || target >= movable.length) return;
    [movable[index], movable[target]] = [movable[target], movable[index]];
    try {
      await post('/modes/order', { ids: movable.map((m) => m.id) });
      refresh();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function toggle(mode, enabled) {
    try {
      await put(`/modes/${mode.id}`, { ...mode, enabled });
      toast(enabled ? `${mode.name} on` : `${mode.name} off`);
    } catch (e) { toast(e.message, 'error'); }
    refresh();
  }

  const movable = modes.filter((m) => m.id !== FALLBACK);
  const fallback = modes.find((m) => m.id === FALLBACK);

  const card = (m, i) => {
    const isFallback = m.id === FALLBACK;
    const bound = GESTURES.filter((g) => m.bindings[g.key]).map((g) =>
      h('span', { class: 'mini-binding' }, h('b', {}, g.label), ' ', skillName(m.bindings[g.key])));
    return h('li', { class: `card mode-card${m.enabled ? '' : ' disabled'}`, style: { '--mode-color': m.color } },
      h('div', { class: 'mode-head' },
        h('span', { class: 'mode-icon', 'aria-hidden': 'true' }, m.icon),
        h('div', { class: 'mode-title' },
          h('a', { href: `#/modes/${m.id}`, class: 'mode-name' }, m.name),
          h('span', { class: 'muted small' }, isFallback ? 'Always applies when nothing above does' : summarizeWhen(m.when, placeName))),
        isFallback ? null : h('div', { class: 'mode-controls' },
          h('button', { type: 'button', class: 'btn icon ghost', 'aria-label': `Move ${m.name} up`, disabled: i === 0, onClick: () => move(i, -1) }, '↑'),
          h('button', { type: 'button', class: 'btn icon ghost', 'aria-label': `Move ${m.name} down`, disabled: i === movable.length - 1, onClick: () => move(i, 1) }, '↓'),
          h('label', { class: 'switch', title: m.enabled ? 'On' : 'Off' },
            h('input', { type: 'checkbox', checked: m.enabled, 'aria-label': `${m.name} enabled`, onChange: (e) => toggle(m, e.target.checked) }),
            h('span', { class: 'slider', 'aria-hidden': 'true' })))),
      bound.length ? h('div', { class: 'mini-bindings' }, bound) : h('p', { class: 'muted small' }, 'Uses Everywhere for every gesture.'));
  };

  mount(root,
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', {}, 'Modes'), h('p', { class: 'muted' }, 'The first mode that matches where you are wins. Gestures a mode leaves empty use Everywhere.')),
      h('a', { class: 'btn primary', href: '#/modes/new' }, 'New mode')),
    movable.length ? h('ol', { class: 'mode-list' }, movable.map(card)) : emptyState('Only Everywhere so far', 'Add a mode like “Home” or “Work” to change what the ring does there.'),
    fallback ? h('ol', { class: 'mode-list' }, card(fallback, -1)) : null,
  );
  return undefined;
}

function editor(root, { mode, modes, skills, places, go }) {
  const isNew = !mode;
  const isFallback = mode?.id === FALLBACK;
  const m = structuredClone(mode || {
    id: '', name: '', icon: '◎', color: '#1a73e8', enabled: true, feedback: 'normal',
    when: { places: [], not_places: [], focus: [], time: null, days: [], context: {}, recently: null }, bindings: {},
  });
  const w = m.when;

  const name = h('input', { type: 'text', value: m.name, maxlength: 40, required: true, placeholder: 'e.g. Gym', 'aria-label': 'Name' });
  const icon = h('input', { type: 'text', value: m.icon, maxlength: 8, class: 'icon-input', 'aria-label': 'Icon' });
  const color = h('input', { type: 'color', value: m.color, 'aria-label': 'Color' });
  const feedback = h('select', {},
    [['normal', 'Normal — confirm every press'], ['quiet', 'Quiet — silent confirmations'], ['silent', 'Silent — only reminders and problems']]
      .map(([v, l]) => h('option', { value: v, selected: m.feedback === v }, l)));

  // ---- conditions
  const placeOpts = places.map((p) => ({ value: p.id, label: `${p.icon} ${p.name}` }));
  const atPlaces = new Set(w.places);
  const notPlaces = new Set(w.not_places);
  const focus = new Set(w.focus);
  const days = new Set(w.days);
  const focusOpts = [...new Set([...FOCUS_PRESETS, ...w.focus])];

  const timeOn = h('input', { type: 'checkbox', checked: Boolean(w.time) });
  const after = h('input', { type: 'time', value: w.time?.after || '09:00', 'aria-label': 'From' });
  const before = h('input', { type: 'time', value: w.time?.before || '17:00', 'aria-label': 'Until' });

  const recentOn = h('input', { type: 'checkbox', checked: Boolean(w.recently) });
  const recentMin = h('input', { type: 'number', min: 1, max: 1440, value: w.recently?.within_minutes || 10, class: 'num', 'aria-label': 'Minutes' });
  const recentEvent = h('select', { 'aria-label': 'Event' },
    h('option', { value: 'leave', selected: w.recently?.event === 'leave' }, 'leave'),
    h('option', { value: 'arrive', selected: w.recently?.event === 'arrive' }, 'arrive at'));
  const recentPlace = h('select', { 'aria-label': 'Place' }, places.map((p) => h('option', { value: p.id, selected: w.recently?.place === p.id }, p.name)));

  const ctxRows = h('div', { class: 'kv-rows' });
  const addCtxRow = (k = '', v = '') => {
    const key = h('input', { type: 'text', value: k, placeholder: 'key', class: 'mono', 'aria-label': 'Context key' });
    const val = h('input', { type: 'text', value: v, placeholder: 'value', class: 'mono', 'aria-label': 'Context value' });
    const row = h('div', { class: 'kv' }, key, h('span', {}, '='), val,
      h('button', { type: 'button', class: 'btn icon ghost', 'aria-label': 'Remove', onClick: () => row.remove() }, '✕'));
    row.read = () => [key.value.trim(), parseValue(val.value.trim())];
    ctxRows.append(row);
  };
  for (const [k, v] of Object.entries(w.context || {})) addCtxRow(k, typeof v === 'string' ? v : JSON.stringify(v));

  const conditions = isFallback
    ? h('p', { class: 'muted' }, 'Everywhere always applies when no other mode matches, so it has no conditions.')
    : h('div', { class: 'conditions' },
      h('p', { class: 'muted small' }, 'All the conditions you set must be true. Leave everything empty for “always”.'),
      places.length ? field("When I'm at", chips(placeOpts, atPlaces)) : h('p', { class: 'muted small' }, h('a', { href: '#/setup' }, 'Add places'), ' to use location.'),
      places.length ? field("…but not at", chips(placeOpts, notPlaces)) : null,
      field('When my iPhone Focus is', chips(focusOpts, focus)),
      h('div', { class: 'cond-row' }, h('label', { class: 'check' }, timeOn, ' Only between'), after, h('span', {}, 'and'), before),
      field('On', chips(DAYS, days), 'None selected = every day.'),
      places.length ? h('div', { class: 'cond-row' }, h('label', { class: 'check' }, recentOn, ' Only within'), recentMin, h('span', {}, 'minutes after I'), recentEvent, recentPlace) : null,
      h('details', { class: 'advanced', open: Object.keys(w.context || {}).length > 0 },
        h('summary', {}, 'Advanced: match a context value'),
        h('p', { class: 'muted small' }, 'Shortcuts can set any value, e.g. POST /api/v1/context {"set": {"guests": true}}.'),
        ctxRows, h('button', { type: 'button', class: 'btn small ghost', onClick: () => addCtxRow() }, '+ Add value')));

  // ---- bindings
  const skillOptions = (selected, emptyLabel) => [
    h('option', { value: '', selected: !selected }, emptyLabel),
    ...skills.map((s) => h('option', { value: s.id, selected: s.id === selected }, `${s.icon} ${s.name}`)),
  ];
  const emptyLabel = isFallback ? '— nothing —' : '— same as Everywhere —';
  const bindingSelects = new Map();
  const bindingRows = GESTURES.map((g) => {
    const sel = h('select', { 'aria-label': g.label }, skillOptions(m.bindings[g.key], emptyLabel));
    bindingSelects.set(g.key, sel);
    return h('div', { class: 'binding' }, h('span', { class: 'glyph', 'aria-hidden': 'true' }, g.glyph), h('span', { class: 'binding-label' }, g.label), sel);
  });

  const comboRows = h('div', {});
  const addCombo = (key = 'double-click+click', skill = '') => {
    const [first, second] = key.split('+');
    const a = h('select', { 'aria-label': 'First gesture' }, QUICK_GESTURES.map((k) => h('option', { value: k, selected: k === first }, gestureLabel(k))));
    const b = h('select', { 'aria-label': 'Then' }, QUICK_GESTURES.map((k) => h('option', { value: k, selected: k === second }, gestureLabel(k))));
    const s = h('select', { 'aria-label': 'Skill' }, skillOptions(skill, '— pick a skill —'));
    const row = h('div', { class: 'binding combo' }, a, h('span', {}, 'then'), b, s,
      h('button', { type: 'button', class: 'btn icon ghost', 'aria-label': 'Remove combo', onClick: () => row.remove() }, '✕'));
    row.read = () => [`${a.value}+${b.value}`, s.value];
    comboRows.append(row);
  };
  for (const [k, v] of Object.entries(m.bindings)) if (k.includes('+')) addCombo(k, v);

  const error = h('p', { class: 'form-error', role: 'alert' });

  async function save(e) {
    e.preventDefault();
    error.textContent = '';
    const doc = {
      id: m.id, name: name.value.trim(), icon: icon.value.trim() || '◎', color: color.value, enabled: m.enabled,
      feedback: feedback.value, bindings: {},
      when: isFallback ? {} : {
        places: [...atPlaces], not_places: [...notPlaces], focus: [...focus], days: [...days],
        time: timeOn.checked ? { after: after.value, before: before.value } : null,
        recently: recentOn.checked && places.length ? { event: recentEvent.value, place: recentPlace.value, within_minutes: Number(recentMin.value) } : null,
        context: Object.fromEntries([...ctxRows.children].map((r) => r.read()).filter(([k]) => k)),
      },
    };
    for (const [key, sel] of bindingSelects) if (sel.value) doc.bindings[key] = sel.value;
    for (const row of comboRows.children) { const [k, v] = row.read(); if (v) doc.bindings[k] = v; }
    if (!doc.name) { error.textContent = 'Give the mode a name.'; return; }
    if (isNew) doc.id = uniqueId(slugify(doc.name), new Set(modes.map((x) => x.id)));
    try {
      await put(`/modes/${doc.id}`, doc);
      toast(isNew ? `${doc.name} created` : 'Saved');
      go('modes');
    } catch (ex) { error.textContent = ex.message; }
  }

  const deleteBtn = !isNew && !isFallback ? confirmButton('Delete mode', 'Tap again to delete', async () => {
    try { await del(`/modes/${m.id}`); toast('Mode deleted'); go('modes'); } catch (ex) { error.textContent = ex.message; }
  }) : null;

  mount(root, h('form', { class: 'editor', onSubmit: save },
    h('header', { class: 'page-head' },
      h('div', {}, h('a', { href: '#/modes', class: 'back' }, '← Modes'), h('h1', {}, isNew ? 'New mode' : m.name))),
    h('section', { class: 'card' },
      h('div', { class: 'name-row' }, field('Icon', icon), field('Name', name), field('Color', color)),
      field('Feedback', feedback, 'How loudly the hub confirms presses while this mode is active.')),
    h('section', { class: 'card' }, h('h2', {}, 'When does it apply?'), conditions),
    h('section', { class: 'card' }, h('h2', {}, 'What each gesture does'), h('div', { class: 'bindings' }, bindingRows),
      h('details', { class: 'advanced', open: comboRows.children.length > 0 },
        h('summary', {}, 'Advanced: two-gesture combos'),
        h('p', { class: 'muted small' }, 'e.g. double then click within ~0.6 s. Gestures that start a combo wait that long before running on their own.'),
        comboRows, h('button', { type: 'button', class: 'btn small ghost', onClick: () => addCombo() }, '+ Add combo'))),
    error,
    h('div', { class: 'actions sticky' }, deleteBtn, h('span', { class: 'spacer' }),
      h('a', { class: 'btn ghost', href: '#/modes' }, 'Cancel'), h('button', { type: 'submit', class: 'btn primary' }, 'Save'))));
  name.focus();
  return undefined;
}

function parseValue(v) {
  if (v === 'true') return true;
  if (v === 'false') return false;
  if (v !== '' && !Number.isNaN(Number(v))) return Number(v);
  return v;
}
