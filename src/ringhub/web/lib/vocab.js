// Shared vocabulary: gesture names and plain-language summaries of modes.

export const GESTURES = [
  { key: 'click', label: 'Click', glyph: '●' },
  { key: 'double-click', label: 'Double', glyph: '●●' },
  { key: 'triple-click', label: 'Triple', glyph: '●●●' },
  { key: 'single-click-hold', label: 'Hold & talk', glyph: '●━', voice: true },
  { key: 'double-click-hold', label: 'Click, hold & talk', glyph: '●●━', voice: true },
];

export const QUICK_GESTURES = GESTURES.filter((g) => !g.voice).map((g) => g.key);

export function gestureLabel(key) {
  return key.split('+').map((k) => (GESTURES.find((g) => g.key === k) || { label: k }).label).join(' then ');
}

export const DAYS = [
  { value: 'mon', label: 'Mon' }, { value: 'tue', label: 'Tue' }, { value: 'wed', label: 'Wed' },
  { value: 'thu', label: 'Thu' }, { value: 'fri', label: 'Fri' }, { value: 'sat', label: 'Sat' },
  { value: 'sun', label: 'Sun' },
];

export const FOCUS_PRESETS = ['sleep', 'work', 'driving', 'personal', 'fitness', 'reading'];

export const FALLBACK = 'everywhere';

/** "At Home · 05:00–10:30 · weekdays" */
export function summarizeWhen(when, placeName) {
  if (!when) return 'Always';
  const parts = [];
  if (when.places?.length) parts.push(`At ${when.places.map(placeName).join(' or ')}`);
  if (when.not_places?.length) parts.push(`Not at ${when.not_places.map(placeName).join(' or ')}`);
  if (when.focus?.length) parts.push(`${when.focus.map(cap).join(' or ')} Focus`);
  if (when.time) parts.push(`${when.time.after}–${when.time.before}`);
  if (when.days?.length) {
    const d = when.days;
    const weekdays = ['mon', 'tue', 'wed', 'thu', 'fri'];
    if (d.length === 5 && weekdays.every((x) => d.includes(x))) parts.push('weekdays');
    else if (d.length === 2 && d.includes('sat') && d.includes('sun')) parts.push('weekends');
    else parts.push(d.map(cap).join(', '));
  }
  if (when.recently) {
    const verb = when.recently.event === 'leave' ? 'leaving' : 'arriving at';
    parts.push(`${when.recently.within_minutes} min after ${verb} ${placeName(when.recently.place)}`);
  }
  for (const [k, v] of Object.entries(when.context || {})) parts.push(`${k} = ${JSON.stringify(v)}`);
  return parts.length ? parts.join(' · ') : 'Always';
}

export const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
