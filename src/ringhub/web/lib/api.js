// Thin client for /api/v1. Cookie auth + the X-Ringhub CSRF header on every write.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

const listeners = new Set();
/** Called with no args whenever the server says we're signed out. */
export function onUnauthorized(fn) { listeners.add(fn); }

function describe(detail) {
  if (detail == null) return 'Something went wrong';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // pydantic errors: [{loc: [...], msg: '...'}]
    return detail.map((e) => {
      const loc = (e.loc || []).filter((p) => p !== 'body').join(' › ');
      return loc ? `${loc}: ${e.msg}` : e.msg;
    }).join('\n');
  }
  return JSON.stringify(detail);
}

export async function api(method, path, body) {
  const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (method !== 'GET' && method !== 'HEAD') init.headers['X-Ringhub'] = '1';
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(`/api/v1${path}`, init);
  } catch {
    throw new ApiError(0, "Can't reach the hub. Is it running?");
  }
  const isJson = (res.headers.get('content-type') || '').includes('json');
  const data = isJson ? await res.json() : null;
  if (res.status === 401 && path !== '/session') {
    listeners.forEach((fn) => fn());
  }
  if (!res.ok) throw new ApiError(res.status, describe(data && (data.detail ?? data.error)));
  return data;
}

export const get = (path) => api('GET', path);
export const post = (path, body = {}) => api('POST', path, body);
export const put = (path, body) => api('PUT', path, body);
export const patch = (path, body) => api('PATCH', path, body);
export const del = (path) => api('DELETE', path);
