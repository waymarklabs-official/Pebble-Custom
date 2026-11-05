// Skills: things the ring can do. Built from templates (forms) or custom steps.

import { del, get, post, put } from '../lib/api.js';
import { confirmButton, field, h, mount, slugify, toast, uniqueId } from '../lib/dom.js';
import { schemaForm } from '../lib/schemaform.js';
import { gestureLabel } from '../lib/vocab.js';

const FEATURE_LABEL = { ntfy: 'ntfy', tesla: 'Tesla', devices: 'TVs' };

export async function render(root, { params, go }) {
  const [skills, templates, now] = await Promise.all([get('/skills'), get('/templates'), get('/now')]);
  const features = { ...now.features, devices: now.features.tv };
  const templateName = (id) => (id === 'builtin' ? 'Built-in' : (templates.find((t) => t.id === id) || { name: id }).name);

  if (params[0] === 'new' && !params[1]) return picker(root, templates, features);
  if (params[0] === 'new' && params[1]) {
    const tpl = templates.find((t) => t.id === params[1]);
    if (!tpl) { go('skills/new'); return undefined; }
    return editor(root, { tpl, skill: null, skills, go });
  }
  if (params[0]) {
    const skill = skills.find((s) => s.id === params[0]);
    if (!skill) { go('skills'); return undefined; }
    const tpl = templates.find((t) => t.id === skill.template) || null;
    return editor(root, { tpl, skill, skills, go });
  }

  async function run(s, btn) {
    btn.disabled = true;
    try { await post(`/skills/${s.id}/run`); toast(`Ran ${s.name}`); } catch (e) { toast(e.message, 'error'); } finally { btn.disabled = false; }
  }

  const card = (s) => {
    const runBtn = h('button', { type: 'button', class: 'btn small ghost' }, 'Run');
    runBtn.addEventListener('click', () => run(s, runBtn));
    const runnable = !['capture', 'journal'].includes(s.id);
    return h('li', { class: 'card skill-card' },
      h('a', { href: `#/skills/${s.id}`, class: 'skill-link' },
        h('span', { class: 'skill-icon', 'aria-hidden': 'true' }, s.icon),
        h('span', { class: 'skill-name' }, s.name),
        h('span', { class: 'muted small' }, templateName(s.template))),
      s.description ? h('p', { class: 'small muted clamp' }, s.description) : null,
      s.phrases.length ? h('div', { class: 'phrases' }, s.phrases.slice(0, 3).map((p) => h('span', { class: 'phrase' }, `“${p}”`))) : null,
      h('div', { class: 'skill-foot' },
        s.used_by.length
          ? h('span', { class: 'small' }, s.used_by.slice(0, 3).map((u) => h('span', { class: 'tag' }, `${u.name}: ${gestureLabel(u.gesture)}`)))
          : h('span', { class: 'small muted' }, s.phrases.length ? 'Voice only' : 'Not assigned yet'),
        runnable ? runBtn : null));
  };

  const custom = skills.filter((s) => !s.builtin);
  const builtin = skills.filter((s) => s.builtin);
  mount(root,
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', {}, 'Skills'), h('p', { class: 'muted' }, 'Things the ring can do. Assign them to gestures in Modes, or give them a phrase to say.')),
      h('a', { class: 'btn primary', href: '#/skills/new' }, 'New skill')),
    h('ul', { class: 'skill-grid' }, custom.map(card)),
    h('h2', { class: 'section-title' }, 'Built in'),
    h('ul', { class: 'skill-grid' }, builtin.map(card)),
  );
  return undefined;
}

function picker(root, templates, features) {
  mount(root,
    h('header', { class: 'page-head' }, h('div', {}, h('a', { href: '#/skills', class: 'back' }, '← Skills'), h('h1', {}, 'What should it do?'))),
    h('ul', { class: 'template-grid' }, templates.map((t) => {
      const missing = t.needs.filter((n) => !features[n]);
      return h('li', {}, h('a', { class: 'card template-card', href: `#/skills/new/${t.id}` },
        h('span', { class: 'skill-icon', 'aria-hidden': 'true' }, t.icon),
        h('span', { class: 'skill-name' }, t.name),
        h('span', { class: 'small muted' }, t.description),
        missing.length ? h('span', { class: 'tag warn' }, `Needs ${missing.map((n) => FEATURE_LABEL[n] || n).join(', ')} in config`) : null));
    })));
  return undefined;
}

function editor(root, { tpl, skill, skills, go }) {
  const isNew = !skill;
  const builtin = Boolean(skill?.builtin);
  const s = skill ? structuredClone(skill) : { id: '', name: tpl.name, icon: tpl.icon, description: '', template: tpl.id, params: {}, steps: [], phrases: [] };

  const name = h('input', { type: 'text', value: s.name, maxlength: 60, required: true, 'aria-label': 'Name' });
  const icon = h('input', { type: 'text', value: s.icon, maxlength: 8, class: 'icon-input', 'aria-label': 'Icon' });
  const description = h('input', { type: 'text', value: s.description, maxlength: 300, placeholder: 'Optional', 'aria-label': 'Description' });

  const form = tpl && !builtin && tpl.id !== 'custom' ? schemaForm(tpl.schema, s.params) : null;
  const steps = s.template === 'custom'
    ? h('textarea', { rows: 10, class: 'mono', spellcheck: 'false', 'aria-label': 'Steps (JSON)' }, JSON.stringify(s.steps.length ? s.steps : [{ notify: { message: 'Hello from the ring' } }], null, 2))
    : null;
  const checkResult = h('pre', { class: 'check-result', hidden: true });

  const phraseList = h('div', { class: 'phrase-list' });
  const addPhrase = (value = '') => {
    const input = h('input', { type: 'text', value, placeholder: 'e.g. warm up the car', 'aria-label': 'Phrase' });
    const row = h('div', { class: 'phrase-row' }, input,
      h('button', { type: 'button', class: 'btn icon ghost', 'aria-label': 'Remove phrase', onClick: () => row.remove() }, '✕'));
    row.read = () => input.value.trim();
    phraseList.append(row);
    return input;
  };
  s.phrases.forEach((p) => addPhrase(p));

  const error = h('p', { class: 'form-error', role: 'alert' });

  function collect() {
    const doc = {
      id: s.id, name: name.value.trim(), icon: icon.value.trim() || '✨', description: description.value.trim(),
      template: s.template, params: s.params, steps: [], phrases: [...phraseList.children].map((r) => r.read()).filter(Boolean),
    };
    if (form) doc.params = form.read();
    if (steps) {
      try { doc.steps = JSON.parse(steps.value); } catch { throw new Error('Steps: not valid JSON'); }
      if (!Array.isArray(doc.steps)) throw new Error('Steps must be a JSON list, e.g. [{"notify": "hi"}]');
      doc.params = {};
    }
    if (!doc.name) throw new Error('Give the skill a name.');
    return doc;
  }

  async function check() {
    error.textContent = '';
    try {
      const res = await post('/skills/validate', collect());
      checkResult.hidden = false;
      checkResult.classList.remove('bad');
      checkResult.textContent = `Looks good. Runs:\n${JSON.stringify(res.steps, null, 2)}`;
    } catch (e) {
      checkResult.hidden = false;
      checkResult.classList.add('bad');
      checkResult.textContent = e.message;
    }
  }

  async function save(e) {
    e.preventDefault();
    error.textContent = '';
    let doc;
    try { doc = collect(); } catch (ex) { error.textContent = ex.message; return; }
    if (isNew) doc.id = uniqueId(slugify(doc.name), new Set(skills.map((x) => x.id)));
    try {
      await put(`/skills/${doc.id}`, doc);
      toast(isNew ? `${doc.name} created — assign it in Modes` : 'Saved');
      go('skills');
    } catch (ex) { error.textContent = ex.message; }
  }

  const runBtn = !isNew ? h('button', {
    type: 'button', class: 'btn ghost',
    onClick: async () => { try { await post(`/skills/${s.id}/run`); toast(`Ran ${s.name}`); } catch (ex) { error.textContent = ex.message; } },
  }, 'Run now') : null;
  const deleteBtn = !isNew && !builtin ? confirmButton('Delete', 'Tap again to delete', async () => {
    try { await del(`/skills/${s.id}`); toast('Skill deleted'); go('skills'); } catch (ex) { error.textContent = ex.message; }
  }) : null;

  const usedBy = skill?.used_by?.length
    ? h('p', { class: 'small' }, 'Used in: ', skill.used_by.map((u) => h('a', { class: 'tag', href: `#/modes/${u.mode}` }, `${u.name} · ${gestureLabel(u.gesture)}`)))
    : null;

  mount(root, h('form', { class: 'editor', onSubmit: save },
    h('header', { class: 'page-head' }, h('div', {},
      h('a', { href: '#/skills', class: 'back' }, '← Skills'),
      h('h1', {}, isNew ? `New: ${tpl.name}` : s.name),
      tpl ? h('p', { class: 'muted' }, tpl.description) : null)),
    h('section', { class: 'card' },
      h('div', { class: 'name-row' }, field('Icon', icon), field('Name', name)),
      field('Description', description),
      builtin ? h('p', { class: 'muted small' }, 'Built-in skills power the default gestures. You can rename them, but not change what they do.') : null,
      usedBy),
    form ? h('section', { class: 'card' }, h('h2', {}, 'Settings'), form.el) : null,
    steps ? h('section', { class: 'card' }, h('h2', {}, 'Steps'),
      h('p', { class: 'muted small' }, 'A JSON list of actions, run in order. Templates can use {{ place }}, {{ mode }}, {{ now.strftime("%H:%M") }} and slots from phrases.'),
      steps, h('div', { class: 'inline-form' }, h('button', { type: 'button', class: 'btn small ghost', onClick: check }, 'Check')), checkResult) : null,
    builtin ? null : h('section', { class: 'card' }, h('h2', {}, 'Say it'),
      h('p', { class: 'muted small' }, 'Hold the ring and say one of these to run the skill from any mode. The whole sentence has to match. Use {name} for a word you fill in, e.g. “tell {who} I\'m running late”, or {minutes:number}.'),
      phraseList, h('button', { type: 'button', class: 'btn small ghost', onClick: () => addPhrase().focus() }, '+ Add phrase')),
    error,
    h('div', { class: 'actions sticky' }, deleteBtn, runBtn, h('span', { class: 'spacer' }),
      h('a', { class: 'btn ghost', href: '#/skills' }, 'Cancel'), h('button', { type: 'submit', class: 'btn primary' }, 'Save'))));
  return undefined;
}
