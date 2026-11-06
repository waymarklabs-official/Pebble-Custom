// Memory: everything you've said, and everything you asked to be reminded of.

import { del, get, patch, post } from '../lib/api.js';
import { add, clear, clock, confirmButton, emptyState, field, h, mount, relTime, toast } from '../lib/dom.js';

const KIND_ICON = { note: '📝', journal: '📓', moment: '📍', log: '✔', reminder: '⏰' };

/** "7:00 PM", "When I arrive at Work", "On my list" */
export function reminderWhen(r, placeName) {
  if (r.trigger === 'time' && r.due_at) return `${clock(r.due_at)} (${relTime(r.due_at)})`;
  if (r.trigger === 'arrive') return `When I arrive at ${placeName(r.place)}`;
  if (r.trigger === 'leave') return `When I leave ${placeName(r.place)}`;
  return 'On my list';
}

export async function render(root, { params }) {
  const tab = params[0] === 'reminders' ? 'reminders' : 'notes';
  const places = await get('/places');
  const placeName = (id) => (places.find((p) => p.id === id) || { name: id || '?' }).name;

  const tabs = h('div', { class: 'segmented', role: 'tablist' },
    h('a', { href: '#/memory', role: 'tab', class: tab === 'notes' ? 'on' : '', 'aria-selected': String(tab === 'notes') }, 'Notes'),
    h('a', { href: '#/memory/reminders', role: 'tab', class: tab === 'reminders' ? 'on' : '', 'aria-selected': String(tab === 'reminders') }, 'Reminders'));
  const body = h('div', {});
  mount(root, h('header', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Memory'))), tabs, body);

  if (tab === 'notes') await notes(body, placeName);
  else await reminders(body, placeName, places, params[1] === 'all');
  return undefined;
}

async function notes(body, placeName) {
  const search = h('input', { type: 'search', placeholder: 'Search everything you said…', 'aria-label': 'Search notes', autocomplete: 'off' });
  const list = h('ul', { class: 'list notes' });
  let timer = null;
  let seq = 0;

  async function load() {
    const mine = ++seq;
    const q = search.value.trim();
    let items;
    try { items = await get(`/notes?limit=100${q ? `&q=${encodeURIComponent(q)}` : ''}`); } catch (e) { toast(e.message, 'error'); return; }
    if (mine !== seq) return;
    clear(list);
    if (!items.length) {
      list.append(q ? emptyState('No matches', `Nothing you said mentions “${q}”.`) : emptyState('No notes yet', 'Hold the ring and talk. Anything that isn\'t a command or reminder is saved here.'));
      return;
    }
    for (const n of items) list.append(noteRow(n));
  }

  function noteRow(n) {
    const text = h('span', { class: 'row-title' }, n.text);
    const row = h('li', { class: 'row' },
      h('span', { class: 'row-icon', 'aria-hidden': 'true' }, KIND_ICON[n.kind] || '📝'),
      h('div', { class: 'row-main' }, text,
        h('span', { class: 'row-sub' }, clock(n.created_at), n.place ? ` · ${placeName(n.place)}` : '', n.audio ? ' · 🎙️ audio' : '')),
      h('div', { class: 'row-actions' },
        h('button', { type: 'button', class: 'btn small ghost', onClick: () => edit() }, 'Edit'),
        confirmButton('✕', 'Delete?', async () => {
          try { await del(`/notes/${n.id}`); row.remove(); toast('Deleted'); } catch (e) { toast(e.message, 'error'); }
        }, 'btn small ghost')));

    function edit() {
      const input = h('textarea', { rows: 2 }, n.text);
      const form = h('form', {
        class: 'inline-edit',
        onSubmit: async (e) => {
          e.preventDefault();
          const value = input.value.trim();
          if (!value) return;
          try { await patch(`/notes/${n.id}`, { text: value }); n.text = value; toast('Saved'); row.replaceWith(noteRow(n)); } catch (ex) { toast(ex.message, 'error'); }
        },
      }, input, h('div', { class: 'inline-form' },
        h('button', { type: 'submit', class: 'btn small primary' }, 'Save'),
        h('button', { type: 'button', class: 'btn small ghost', onClick: () => row.replaceWith(noteRow(n)) }, 'Cancel')));
      text.replaceWith(form);
      input.focus();
    }
    return row;
  }

  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  add(body, h('section', { class: 'card' }, search, list));
  await load();
}

async function reminders(body, placeName, places, showAll) {
  const list = h('ul', { class: 'list' });
  const filter = h('div', { class: 'segmented small' },
    h('a', { href: '#/memory/reminders', class: showAll ? '' : 'on' }, 'Open'),
    h('a', { href: '#/memory/reminders/all', class: showAll ? 'on' : '' }, 'All'));

  async function load() {
    const items = await get(`/reminders?status=${showAll ? 'all' : 'open'}`);
    clear(list);
    if (!items.length) {
      list.append(emptyState('All clear', 'Say “remind me at 7 to…”, “when I get home…”, or “remind me to…” for a list item.'));
      return;
    }
    for (const r of items) {
      const open = r.status === 'pending' || r.status === 'fired';
      list.append(h('li', { class: `row${open ? '' : ' done'}` },
        h('span', { class: 'row-icon', 'aria-hidden': 'true' }, r.trigger === 'time' ? '⏰' : r.trigger === 'none' ? '📋' : '📍'),
        h('div', { class: 'row-main' },
          h('span', { class: 'row-title' }, r.text),
          h('span', { class: 'row-sub' }, reminderWhen(r, placeName), open ? (r.status === 'fired' ? ' · fired' : '') : ` · ${r.status}`)),
        open ? h('div', { class: 'row-actions' },
          act(r, 'done', 'Done'), r.status === 'fired' ? act(r, 'snooze', 'Snooze') : null, act(r, 'cancel', '✕')) : null));
    }
  }

  function act(r, action, label) {
    return h('button', {
      type: 'button', class: 'btn small ghost', 'aria-label': action,
      onClick: async () => { try { await post(`/reminders/${r.id}/${action}`); toast(action === 'done' ? 'Done ✓' : action); await load(); } catch (e) { toast(e.message, 'error'); } },
    }, label);
  }

  // ---- add form
  const text = h('input', { type: 'text', placeholder: 'What to remember', maxlength: 300, required: true, 'aria-label': 'Reminder text' });
  const trigger = h('select', { 'aria-label': 'When' },
    h('option', { value: 'in' }, 'In… minutes'), h('option', { value: 'at' }, 'At a time'),
    h('option', { value: 'arrive' }, 'When I arrive at…'), h('option', { value: 'leave' }, 'When I leave…'),
    h('option', { value: 'none' }, 'Just add to my list'));
  const minutes = h('input', { type: 'number', min: 1, value: 30, class: 'num', 'aria-label': 'Minutes' });
  const at = h('input', { type: 'datetime-local', 'aria-label': 'Date and time' });
  const place = h('select', { 'aria-label': 'Place' }, places.map((p) => h('option', { value: p.id }, `${p.icon} ${p.name}`)));
  const extra = h('span', {});
  const syncExtra = () => {
    clear(extra);
    const v = trigger.value;
    if (v === 'in') extra.append(minutes);
    if (v === 'at') extra.append(at);
    if (v === 'arrive' || v === 'leave') extra.append(places.length ? place : h('span', { class: 'muted small' }, 'Add a place in Setup first'));
  };
  trigger.addEventListener('change', syncExtra);
  syncExtra();
  const error = h('p', { class: 'form-error', role: 'alert' });
  const form = h('form', {
    class: 'card add-reminder',
    onSubmit: async (e) => {
      e.preventDefault();
      error.textContent = '';
      const v = trigger.value;
      const payload = { text: text.value.trim() };
      if (v === 'in') Object.assign(payload, { trigger: 'time', after_minutes: Number(minutes.value) });
      else if (v === 'at') {
        if (!at.value) { error.textContent = 'Pick a date and time.'; return; }
        Object.assign(payload, { trigger: 'time', at: new Date(at.value).toISOString() });
      } else if (v === 'arrive' || v === 'leave') Object.assign(payload, { trigger: v, place: place.value });
      else payload.trigger = 'none';
      try { await post('/reminders', payload); text.value = ''; toast('Reminder added'); await load(); } catch (ex) { error.textContent = ex.message; }
    },
  }, h('h2', {}, 'Add a reminder'), field('Remind me to', text), h('div', { class: 'inline-form wrap' }, trigger, extra,
    h('button', { type: 'submit', class: 'btn primary' }, 'Add')), error);

  add(body, h('section', { class: 'card' }, filter, list), form);
  await load();
}
