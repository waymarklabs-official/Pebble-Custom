// Render a form from a pydantic JSON Schema (the skill templates), and read it back.
// Supports: string, integer/number, boolean, enum, array-of-string, object (as JSON), nullable.

import { field, h } from './dom.js';

function resolve(schema, root) {
  if (schema && schema.$ref) {
    const name = schema.$ref.split('/').pop();
    return { ...root.$defs[name], ...schema, $ref: undefined };
  }
  return schema;
}

function unwrap(prop, root) {
  let s = resolve(prop, root);
  let nullable = false;
  if (s.anyOf) {
    const nonNull = s.anyOf.filter((x) => x.type !== 'null');
    nullable = nonNull.length !== s.anyOf.length;
    s = { ...resolve(nonNull[0], root), title: s.title, description: s.description, default: s.default };
  }
  return { s, nullable };
}

/**
 * @returns {{el: HTMLElement, read: () => object}} read() throws Error with a readable message.
 */
export function schemaForm(schema, values = {}) {
  const props = schema.properties || {};
  const required = new Set(schema.required || []);
  const readers = [];
  const el = h('div', { class: 'schema-form' });

  for (const [name, raw] of Object.entries(props)) {
    const { s, nullable } = unwrap(raw, schema);
    const label = (s.title || name) + (required.has(name) ? '' : '');
    const current = values[name] ?? s.default;
    let control;
    let read;

    if (s.enum) {
      control = h('select', {}, s.enum.map((v) => h('option', { value: v, selected: v === current }, prettify(v))));
      read = () => control.value;
    } else if (s.type === 'boolean') {
      control = h('input', { type: 'checkbox', checked: Boolean(current) });
      read = () => control.checked;
    } else if (s.type === 'integer' || s.type === 'number') {
      control = h('input', {
        type: 'number', inputmode: 'numeric', value: current ?? '',
        min: s.minimum, max: s.maximum, step: s.type === 'integer' ? 1 : 'any',
      });
      read = () => {
        if (control.value === '') {
          if (nullable || !required.has(name)) return undefined;
          throw new Error(`${label} is required`);
        }
        return s.type === 'integer' ? parseInt(control.value, 10) : Number(control.value);
      };
    } else if (s.type === 'array') {
      control = h('input', { type: 'text', value: (current || []).join(', '), placeholder: 'comma, separated' });
      read = () => control.value.split(',').map((x) => x.trim()).filter(Boolean);
    } else if (s.type === 'object') {
      control = h('textarea', { rows: 3, spellcheck: 'false', class: 'mono', placeholder: '{}' },
        current && Object.keys(current).length ? JSON.stringify(current, null, 2) : '');
      read = () => {
        const text = control.value.trim();
        if (!text) return nullable ? undefined : {};
        let parsed;
        try { parsed = JSON.parse(text); } catch { throw new Error(`${label}: not valid JSON`); }
        if (typeof parsed !== 'object' || Array.isArray(parsed) || parsed === null) {
          throw new Error(`${label}: must be a JSON object`);
        }
        return parsed;
      };
    } else {
      const long = (s.maxLength || 0) > 120;
      control = long
        ? h('textarea', { rows: 3 }, current ?? '')
        : h('input', { type: 'text', value: current ?? '', maxlength: s.maxLength, autocapitalize: 'off' });
      read = () => {
        const v = control.value.trim();
        if (!v) {
          if (required.has(name)) throw new Error(`${label} is required`);
          return undefined;
        }
        return v;
      };
    }
    el.append(field(label, control, s.description));
    readers.push([name, read]);
  }

  return {
    el,
    read() {
      const out = {};
      for (const [name, read] of readers) {
        const v = read();
        if (v !== undefined) out[name] = v;
      }
      return out;
    },
  };
}

function prettify(v) {
  return String(v).replace(/_/g, ' ');
}
