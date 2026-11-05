// Setup: places, how your phone reports them, what happened, backup.

import { logout } from '../app.js';
import { del, get, post, put } from '../lib/api.js';
import { add, clock, confirmButton, emptyState, field, h, mount, relTime, slugify, toast, uniqueId } from '../lib/dom.js';

export async function render(root, { params, go, refresh }) {
  const [places, setup, ctx, events] = await Promise.all([get('/places'), get('/setup'), get('/context'), get('/events?limit=40')]);

  if (params[0] === 'place') {
    const existing = params[1] && params[1] !== 'new' ? places.find((p) => p.id === params[1]) : null;
    return placeEditor(root, existing, places, go);
  }

  const placeList = places.length
    ? h('ul', { class: 'list' }, places.map((p) => h('li', { class: 'row' },
      h('span', { class: 'row-icon', 'aria-hidden': 'true' }, p.icon),
      h('div', { class: 'row-main' },
        h('a', { class: 'row-title', href: `#/setup/place/${p.id}` }, p.name),
        h('span', { class: 'row-sub' }, `Say: ${[...new Set([p.name.toLowerCase(), ...p.aliases])].join(', ')}`, p.lat != null ? ` · geofence ${p.radius_m} m` : '')))))
    : emptyState('No places yet', 'Add Home so the ring knows when you\'re there.');

  mount(root,
    h('header', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Setup'))),
    h('section', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Places'), h('a', { class: 'btn small primary', href: '#/setup/place/new' }, 'Add place')),
      h('p', { class: 'muted small' }, 'Modes and “when I get to…” reminders use these. Your phone tells the hub when you arrive and leave.'),
      placeList),
    phoneSetup(setup),
    timeline(ctx, places),
    activity(events),
    backup(refresh),
    h('section', { class: 'card' }, h('button', { type: 'button', class: 'btn ghost', onClick: logout }, 'Sign out')),
  );
  return undefined;
}

function copyRow(label, value, { secret = false } = {}) {
  const shown = h('code', { class: 'copy-value' }, secret ? '•'.repeat(18) : value);
  let revealed = !secret;
  return h('div', { class: 'copy-row' },
    h('span', { class: 'copy-label' }, label), shown,
    secret ? h('button', {
      type: 'button', class: 'btn small ghost',
      onClick: (e) => { revealed = !revealed; shown.textContent = revealed ? value : '•'.repeat(18); e.target.textContent = revealed ? 'Hide' : 'Show'; },
    }, 'Show') : null,
    h('button', {
      type: 'button', class: 'btn small ghost',
      onClick: async () => {
        try { await navigator.clipboard.writeText(value); toast('Copied'); } catch { toast('Copy failed — select it by hand', 'error'); }
      },
    }, 'Copy'));
}

function phoneSetup(setup) {
  const card = h('section', { class: 'card' }, h('h2', {}, 'Phone setup'));
  if (!setup.public_url) {
    add(card, h('p', {}, 'Set ', h('code', {}, 'server.public_url'), ' in config.yaml (for example your Tailscale https URL) so your phone can reach the hub, then restart. That also turns on the Done and Snooze buttons on reminders.'));
    return card;
  }
  const base = setup.public_url;
  const token = setup.context_token;
  add(card,
    h('h3', {}, 'Index app → Webhook (every gesture)'),
    copyRow('URL', setup.webhook_url),
    h('p', { class: 'muted small' }, 'Turn on “Sign requests” and paste RINGHUB_SECRET from your .env. Voice gestures: payload “Transcription only” (or “Both” to keep audio).'));
  if (!token) {
    add(card, h('p', { class: 'form-error' }, 'Set server.context_token in config.yaml to let your phone report where you are.'));
    return card;
  }
  add(card,
    h('h3', {}, 'Shortcuts automations'),
    h('p', { class: 'muted small' }, 'Shortcuts → Automation → + → choose the trigger → Run Immediately → add “Get Contents of URL”, Method POST, and a header:'),
    copyRow('Header', `Authorization: Bearer ${token}`, { secret: true }),
  );
  const rows = [];
  for (const p of setup.places) {
    rows.push([`Arrive at ${p.name}`, `${base}/api/v1/context/arrive/${p.id}`]);
    rows.push([`Leave ${p.name}`, `${base}/api/v1/context/leave/${p.id}`]);
  }
  rows.push(['Sleep Focus turns on', `${base}/api/v1/context/focus/sleep`]);
  rows.push(['Any Focus turns off', `${base}/api/v1/context/focus/off`]);
  rows.push(['NFC tag / Wallet tap at a store', `${base}/api/v1/context/visit/<place>`]);
  add(card, h('div', { class: 'copy-list' }, rows.map(([label, url]) => copyRow(label, url))),
    h('p', { class: 'muted small' }, 'Car: use Bluetooth “connects” → Arrive at Car and “disconnects” → Leave Car. Leaving the car saves where you parked. Focus, NFC and CarPlay automations run silently; location, Wi-Fi and Bluetooth ones show a banner.'),
    h('h3', {}, 'Or OwnTracks (silent, continuous)'),
    copyRow('URL', `${base}/api/v1/owntracks`),
    copyRow('Password', token, { secret: true }),
    h('p', { class: 'muted small' }, 'Mode HTTP, any username. Places with a latitude/longitude are sent to OwnTracks as regions automatically.'));
  return card;
}

function timeline(ctx, places) {
  const name = (id) => (places.find((p) => p.id === id) || { name: id }).name;
  const describe = (e) => {
    if (e.key === 'place') return e.value ? `Arrived at ${name(e.value)}` : 'Left (now away)';
    if (e.key === 'focus') return e.value ? `${e.value} Focus on` : 'Focus off';
    if (e.key === 'location') return 'Location update';
    if (e.key === 'last_visit') return `Visited ${name(e.value?.place)}`;
    if (e.key === 'last_left' || e.key === 'place_since') return null;
    return `${e.key} = ${JSON.stringify(e.value)}`;
  };
  const items = ctx.history.map((e) => [e, describe(e)]).filter(([, d]) => d).slice(0, 25);
  return h('section', { class: 'card' }, h('h2', {}, 'What your phone reported'),
    items.length ? h('ul', { class: 'list compact' }, items.map(([e, d]) => h('li', { class: 'row' },
      h('div', { class: 'row-main' }, h('span', { class: 'row-title' }, d), h('span', { class: 'row-sub' }, `${clock(e.at)} · ${e.source || ''}`)))))
      : emptyState('Nothing yet', 'Once a Shortcut or OwnTracks reports in, it shows up here.'));
}

function activity(events) {
  return h('section', { class: 'card' }, h('h2', {}, 'Recent activity'),
    events.length ? h('ul', { class: 'list compact' }, events.map((e) => h('li', { class: `row status-${e.status}` },
      h('span', { class: 'status-dot', 'aria-label': e.status }),
      h('div', { class: 'row-main' },
        h('span', { class: 'row-title' }, `${e.gesture || 'scheduled'} → ${e.detail || ''}`),
        h('span', { class: 'row-sub' }, relTime(e.at), e.error ? ` · ${e.error}` : '')))))
      : emptyState('No activity yet', 'Press the ring or use “Try” on the Now page.'));
}

function backup(refresh) {
  const file = h('input', { type: 'file', accept: 'application/json', class: 'visually-hidden', id: 'import-file' });
  file.addEventListener('change', async () => {
    const f = file.files[0];
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      await post('/catalog/import', data);
      toast('Imported — modes, skills and places replaced');
      refresh();
    } catch (e) { toast(e.message || 'Import failed', 'error'); } finally { file.value = ''; }
  });
  return h('section', { class: 'card' }, h('h2', {}, 'Backup'),
    h('p', { class: 'muted small' }, 'Your modes, skills and places as one JSON file. Notes and reminders stay in the database.'),
    h('div', { class: 'inline-form wrap' },
      h('button', {
        type: 'button', class: 'btn ghost',
        onClick: async () => {
          try {
            const data = await get('/catalog/export');
            const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
            const a = h('a', { href: url, download: `ringhub-catalog-${new Date().toISOString().slice(0, 10)}.json` });
            document.body.append(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          } catch (e) { toast(e.message, 'error'); }
        },
      }, 'Download'),
      h('label', { class: 'btn ghost', for: 'import-file' }, 'Import…'), file));
}

function placeEditor(root, place, places, go) {
  const isNew = !place;
  const p = place ? structuredClone(place) : { id: '', name: '', icon: '📍', aliases: [], lat: null, lon: null, radius_m: 150 };
  const name = h('input', { type: 'text', value: p.name, maxlength: 60, required: true, placeholder: 'e.g. Gym', 'aria-label': 'Name' });
  const icon = h('input', { type: 'text', value: p.icon, maxlength: 8, class: 'icon-input', 'aria-label': 'Icon' });
  const aliases = h('input', { type: 'text', value: p.aliases.join(', '), placeholder: 'the gym, crossfit', 'aria-label': 'Also called' });
  const lat = h('input', { type: 'number', step: 'any', value: p.lat ?? '', placeholder: 'optional', 'aria-label': 'Latitude' });
  const lon = h('input', { type: 'number', step: 'any', value: p.lon ?? '', placeholder: 'optional', 'aria-label': 'Longitude' });
  const radius = h('input', { type: 'number', min: 100, max: 5000, value: p.radius_m, class: 'num', 'aria-label': 'Radius in metres' });
  const error = h('p', { class: 'form-error', role: 'alert' });

  async function save(e) {
    e.preventDefault();
    error.textContent = '';
    const doc = {
      name: name.value.trim(), icon: icon.value.trim() || '📍',
      aliases: aliases.value.split(',').map((x) => x.trim()).filter(Boolean),
      lat: lat.value === '' ? null : Number(lat.value), lon: lon.value === '' ? null : Number(lon.value),
      radius_m: Number(radius.value) || 150,
    };
    if (!doc.name) { error.textContent = 'Give the place a name.'; return; }
    if ((doc.lat === null) !== (doc.lon === null)) { error.textContent = 'Fill in both latitude and longitude, or neither.'; return; }
    const id = isNew ? uniqueId(slugify(doc.name), new Set(places.map((x) => x.id))) : p.id;
    try { await put(`/places/${id}`, doc); toast(isNew ? `${doc.name} added` : 'Saved'); go('setup'); } catch (ex) { error.textContent = ex.message; }
  }

  const deleteBtn = !isNew ? confirmButton('Delete place', 'Tap again to delete', async () => {
    try { await del(`/places/${p.id}`); toast('Place deleted'); go('setup'); } catch (ex) { error.textContent = ex.message; }
  }) : null;

  mount(root, h('form', { class: 'editor', onSubmit: save },
    h('header', { class: 'page-head' }, h('div', {}, h('a', { href: '#/setup', class: 'back' }, '← Setup'), h('h1', {}, isNew ? 'New place' : p.name))),
    h('section', { class: 'card' },
      h('div', { class: 'name-row' }, field('Icon', icon), field('Name', name)),
      field('Also called', aliases, 'What you might say: “the office”, “work”. Used to understand “when I get to the office…”.'),
      isNew ? null : h('p', { class: 'muted small' }, 'Id (used in phone automations): ', h('code', {}, p.id))),
    h('section', { class: 'card' }, h('h2', {}, 'Location (optional)'),
      h('p', { class: 'muted small' }, 'Only needed for OwnTracks, which turns these into regions on your phone. Shortcuts automations pick the location in the Shortcuts app instead. Under 100 m is unreliable on iPhone.'),
      h('div', { class: 'name-row' }, field('Latitude', lat), field('Longitude', lon), field('Radius (m)', radius))),
    error,
    h('div', { class: 'actions sticky' }, deleteBtn, h('span', { class: 'spacer' }),
      h('a', { class: 'btn ghost', href: '#/setup' }, 'Cancel'), h('button', { type: 'submit', class: 'btn primary' }, 'Save'))));
  name.focus();
  return undefined;
}
