// ringhub dashboard: shell, sign-in and hash router.

import { ApiError, del, get, onUnauthorized, post } from './lib/api.js';
import { clear, h, icon, mount, toast } from './lib/dom.js';
import * as memory from './views/memory.js';
import * as modes from './views/modes.js';
import * as now from './views/now.js';
import * as setup from './views/setup.js';
import * as skills from './views/skills.js';

const ROUTES = [
  { path: 'now', label: 'Now', view: now },
  { path: 'modes', label: 'Modes', view: modes },
  { path: 'skills', label: 'Skills', view: skills },
  { path: 'memory', label: 'Memory', view: memory },
  { path: 'setup', label: 'Setup', view: setup },
];

const root = document.getElementById('app');
let cleanup = null;
let renderToken = 0;

function currentRoute() {
  const [path, ...rest] = location.hash.replace(/^#\/?/, '').split('/');
  const route = ROUTES.find((r) => r.path === path) || ROUTES[0];
  return { route, params: rest.map(decodeURIComponent) };
}

export function go(path) {
  location.hash = `#/${path}`;
}

function shell() {
  const nav = h('nav', { class: 'nav', 'aria-label': 'Sections' },
    h('div', { class: 'brand' }, h('span', { class: 'brand-mark' }, '◎'), h('span', {}, 'ringhub')),
    ROUTES.map((r) => h('a', { href: `#/${r.path}`, class: 'nav-item', dataset: { path: r.path } },
      h('span', { class: 'nav-icon' }, icon(r.path)), h('span', { class: 'nav-label' }, r.label))),
  );
  const main = h('main', { id: 'view', class: 'view', tabindex: '-1' });
  mount(root, nav, main);
  return main;
}

async function render() {
  const token = ++renderToken;
  if (cleanup) { cleanup(); cleanup = null; }
  let main = document.getElementById('view');
  if (!main) main = shell();
  const { route, params } = currentRoute();
  document.querySelectorAll('.nav-item').forEach((a) => a.classList.toggle('active', a.dataset.path === route.path));
  document.title = `${route.label} · ringhub`;
  mount(main, h('div', { class: 'loading' }, 'Loading…'));
  try {
    const result = await route.view.render(main, { params, go, refresh: render });
    if (token !== renderToken) { if (typeof result === 'function') result(); return; }
    cleanup = typeof result === 'function' ? result : null;
  } catch (err) {
    if (token !== renderToken) return;
    if (err instanceof ApiError && err.status === 401) return;
    mount(main, h('div', { class: 'card error-card' }, h('h2', {}, "Couldn't load this page"), h('p', {}, err.message)));
  }
}

function loginScreen(message) {
  if (cleanup) { cleanup(); cleanup = null; }
  const input = h('input', {
    type: 'password', autocomplete: 'current-password', placeholder: 'Admin token', required: true, autofocus: true,
    'aria-label': 'Admin token',
  });
  const err = h('p', { class: 'form-error', role: 'alert' }, message || '');
  const form = h('form', {
    class: 'login card',
    onSubmit: async (e) => {
      e.preventDefault();
      err.textContent = '';
      try {
        await post('/session', { token: input.value });
        clear(root);
        await render();
      } catch (ex) {
        err.textContent = ex.status === 401 ? "That token didn't work." : ex.message;
        input.select();
      }
    },
  },
  h('div', { class: 'brand big' }, h('span', { class: 'brand-mark' }, '◎'), h('span', {}, 'ringhub')),
  h('p', { class: 'muted' }, 'Sign in with the admin token from your config (server.admin_token).'),
  input, err, h('button', { class: 'btn primary wide', type: 'submit' }, 'Sign in'));
  mount(root, h('div', { class: 'login-wrap' }, form));
  input.focus();
}

async function start() {
  onUnauthorized(() => loginScreen('Your session ended. Sign in again.'));
  let session;
  try {
    session = await get('/session');
  } catch (e) {
    mount(root, h('div', { class: 'login-wrap' }, h('div', { class: 'card' }, h('p', {}, e.message))));
    return;
  }
  if (!session.enabled) {
    mount(root, h('div', { class: 'login-wrap' }, h('div', { class: 'card' },
      h('h2', {}, 'Dashboard is off'), h('p', {}, 'Set server.admin_token in config.yaml and restart the hub.'))));
    return;
  }
  if (!session.authenticated) { loginScreen(); return; }
  await render();
}

export async function logout() {
  try { await del('/session'); } catch { /* signed out anyway */ }
  toast('Signed out');
  loginScreen();
}

window.addEventListener('hashchange', () => { if (document.getElementById('view')) render(); });
start();
