"""JavaScript evaluated inside each frame by the web surface.

Kept in one place so the in-page logic can be read (and reviewed) on its own. Every script
is a single arrow function taking one JSON-serialisable argument.
"""

# Shared helpers, prepended to every script.
_HELPERS = r"""
const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
const isVisible = (el) => {
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
};
const INTERACTIVE = 'a[href], button, input:not([type=hidden]), select, textarea, [onclick], ' +
                    '[role=button], [role=link], [role=checkbox], [role=tab], [role=menuitem]';
const roleOf = (el) => {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  if (tag === 'a' && el.hasAttribute('href')) return 'link';
  if (tag === 'button') return 'button';
  if (tag === 'input') {
    if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
    if (type === 'checkbox' || type === 'radio') return type;
    if (type === 'password') return 'password';
    return 'textbox';
  }
  if (tag === 'select') return 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (el.hasAttribute('onclick')) return 'clickable';
  if (tag === 'td') return 'cell';
  if (tag === 'th') return 'columnheader';
  return 'text';
};
const explicitLabel = (el) => {
  if (el.id) {
    const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (l) return clean(l.innerText);
  }
  const wrap = el.closest('label');
  if (wrap) return clean(wrap.innerText);
  return clean(el.getAttribute('aria-label') || el.getAttribute('title') || '');
};
const nameOf = (el, role) => {
  const label = explicitLabel(el);
  if (label) return label;
  if (role === 'button' && el.tagName === 'INPUT') return clean(el.value);
  if (['link', 'button', 'clickable', 'cell', 'columnheader', 'text', 'tab', 'menuitem']
      .includes(role)) return clean(el.innerText).slice(0, 120);
  return '';
};
const rowOf = (el) => el.closest('tr');
// Text of the first other non-empty cell in the element's innermost row (visual label).
const rowAnchor = (el) => {
  const row = rowOf(el);
  if (!row) return '';
  for (const cell of row.cells) {
    if (cell.contains(el)) continue;
    const t = clean(cell.innerText);
    if (t) return t;
  }
  return '';
};
// A header row is one made of <th> cells, or one where every cell is rendered bold
// (legacy grids style header rows instead of using <th>).
const isHeaderRow = (r) => r.cells.length > 0 && Array.from(r.cells).every((c) =>
  c.tagName === 'TH' || parseInt(getComputedStyle(c).fontWeight, 10) >= 600);
const headerFor = (cell) => {
  const table = cell.closest('table');
  if (!table) return '';
  const idx = cell.cellIndex;
  for (const r of table.rows) {
    if (r.contains(cell)) break;
    const c = r.cells[idx];
    if (c && isHeaderRow(r)) {
      const t = clean(c.innerText);
      if (t) return t;
    }
  }
  return '';
};
"""


def _script(body: str) -> str:
    return "(arg) => {\n" + _HELPERS + body + "\n}"


OBSERVE = _script(
    r"""
const { start, max } = arg;
document.querySelectorAll('[data-cua-ref]').forEach((e) => e.removeAttribute('data-cua-ref'));
const picked = [];
const seen = new Set();
const add = (el) => { if (!seen.has(el) && isVisible(el)) { seen.add(el); picked.push(el); } };
document.querySelectorAll(INTERACTIVE).forEach(add);
// Readable leaves: table cells without nested tables/controls, and inline text outside cells.
document.querySelectorAll('td, th').forEach((c) => {
  if (!c.querySelector('table') && !c.querySelector(INTERACTIVE) && clean(c.innerText)) add(c);
});
document.querySelectorAll('b, strong, font, p, h1, h2, h3, h4, div, span, label').forEach((el) => {
  if (el.closest('td, th, a, button, [onclick]') || el.querySelector(INTERACTIVE)) return;
  const own = clean(Array.from(el.childNodes).filter((n) => n.nodeType === 3)
                      .map((n) => n.textContent).join(' '));
  if (own) add(el);
});
const out = [];
for (const el of picked.slice(0, max)) {
  const ref = 'e' + (start + out.length + 1);
  el.setAttribute('data-cua-ref', ref);
  const role = roleOf(el);
  const tag = el.tagName.toLowerCase();
  const row = rowOf(el);
  let value = null;
  if (['input', 'textarea', 'select'].includes(tag) && role !== 'button') {
    value = role === 'password' ? (el.value ? '********' : '') : el.value;
  }
  out.push({
    ref, tag, role,
    name: nameOf(el, role),
    text: clean(el.innerText || '').slice(0, 120),
    value,
    row: row ? clean(row.innerText).slice(0, 120) : null,
    column: (tag === 'td' || tag === 'th') ? headerFor(el) || null : null,
    disabled: !!el.disabled,
    checked: (role === 'checkbox' || role === 'radio') ? !!el.checked : null,
    options: tag === 'select' ? Array.from(el.options).slice(0, 25).map((o) => clean(o.text)) : [],
  });
}
return out;
"""
)

DESCRIBE = _script(
    r"""
const el = document.querySelector('[data-cua-ref="' + arg + '"]');
if (!el) return null;
const role = roleOf(el);
const tag = el.tagName.toLowerCase();
const cssPath = (node) => {
  const parts = [];
  let cur = node;
  while (cur && cur.nodeType === 1 && cur !== document.body) {
    if (cur.tagName === 'FORM' && cur.getAttribute('name')) {
      parts.unshift('form[name="' + cur.getAttribute('name') + '"]');
      return parts.join(' > ');
    }
    let part = cur.tagName.toLowerCase();
    const same = Array.from(cur.parentElement.children).filter((s) => s.tagName === cur.tagName);
    if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
    parts.unshift(part);
    cur = cur.parentElement;
  }
  parts.unshift('body');
  return parts.join(' > ');
};
let cell = null;
if (tag === 'td' || tag === 'th') cell = el;
else if (el.closest('td') && el.closest('td').querySelectorAll(INTERACTIVE).length === 1) {
  cell = el.closest('td');
}
let tableCell = null;
if (cell) {
  const header = headerFor(cell);
  const row = rowOf(cell);
  let rowText = '';
  if (row) {
    for (const c of row.cells) {
      const t = clean(c.innerText);
      if (t && t !== header) { rowText = t; break; }
    }
  }
  if (header && rowText) tableCell = { row_text: rowText, column_header: header };
}
const ownText = clean(el.innerText || '');
return {
  tag, role,
  name: nameOf(el, role),
  label: explicitLabel(el),
  text: ownText.length <= 60 ? ownText : '',
  anchor: rowAnchor(el),
  table_cell: tableCell,
  attrs: {
    name: el.getAttribute('name'),
    href: el.getAttribute('href'),
    value: tag === 'input' && role === 'button' ? el.getAttribute('value') : null,
  },
  css: cssPath(el),
};
"""
)

MARK_TABLE_CELL = _script(
    r"""
const { rowText, columnHeader, token } = arg;
let n = 0;
for (const table of document.querySelectorAll('table')) {
  let col = -1;
  for (const r of table.rows) {
    if (!isHeaderRow(r)) continue;
    for (const c of r.cells) {
      if (clean(c.innerText) === columnHeader) { col = c.cellIndex; break; }
    }
    if (col >= 0) break;
  }
  if (col < 0) continue;
  for (const r of table.rows) {
    if (!Array.from(r.cells).some((c) => clean(c.innerText) === rowText)) continue;
    const cell = r.cells[col];
    if (!cell || clean(cell.innerText) === columnHeader) continue;
    const controls = cell.querySelectorAll(INTERACTIVE);
    const target = controls.length === 1 ? controls[0] : cell;
    target.setAttribute('data-cua-cell', token);
    n += 1;
  }
}
return n;
"""
)

CLEAR_MARKS = _script(
    r"""
document.querySelectorAll('[' + arg + ']').forEach((e) => e.removeAttribute(arg));
return true;
"""
)

MARK_TEXT_MATCHES = _script(
    r"""
const regexes = arg.map((p) => new RegExp(p));
document.querySelectorAll('[data-cua-mask]').forEach((e) => e.removeAttribute('data-cua-mask'));
let n = 0;
const walker = document.createTreeWalker(document.body || document.documentElement,
                                         NodeFilter.SHOW_TEXT);
let node;
while ((node = walker.nextNode())) {
  const text = node.textContent || '';
  if (regexes.some((r) => r.test(text)) && node.parentElement) {
    node.parentElement.setAttribute('data-cua-mask', '1');
    n += 1;
  }
}
return n;
"""
)

BODY_TEXT = "() => (document.body || document.documentElement).innerText || ''"
