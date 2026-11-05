// Now: what the ring does right here, right now.

import { get, post } from '../lib/api.js';
import { clear, clock, emptyState, h, mount, relTime, toast } from '../lib/dom.js';
import { GESTURES } from '../lib/vocab.js';
import { reminderWhen } from './memory.js';

const POLL_MS = 10000;

export async function render(root) {
  let timer = null;
  let disposed = false;
  let modes = [];
  let places = [];

  async function load() {
    const [nowData, modeList, placeList] = await Promise.all([get('/now'), get('/modes'), get('/places')]);
    modes = modeList;
    places = placeList;
    return nowData;
  }

  const placeName = (id) => (places.find((p) => p.id === id) || { name: id }).name;

  function draw(d) {
    const accent = d.mode.color || '#6b7cff';
    root.style.setProperty('--accent', accent);

    const hero = h('section', { class: 'card hero' },
      h('div', { class: 'hero-top' },
        h('span', { class: 'hero-icon', 'aria-hidden': 'true' }, d.mode.icon),
        h('div', {},
          h('p', { class: 'eyebrow' }, d.manual ? 'Mode · set by hand' : 'Mode'),
          h('h1', { class: 'hero-title' }, d.mode.name),
          h('p', { class: 'muted' }, sentence(d.reason)))),
      contextLine(d),
    );

    const attention = d.attention ? h('section', { class: 'card banner' },
      d.attention.kind === 'reminder'
        ? 'A reminder just fired — click the ring for done, double for snooze.'
        : 'Just captured — double-click the ring within 30 s to undo.') : null;

    const gestureList = h('ul', { class: 'gestures' }, d.gestures.map((g) => gestureRow(g)));

    const section = (title, ...body) => h('section', { class: 'card' }, h('h2', {}, title), ...body);

    mount(root,
      hero,
      attention,
      section('What each gesture does', gestureList,
        h('p', { class: 'muted small' }, 'Change these in ', h('a', { href: '#/modes' }, 'Modes'), '.')),
      section('Coming up', remindersList(d.reminders)),
      section('Recently captured', notesList(d.notes)),
      section('Switch mode by hand', overrideControl(d)),
    );
  }

  function contextLine(d) {
    const c = d.context || {};
    const items = [];
    items.push(h('span', { class: 'pill' }, '📍 ', c.place ? `${placeName(c.place)}` : 'Away / unknown',
      c.place_since ? h('span', { class: 'muted' }, ` · ${relTime(c.place_since)}`) : null));
    if (c.focus) items.push(h('span', { class: 'pill' }, `🌗 ${c.focus} Focus`));
    if (d.vehicle) items.push(h('span', { class: 'pill' }, `🚗 ${d.vehicle.summary}`));
    return h('div', { class: 'pills' }, items);
  }

  function gestureRow(g) {
    const meta = GESTURES.find((x) => x.key === g.key) || { glyph: '', voice: false };
    const name = g.skill ? `${g.skill.icon} ${g.skill.name}` : 'Nothing';
    const tryArea = h('div', { class: 'try' });
    const tryBtn = h('button', {
      type: 'button', class: 'btn small ghost',
      onClick: () => (meta.voice ? openVoice() : send()),
    }, 'Try');

    async function send(transcript) {
      tryBtn.disabled = true;
      try {
        await post('/simulate', transcript === undefined ? { gesture: g.key } : { gesture: g.key, transcript });
        toast(`Sent ${g.label}`);
        setTimeout(refresh, 900);
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        tryBtn.disabled = false;
      }
    }

    function openVoice() {
      clear(tryArea);
      const input = h('input', { type: 'text', placeholder: 'What you would say…', 'aria-label': 'Transcript' });
      const form = h('form', {
        class: 'inline-form',
        onSubmit: (e) => { e.preventDefault(); if (input.value.trim()) { send(input.value.trim()); clear(tryArea); } },
      }, input, h('button', { class: 'btn small primary', type: 'submit' }, 'Send'));
      tryArea.append(form);
      input.focus();
    }

    return h('li', { class: 'gesture' },
      h('span', { class: 'glyph', 'aria-hidden': 'true' }, meta.glyph),
      h('span', { class: 'gesture-label' }, g.label),
      h('span', { class: `gesture-skill${g.skill ? '' : ' muted'}` }, name,
        g.inherited ? h('span', { class: 'tag' }, 'from Everywhere') : null),
      tryBtn,
      tryArea);
  }

  function remindersList(items) {
    if (!items.length) return emptyState('Nothing pending', 'Hold the ring and say “remind me at 7 to call mom” or “when I get home…”.');
    return h('ul', { class: 'list' }, items.map((r) => h('li', { class: 'row' },
      h('div', { class: 'row-main' },
        h('span', { class: 'row-title' }, r.text),
        h('span', { class: 'row-sub' }, reminderWhen(r, placeName), r.status === 'fired' ? ' · fired' : '')),
      h('div', { class: 'row-actions' },
        reminderAction(r, 'done', 'Done'),
        r.status === 'fired' ? reminderAction(r, 'snooze', 'Snooze') : null,
        reminderAction(r, 'cancel', '✕', 'Cancel reminder')))));
  }

  function reminderAction(r, action, label, aria) {
    return h('button', {
      type: 'button', class: 'btn small ghost', 'aria-label': aria || label,
      onClick: async () => {
        try { await post(`/reminders/${r.id}/${action}`); toast(action === 'done' ? 'Done ✓' : action === 'snooze' ? 'Snoozed 10 min' : 'Cancelled'); refresh(); } catch (e) { toast(e.message, 'error'); }
      },
    }, label);
  }

  function notesList(items) {
    if (!items.length) return emptyState('No notes yet', 'Hold the ring and say anything — it lands here, tagged with where you were.');
    const icons = { note: '📝', journal: '📓', moment: '📍', log: '✔', reminder: '⏰' };
    return h('ul', { class: 'list' }, items.slice(0, 6).map((n) => h('li', { class: 'row' },
      h('span', { class: 'row-icon', 'aria-hidden': 'true' }, icons[n.kind] || '📝'),
      h('div', { class: 'row-main' },
        h('span', { class: 'row-title' }, n.text),
        h('span', { class: 'row-sub' }, clock(n.created_at), n.place ? ` · ${placeName(n.place)}` : '')))),
    h('a', { class: 'more', href: '#/memory' }, 'All notes →'));
  }

  function overrideControl(d) {
    const select = h('select', { 'aria-label': 'Mode' },
      modes.filter((m) => m.enabled).map((m) => h('option', { value: m.id, selected: m.id === d.mode.id }, `${m.icon} ${m.name}`)));
    const duration = h('select', { 'aria-label': 'For how long' },
      h('option', { value: '' }, 'until I change place'),
      h('option', { value: '30' }, 'for 30 minutes'),
      h('option', { value: '60' }, 'for 1 hour'),
      h('option', { value: '240' }, 'for 4 hours'));
    const apply = h('button', {
      type: 'button', class: 'btn primary',
      onClick: async () => {
        try {
          await post('/override', { mode: select.value, minutes: duration.value ? Number(duration.value) : null });
          toast('Mode switched');
          refresh();
        } catch (e) { toast(e.message, 'error'); }
      },
    }, 'Switch');
    const reset = d.manual ? h('button', {
      type: 'button', class: 'btn ghost',
      onClick: async () => { try { await post('/override', { mode: null }); toast('Back to automatic'); refresh(); } catch (e) { toast(e.message, 'error'); } },
    }, 'Back to automatic') : null;
    return h('div', {},
      h('p', { class: 'muted small' }, 'Modes normally switch themselves from where you are. Triple-click the ring twice to cycle them.'),
      h('div', { class: 'inline-form wrap' }, select, duration, apply, reset));
  }

  async function refresh() {
    if (disposed) return;
    try { draw(await load()); } catch (e) { if (e.status !== 401) toast(e.message, 'error'); }
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  }

  const onVisible = () => { if (!document.hidden) refresh(); };
  document.addEventListener('visibilitychange', onVisible);
  draw(await load());
  schedule();
  return () => {
    disposed = true;
    clearInterval(timer);
    document.removeEventListener('visibilitychange', onVisible);
    root.style.removeProperty('--accent');
  };
}

function sentence(reason) {
  if (!reason) return '';
  if (reason === 'no other mode applies') return 'No other mode applies right now.';
  if (reason === 'set by hand') return 'You picked this mode.';
  return `Because: ${reason}.`;
}
