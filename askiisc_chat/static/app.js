// AskIISc chat page logic. Moved out of index.html so the Content-Security-Policy can forbid inline scripts
// (script-src 'self', DEFECT-55).
const $ = s => document.querySelector(s);
const log = $('#log');
let history = [];
// Gated via server /me response; disabled in production to prevent information disclosure
let DEBUG = false;

// -------------------------------------------------------------------------
// CSRF Token Helper (Double-submit cookie pattern)
// -------------------------------------------------------------------------
function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : (window.__csrf_token || '');
}

// -------------------------------------------------------------------------
// HTML Sanitizer (Zero-dependency defense against stored & reflected XSS)
// -------------------------------------------------------------------------
function escapeHTML(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function sanitizeHTML(dirty) {
  if (!dirty) return '';
  const doc = new DOMParser().parseFromString(dirty, 'text/html');
  const blockedTags = new Set([
    'SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'BASE', 'META',
    'FORM', 'INPUT', 'BUTTON', 'TEXTAREA', 'SELECT', 'SVG', 'LINK', 'APPLET'
  ]);
  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_ELEMENT);
  const toRemove = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (blockedTags.has(node.tagName.toUpperCase())) {   // SVG elements report a lowercase tagName
      toRemove.push(node);
      continue;
    }
    for (const attr of Array.from(node.attributes)) {
      const name = attr.name.toLowerCase();
      const val = attr.value.toLowerCase().replace(/[\s\u0000-\u001f]/g, '');   // "java\tscript:" is javascript:
      // Strip inline event handlers, and style/id/class (DEFECT-57)
      if (name.startsWith('on') || name === 'style' || name === 'id' || name === 'class') {
        node.removeAttribute(attr.name);
        continue;
      }
      // Strip dangerous pseudo-protocol URLs
      if ((name === 'href' || name === 'src' || name === 'action') &&
          (val.startsWith('javascript:') || val.startsWith('vbscript:') || val.startsWith('data:text/html'))) {
        node.removeAttribute(attr.name);
      }
    }
  }
  for (const node of toRemove) {
    node.remove();
  }
  return doc.body.innerHTML;
}

// -------------------------------------------------------------------------
// LaTeX & Markdown Formatter
// -------------------------------------------------------------------------
// DEFECT-53: returns a DocumentFragment, not an HTML string. Math is cut out before Markdown and put back
// afterwards as DOM nodes, only where a placeholder is a text node. It used to be put back by string
// replacement after sanitising, with a fixed placeholder an answer could type: a placeholder inside an
// attribute (an image's alt text) then received the math block's HTML, whose quotes ended the attribute,
// so an answer could carry an onerror handler (stored XSS, shared through the cache).
function renderMathAndMarkdown(text) {
  const mathBlocks = [];   // { html: KaTeX output or null, src: formula text, display: bool }
  if (!text) return document.createDocumentFragment();
  // Unguessable per render, letters and digits only (Markdown leaves it alone); answer text cannot contain it.
  const rnd = new Uint32Array(3);
  if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(rnd);
  else for (let i = 0; i < rnd.length; i++) rnd[i] = Math.floor(Math.random() * 4294967296);
  const tag = 'MQ' + Array.from(rnd, n => n.toString(36)).join('Z') + 'Q';
  const hold = (html, src, display) => { mathBlocks.push({ html, src, display }); return tag + (mathBlocks.length - 1) + 'X'; };

  // Step 1: Extract Display Math ($$ ... $$ or \[ ... \] or \begin{...} ... \end{...})
  let processed = text.replace(
    /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\begin\{(equation|align|gather|split|matrix|bmatrix|pmatrix|cases)\*?\}([\s\S]+?)\\end\{\3\*?\}/g,
    (match, p1, p2, env, p3) => {
      const formula = p1 || p2 || (env ? `\\begin{${env}}${p3}\\end{${env}}` : match);
      try {
        if (window.katex) {
          const rendered = window.katex.renderToString(formula.trim(), {
            displayMode: true,
            throwOnError: false
          });
          return `\n\n${hold(rendered, formula, true)}\n\n`;
        }
      } catch (e) {}
      return `\n\n${hold(null, formula, true)}\n\n`;   // shown as plain text, never as HTML (DEFECT-48)
    }
  );

  // Step 2: Extract Inline Math ($ ... $ or \( ... \))
  processed = processed.replace(
    /(?<!\\)\$([^\$\n]+?)(?<!\\)\$|\\\(([^\)]+?)\\\)/g,
    (match, p1, p2) => {
      const formula = p1 || p2;
      // Avoid false positives on currency numbers like $100 or empty
      if (!formula || /^\s*\d+([.,]\d+)?\s*$/.test(formula)) return match;
      try {
        if (window.katex) {
          const rendered = window.katex.renderToString(formula.trim(), {
            displayMode: false,
            throwOnError: false
          });
          return hold(rendered, formula, false);
        }
      } catch (e) {}
      return hold(null, formula, false);   // e.g. KaTeX RangeError on deep nesting: plain text
    }
  );

  // Step 3: Parse Markdown using Marked
  let html = '';
  if (window.marked && typeof window.marked.parse === 'function') {
    try {
      html = window.marked.parse(processed, { breaks: true, gfm: true });
    } catch (e) {
      html = processed;
    }
  } else {
    html = processed
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\n\n/g, '<br><br>')
      .replace(/\n/g, '<br>');
  }

  // Step 3.5: Sanitize Markdown HTML output with DOMPurify (with robust native fallback)
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    html = window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'base', 'meta', 'link', 'form', 'input'],
      // DEFECT-57: no style/id/class from answer text: a positioned overlay ("session expired, sign in here"),
      // an id that shadows a page control (#go, #q) or a class borrowed from the page's own UI. KaTeX blocks are
      // sanitised separately and keep their layout styles.
      FORBID_ATTR: ['onerror', 'onload', 'onclick', 'onmouseover', 'onfocus', 'onblur', 'style', 'id', 'class']
    });
  } else {
    html = sanitizeHTML(html);
  }

  // Step 4: Restore math as DOM nodes (DEFECT-53). Each KaTeX block is sanitised on its own (it emits
  // MathML/SVG) and inserted only in place of a placeholder text node, never into markup or attributes.
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  // DEFECT-58: instructions planted in a retrieved document can make the 7B model add an image or a link to its
  // answer (6/9 runs even with the prompt rules). Answers never load images (math is text, sources are listed
  // separately), and only links to this site or *.iisc.ac.in stay clickable; any other link is shown as plain text.
  for (const img of Array.from(tpl.content.querySelectorAll('img'))) {
    const alt = (img.getAttribute('alt') || '').trim();
    img.replaceWith(document.createTextNode(alt ? `[image: ${alt}]` : '[image]'));
  }
  for (const a of Array.from(tpl.content.querySelectorAll('a[href]'))) {
    const href = a.getAttribute('href') || '';
    let keep = href.startsWith('#');
    try {
      const u = new URL(href, location.href);
      keep = keep || (u.origin === location.origin && location.origin !== 'null') ||
        (/^https?:$/.test(u.protocol) && /(^|\.)iisc\.ac\.in$/i.test(u.hostname));
    } catch (e) {}
    if (!keep) {
      const label = a.textContent.trim();
      a.replaceWith(document.createTextNode(label && label !== href ? `${label} (${href})` : href));
    }
  }
  const placeholder = new RegExp(tag + '(\\d+)X', 'g');
  const block = i => mathBlocks[Number(i)] || { html: null, src: '', display: false };
  const URL_ATTRS = new Set(['href', 'src', 'srcset', 'action', 'formaction', 'xlink:href', 'background', 'poster', 'cite']);
  for (const el of tpl.content.querySelectorAll('*')) {
    for (const a of Array.from(el.attributes)) {
      if (!a.value.includes(tag)) continue;
      // math written inside a link or image address: drop the address; elsewhere (alt, title): plain formula text
      if (URL_ATTRS.has(a.name.toLowerCase())) el.removeAttribute(a.name);
      else el.setAttribute(a.name, a.value.replace(placeholder, (m, i) => '$' + block(i).src + '$'));
    }
  }
  const mathNode = b => {
    if (b.html && window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
      const t = document.createElement('template');
      t.innerHTML = window.DOMPurify.sanitize(b.html, { USE_PROFILES: { html: true, mathMl: true, svg: true } });
      return t.content;
    }
    const plain = b.display ? '$$' + b.src + '$$' : '$' + b.src + '$';
    if (!b.display) return document.createTextNode(plain);
    const div = document.createElement('div');
    div.className = 'katex-display';
    div.textContent = plain;
    return div;
  };
  const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_TEXT);
  const hits = [];
  while (walker.nextNode()) if (walker.currentNode.nodeValue.includes(tag)) hits.push(walker.currentNode);
  for (const node of hits) {
    const out = document.createDocumentFragment();
    node.nodeValue.split(placeholder).forEach((part, k) => {   // [text, index, text, index, ..., text]
      if (k % 2) out.append(mathNode(block(part)));
      else if (part) out.append(document.createTextNode(part));
    });
    node.replaceWith(out);
  }
  return tpl.content;
}

// -------------------------------------------------------------------------
// SSE Stream Parser
// -------------------------------------------------------------------------
function parseSSEBlock(blk) {
  if (!blk) return null;
  let event = null;
  let dataLines = [];
  const lines = blk.split(/\r?\n/);
  for (const line of lines) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!dataLines.length) return null;
  const rawData = dataLines.join('\n');
  if (rawData === '[DONE]') {
    return { event: 'done', data: { done: true } };
  }
  try {
    const data = JSON.parse(rawData);
    if (!event) {
      if (data.type) event = data.type;
      else if (data.t !== undefined || data.delta !== undefined || data.content !== undefined || data.token !== undefined) event = 'token';
      else if (data.chunks || data.citations) event = 'sources';
    }
    return { event, data };
  } catch (err) {
    return null;
  }
}

// -------------------------------------------------------------------------
// UI Helpers
// -------------------------------------------------------------------------
// Icons are constant markup written here, never built from server or answer text.
const ICONS = {
  send: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>',
  stop: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2.5"/></svg>',
  copy: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  check: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  trash: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m6 6 1 14h10l1-14"/></svg>'
};
function icon(name) {
  const s = document.createElement('span');
  s.className = 'ic';
  s.innerHTML = ICONS[name];
  return s;
}
const div = (cls, txt) => {
  const d = document.createElement('div');
  if (cls) d.className = cls;
  if (txt !== undefined) d.textContent = txt;
  return d;
};

const GEN_LABEL = 'Send code';
const VER_LABEL = 'Verify &amp; continue';
const RESEND_LABEL = 'Resend code';
const MAX_CHATS = 30;     // chats kept in this browser per signed-in user
const MAX_MSGS = 40;      // messages kept per chat
const scroller = $('#scroll');
const mobile = () => window.matchMedia('(max-width: 767px)').matches;

let me = { email: null };
let chats = [];           // [{ id, title, updated, msgs: [{ role, text, route?, notes?, sources? }] }]
let currentId = null;
let busy = false;
let controller = null;

// Keep the view pinned to the newest text unless the reader has scrolled up.
let stick = true;
scroller.addEventListener('scroll', () => {
  stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
});
function toBottom(force) {
  if (force || stick) scroller.scrollTop = scroller.scrollHeight;
}
function updateEmpty() {
  $('#chat').classList.toggle('empty', !log.children.length);
}

function addUserTurn(q) {
  const t = div('turn user');
  t.append(div('bubble', q));
  log.append(t);
  updateEmpty();
  stick = true;
  toBottom(true);
}

function addBotTurn() {
  const t = div('turn bot');
  const avatar = div('avatar', 'A');
  avatar.setAttribute('aria-hidden', 'true');
  const box = div('msg');
  const head = div('head');
  const body = div('body');
  const foot = div('note foot');
  const actions = div('actions');
  box.append(head, body, foot, actions);
  t.append(avatar, box);
  log.append(t);
  updateEmpty();
  toBottom();
  return { head, body, foot, actions };
}

function badge(label, cls) {
  const b = document.createElement('span');
  b.className = 'badge ' + cls;
  b.textContent = label;
  return b;
}
const ROUTES = { slm: 'SLM', llm: 'LLM', cache: 'Cached answer' };
function routeBadge(route) {
  return badge(ROUTES[route] || 'LLM', ROUTES[route] ? route : 'llm');
}

function typingDots() {
  const d = div('dots');
  d.setAttribute('aria-label', 'Thinking');
  d.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));
  return d;
}

// navigator.clipboard needs HTTPS or localhost; the campus page is plain HTTP, so fall back to execCommand.
function copyText(s) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(s);
  const ta = document.createElement('textarea');
  ta.value = s;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.append(ta);
  ta.select();
  try { document.execCommand('copy'); } finally { ta.remove(); }
  return Promise.resolve();
}
function addCopyButton(actions, s) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'icon-btn';
  b.title = 'Copy';
  b.setAttribute('aria-label', 'Copy answer');
  b.append(icon('copy'));
  b.onclick = async () => {
    try {
      await copyText(s);
      b.replaceChildren(icon('check'));
      setTimeout(() => b.replaceChildren(icon('copy')), 1500);
    } catch (e) {}
  };
  actions.prepend(b);
}

// The [Document n] numbers a source covers (several passages of one thesis are one source).
function docNumbers(c) {
  return Array.isArray(c && c.documents) ? c.documents.filter(n => Number.isInteger(n) && n > 0 && n <= 50) : [];
}

// Sources list, built from text only. `list` is already de-duplicated.
function sourcesEl(list) {
  const dt = document.createElement('details');
  dt.className = 'sources';
  const sm = document.createElement('summary');
  sm.textContent = list.length + ' source' + (list.length > 1 ? 's' : '');
  dt.append(sm);
  list.forEach((c, idx) => {
    const docs = docNumbers(c);
    dt.append(div('citation-card',
      `[${idx + 1}] ${c.title || c.thesis_id || 'Untitled Document'}` +
      (docs.length ? ` · ${docs.length > 1 ? 'Documents' : 'Document'} ${docs.join(', ')}` : '') +
      (c.chapter ? ` · ${c.chapter}` : '') +
      (c.version ? ` · v${c.version}` : '') +
      (DEBUG && c.distance ? ` (dist: ${Number(c.distance).toFixed(3)})` : '') +
      (c.text ? `\n${c.text}…` : '')));
  });
  return dt;
}

let cd = null;
function showGate(on) {
  $('#gate').style.display = on ? 'flex' : 'none';
  $('#chat').style.display = on ? 'none' : 'flex';
}

async function post(u, b) {
  const headers = { 'Content-Type': 'application/json' };
  const csrf = getCsrfToken();
  if (csrf) headers['X-CSRF-Token'] = csrf;
  const r = await fetch(u, {
    method: 'POST',
    headers: headers,
    body: JSON.stringify(b)
  });
  let d = {};
  try { d = await r.json(); } catch (_) {}
  return { ok: r.ok, status: r.status, d };
}

// -------------------------------------------------------------------------
// Chat list (this browser only; cleared on sign-out)
// -------------------------------------------------------------------------
const storeKey = () => 'askiisc.chats.' + (me.email || 'open');
function loadChats() {
  try {
    const v = JSON.parse(localStorage.getItem(storeKey()) || '[]');
    chats = Array.isArray(v) ? v.filter(c => c && typeof c.id === 'string' && Array.isArray(c.msgs)) : [];
  } catch (e) {
    chats = [];
  }
}
function saveChats() {
  chats.sort((a, b) => (b.updated || 0) - (a.updated || 0));
  chats = chats.slice(0, MAX_CHATS);
  try { localStorage.setItem(storeKey(), JSON.stringify(chats)); } catch (e) {}
}
function forgetChats() {
  try { localStorage.removeItem(storeKey()); } catch (e) {}
}

function renderHistory() {
  const nav = $('#hist');
  nav.replaceChildren();
  $('#hist-label').hidden = !chats.length;
  for (const c of chats) {
    const item = div('hist-item' + (c.id === currentId ? ' active' : ''));
    item.tabIndex = 0;
    item.setAttribute('role', 'button');
    const title = String(c.title || 'Untitled');
    item.title = title;
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = title;
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'icon-btn del';
    del.title = 'Delete chat';
    del.setAttribute('aria-label', 'Delete chat');
    del.append(icon('trash'));
    del.onclick = e => { e.stopPropagation(); deleteChat(c.id); };
    item.onclick = () => openChat(c.id);
    item.onkeydown = e => { if (e.key === 'Enter') openChat(c.id); };
    item.append(t, del);
    nav.append(item);
  }
}

function closeSideOnMobile() {
  document.body.classList.remove('side-open');
}

function newChat() {
  if (busy) return;
  currentId = null;
  history = [];
  log.replaceChildren();
  updateEmpty();
  renderHistory();
  closeSideOnMobile();
  $('#q').focus();
}

function renderSavedAnswer(m) {
  const t = addBotTurn();
  if (m.route) t.head.append(routeBadge(m.route));
  (Array.isArray(m.notes) ? m.notes : []).forEach(n => t.foot.before(div('note fix-note', String(n))));
  const text = String(m.text || '');
  t.body.replaceChildren(renderMathAndMarkdown(text));
  if (Array.isArray(m.sources) && m.sources.length) t.foot.before(sourcesEl(m.sources));
  addCopyButton(t.actions, text);
}

function openChat(id) {
  if (busy) return;
  const c = chats.find(x => x.id === id);
  if (!c) return;
  currentId = id;
  log.replaceChildren();
  for (const m of c.msgs) {
    if (m.role === 'user') addUserTurn(String(m.text || ''));
    else renderSavedAnswer(m);
  }
  history = c.msgs.map(m => ({ role: m.role === 'user' ? 'user' : 'assistant', content: String(m.text || '') })).slice(-20);
  updateEmpty();
  renderHistory();
  closeSideOnMobile();
  toBottom(true);
}

function deleteChat(id) {
  if (busy) return;
  chats = chats.filter(c => c.id !== id);
  saveChats();
  if (id === currentId) newChat();
  else renderHistory();
}

function record(q, answer, isRegen) {
  let c = chats.find(x => x.id === currentId);
  if (!c) {
    c = { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7), title: q.slice(0, 60), updated: 0, msgs: [] };
    chats.unshift(c);
    currentId = c.id;
  }
  if (!isRegen) c.msgs.push({ role: 'user', text: q });
  c.msgs.push(answer);
  c.msgs = c.msgs.slice(-MAX_MSGS);
  c.updated = Date.now();
  saveChats();
  renderHistory();
}

// -------------------------------------------------------------------------
// Authentication Handlers
// -------------------------------------------------------------------------
function startResendCountdown(seconds) {
  let t = seconds || 60;
  const resendBtn = $('#resend-btn');
  if (!resendBtn) return;
  resendBtn.disabled = true;
  resendBtn.textContent = `${RESEND_LABEL} (${t}s)`;
  clearInterval(cd);
  cd = setInterval(() => {
    t--;
    if (t > 0) {
      resendBtn.textContent = `${RESEND_LABEL} (${t}s)`;
    } else {
      clearInterval(cd);
      resendBtn.disabled = false;
      resendBtn.textContent = RESEND_LABEL;
    }
  }, 1000);
}

function gateMessage(msg, ok) {
  $('#gerr').className = 'err-line ' + (ok ? 'slm' : 'err');
  $('#gerr').textContent = msg || '';
}

async function requestOtp(isResend = false) {
  const email = $('#em').value.trim();
  if (!email) {
    gateMessage('Please enter your IISc email address.');
    return;
  }
  gateMessage('');
  const genBtn = $('#gen');
  const resendBtn = $('#resend-btn');

  if (isResend) {
    if (resendBtn) {
      resendBtn.disabled = true;
      resendBtn.innerHTML = '<span class="spinner"></span> Sending...';
    }
  } else {
    genBtn.disabled = true;
    genBtn.innerHTML = '<span class="spinner"></span> Sending code...';
  }

  const endpoint = isResend ? '/auth/otp/resend' : '/auth/otp/request';
  try {
    const r = await post(endpoint, { email });
    if (!r.ok) {
      gateMessage(r.d.error || 'Failed to request OTP');
      genBtn.disabled = false;
      genBtn.innerHTML = GEN_LABEL;
      if (isResend && r.d.resend_in) {
        startResendCountdown(r.d.resend_in);
      } else if (resendBtn) {
        resendBtn.disabled = false;
        resendBtn.textContent = RESEND_LABEL;
      }
      return;
    }

    if (r.d.message) gateMessage(r.d.message, true);

    const targetEl = $('#target-email');
    if (targetEl) targetEl.textContent = email;
    const step1 = $('#step1');
    if (step1) step1.style.display = 'none';
    $('#step2').style.display = 'flex';
    $('#otp').value = '';
    $('#otp').focus();

    genBtn.disabled = false;
    genBtn.innerHTML = GEN_LABEL;

    startResendCountdown(r.d.resend_in || 60);
  } catch (err) {
    gateMessage('Network error. Please try again.');
    genBtn.disabled = false;
    genBtn.innerHTML = GEN_LABEL;
    if (resendBtn) {
      resendBtn.disabled = false;
      resendBtn.textContent = RESEND_LABEL;
    }
  }
}

$('#gen').onclick = () => requestOtp(false);
const resendBtnEl = $('#resend-btn');
if (resendBtnEl) resendBtnEl.onclick = () => requestOtp(true);

const changeEmEl = $('#change-em');
if (changeEmEl) {
  changeEmEl.onclick = e => {
    e.preventDefault();
    clearInterval(cd);
    $('#step2').style.display = 'none';
    const step1 = $('#step1');
    if (step1) step1.style.display = 'flex';
    $('#gen').disabled = false;
    $('#gen').innerHTML = GEN_LABEL;
    gateMessage('');
    $('#em').focus();
  };
}

$('#ver').onclick = async () => {
  const code = $('#otp').value.trim();
  if (!code) {
    gateMessage('Please enter the 6-digit code.');
    return;
  }
  gateMessage('');
  const verBtn = $('#ver');
  verBtn.disabled = true;
  verBtn.innerHTML = '<span class="spinner"></span> Verifying...';
  try {
    const r = await post('/auth/otp/verify', { email: $('#em').value, code });
    if (!r.ok) {
      gateMessage(r.d.error || 'Invalid or expired code');
      verBtn.disabled = false;
      verBtn.innerHTML = VER_LABEL;
      return;
    }
    location.reload();
  } catch (err) {
    gateMessage('Verification error. Please try again.');
    verBtn.disabled = false;
    verBtn.innerHTML = VER_LABEL;
  }
};

$('#otp').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#ver').click();
});
$('#em').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#gen').click();
});

fetch('/me').then(r => r.json()).then(m => {
  DEBUG = Boolean(m.debug_enabled);
  window.__csrf_token = m.csrf_token || '';
  if (m.backend !== 'gateway') {   // thesis filter and model override exist only on the standalone app
    $('#lbl-thesis').hidden = false;
    $('#lbl-force').hidden = false;
  }
  if (m.login_required && !m.email) {
    showGate(true);
    $('#who').textContent = 'Sign-in required';
    return;
  }
  showGate(false);
  me.email = m.email || null;
  $('#who').textContent = m.email || 'Open access';
  $('#who').title = m.email || '';
  $('#me-avatar').textContent = (m.email || '?').charAt(0);
  if (m.email) {
    const lo = $('#signout');
    lo.hidden = false;
    lo.onclick = async () => {
      forgetChats();   // shared lab machines: the next person must not see this user's chats
      const csrf = getCsrfToken();
      const headers = {};
      if (csrf) headers['X-CSRF-Token'] = csrf;
      await fetch('/auth/logout', { method: 'POST', headers: headers });
      location.reload();
    };
  }
  loadChats();
  renderHistory();
  $('#q').focus();
}).catch(() => { showGate(true); });

fetch('/theses').then(r => r.json()).then(a => a.forEach(t => {
  const o = new Option(t, t);
  $('#thesis').add(o);
})).catch(() => {});

// -------------------------------------------------------------------------
// Layout controls
// -------------------------------------------------------------------------
function toggleSide() {
  if (mobile()) document.body.classList.toggle('side-open');
  else document.body.classList.toggle('side-closed');
}
$('#menu').onclick = toggleSide;
$('#menu-close').onclick = toggleSide;
$('#scrim').onclick = closeSideOnMobile;
$('#new').onclick = newChat;
$('#new-top').onclick = newChat;
document.querySelectorAll('.chip').forEach(c => {
  c.onclick = () => ask(c.dataset.q);
});

function grow() {
  const q = $('#q');
  q.style.height = 'auto';
  q.style.height = Math.min(q.scrollHeight, 200) + 'px';
}
function syncSend() {
  $('#go').disabled = !busy && !$('#q').value.trim();
}
function setBusy(on) {
  busy = on;
  const go = $('#go');
  go.replaceChildren(icon(on ? 'stop' : 'send'));
  go.title = on ? 'Stop' : 'Send';
  go.setAttribute('aria-label', on ? 'Stop answering' : 'Send');
  syncSend();
}
setBusy(false);

$('#q').addEventListener('input', () => { grow(); syncSend(); });
$('#q').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!busy) $('#f').requestSubmit();   // Enter never stops a running answer; only the button does
  }
});

// -------------------------------------------------------------------------
// Chat Submission & Streaming Engine
// -------------------------------------------------------------------------
$('#f').onsubmit = async e => {
  e.preventDefault();
  if (busy) {
    if (controller) controller.abort();
    return;
  }
  const q = $('#q').value.trim();
  if (!q) return;
  $('#q').value = '';
  grow();
  await ask(q);
};

// Tell the user where a cached answer came from (text only, never HTML).
function showCacheNote(head, d) {
  const n = document.createElement('div');
  n.className = 'note cache-note';
  if (d.kind === 'semantic') {
    n.textContent = 'Reused answer from your earlier question: “' + (d.matched_prompt || '') + '”';
  } else if (d.kind === 'override') {
    n.textContent = 'Your previously regenerated answer to this question.';
  } else {
    n.textContent = 'Reused answer to an identical earlier question.';
  }
  head.after(n);
  return n.textContent;
}

// Offered ONLY on answers served from the cache: the gateway attaches a single-use token to cached answers.
function addRegenerateButton(foot, q, token) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'regen-btn';
  b.textContent = '↻ Generate a fresh answer';
  b.title = 'This answer came from the cache. Ask the model again if it does not fit your question.';
  b.onclick = async () => {
    if (busy) return;
    b.disabled = true;
    b.textContent = 'Generating a fresh answer…';
    await ask(q, { regenerateToken: token });
    b.remove();
  };
  foot.nextElementSibling.append(b);   // the answer's action row
}

// Ask a question. opts.regenerateToken: request a fresh answer for a cached one.
async function ask(q, opts = {}) {
  if (busy || !q) return;
  setBusy(true);

  if (!opts.regenerateToken) addUserTurn(q);
  const { head, body, foot, actions } = addBotTurn();
  const dots = typingDots();
  body.append(dots);
  let text = '';
  let dtEl = null;
  let shown = [];          // the sources currently listed under the answer
  let cacheInfo = null;
  let route = null;
  let failed = false;
  let stopped = false;
  const notes = [];

  // Mirrors pipeline/sanitizer.py is_no_relevant_docs_answer: an answer IS a refusal only if it is the refusal
  // sentence on its own or a short text; a long answer that mentions a gap in passing keeps its sources.
  const REFUSAL_RE = /^\W*no\s+specific\s+institutional\s+repository\s+thes[ie]s\s+(?:were|was)\s+found\b[^.!?\n]{0,40}[.!?]?\W*/i;
  const startsWithRefusal = s => !!s && REFUSAL_RE.test(s);
  const isNoDocsAnswer = s => {
    if (!s) return false;
    const low = s.toLowerCase().trim();
    if (low.includes('based on general academic knowledge') || low.includes('based on general technical knowledge') ||
        low.includes('retrieval failed')) return true;
    const m = low.match(REFUSAL_RE);
    if (m) return low.slice(m[0].length).split(/\s+/).filter(Boolean).length < 12;
    if (low.split(/\s+/).filter(Boolean).length > 60) return false;
    return low.includes('no specific institutional repository') ||
           low.includes('no institutional repository') ||
           low.includes('no relevant document') ||
           low.includes('no relevant documents') ||
           low.includes('no documents were found') ||
           low.includes('no document was found') ||
           low.includes('no documents found') ||
           low.includes('no documents are present') ||
           low.includes('not present in the thesis') ||
           low.includes('not found in the thesis') ||
           low.includes('not found in any of the provided documents') ||
           low.includes('do not contain information to answer') ||
           low.includes('does not contain information to answer') ||
           low.includes('do not contain any information') ||
           low.includes('does not contain any information') ||
           low.includes('do not provide any information') ||
           low.includes('does not provide any information') ||
           low.includes('cannot answer based on the provided') ||
           low.includes('unable to answer based on the provided') ||
           low.includes('insufficient information') ||
           low.includes('insufficient data') ||
           low.includes('insufficient context') ||
           low.includes('not enough information') ||
           low.includes('do not have enough information') ||
           low.includes('does not have enough information') ||
           low.includes('no information is available') ||
           low.includes('no information was found') ||
           low.includes('no information about') ||
           low.includes('no information provided') ||
           low.includes('there is no information') ||
           low.includes('unable to find information') ||
           low.includes('cannot find information') ||
           low.includes('retrieval failed');
  };

  const hideSources = () => {
    if (dtEl) dtEl.remove();
    dtEl = null;
    shown = [];
  };
  const showSources = cs => {
    hideSources();
    if (!cs || !cs.length || isNoDocsAnswer(text)) return;

    // Deduplicate repeated thesis titles using a Set so each source entry is shown only once
    const seen = new Map();
    const unique = [];
    cs.forEach(c => {
      const key = (c.title || c.thesis_id || '').trim();
      if (key) {
        if (!seen.has(key)) {
          const first = { ...c, documents: docNumbers(c) };
          seen.set(key, first);
          unique.push(first);
        } else {   // same thesis again: one source, all its [Document n] numbers
          const docs = seen.get(key).documents;
          docNumbers(c).forEach(n => { if (!docs.includes(n)) docs.push(n); });
        }
      } else {
        unique.push(c);
      }
    });
    if (!unique.length) return;
    dtEl = sourcesEl(unique);
    foot.before(dtEl);
    shown = unique;
  };
  const fail = msg => {
    failed = true;
    body.textContent = msg;
    body.classList.add('err');
  };

  try {
    controller = new AbortController();
    const headers = { 'Content-Type': 'application/json' };
    const csrf = getCsrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
    const r = await fetch('/chat', {
      method: 'POST',
      headers: headers,
      signal: controller.signal,
      body: JSON.stringify({
        message: q,
        history,
        thesis_id: $('#thesis').value || null,
        force: $('#force').value || null,
        regenerate_token: opts.regenerateToken || null
      })
    });

    if (!r.ok) {
      let errData = {};
      try { errData = await r.json(); } catch (_) {}
      fail(errData.detail || errData.error || errData.msg || `Server error (HTTP ${r.status})`);
      if (r.status === 401) {
        showGate(true);
      }
    } else {
      const rd = r.body.getReader();
      const dec = new TextDecoder('utf-8');
      let buf = '';

      while (true) {
        const { value, done } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });

        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const blk = buf.slice(0, i);
          buf = buf.slice(i + 2);

          const evObj = parseSSEBlock(blk);
          if (!evObj) continue;
          const { event: ev, data: d } = evObj;

          if (ev === 'meta') {
            route = d.route || null;
            head.replaceChildren();
            if (d.route) head.append(routeBadge(d.route));
            if (opts.regenerateToken) head.append(badge('Fresh answer', 'regen'));
            if (DEBUG && d.s != null) head.append(badge(`P(hard)=${d.s} / α=${d.alpha}`, 'dbg'));
            if (d.citations) showSources(d.citations);
          }
          else if (ev === 'cache') {
            cacheInfo = d;
            notes.push(showCacheNote(head, d));
          }
          else if (ev === 'correction') {   // the question was spell-corrected before searching
            const n = div('note fix-note', 'Showing results for: “' + (d.q || '') + '”');
            head.after(n);
            notes.push(n.textContent);
          }
          else if (ev === 'sources' || ev === 'chunks') {
            if (d.citations) showSources(d.citations);
            else if (d.chunks) showSources(d.chunks);
            else showSources([]);
          }
          else if (ev === 'token') {
            const delta = d.t !== undefined ? d.t : (d.delta !== undefined ? d.delta : (d.content || d.token || ''));
            if (delta) {
              text += delta;
              body.replaceChildren(renderMathAndMarkdown(text));
              if (dtEl && startsWithRefusal(text)) hideSources();
            }
          }
          else if (ev === 'escalate') {
            foot.textContent = `SLM confidence ${d.conf} < ${d.threshold}; escalating to 7B…`;
          }
          else if (ev === 'fallback') {
            foot.textContent = `${d.from} unavailable → falling back to ${d.to}`;
          }
          else if (ev === 'refused') {
            body.replaceChildren(renderMathAndMarkdown(d.msg || 'Query refused.'), ' ');
            const a = document.createElement('a');
            a.href = d.login || '/';
            a.textContent = 'Sign in';
            body.append(a);
          }
          else if (ev === 'warning') {
            foot.textContent = d.msg || '';
          }
          else if (ev === 'error') {
            fail(d.msg || 'The answering service reported an error.');
          }
          else if (ev === 'done') {
            if (dtEl && isNoDocsAnswer(text)) hideSources();
            if (d.refused) {
              foot.textContent = 'No models were called';
              continue;
            }
            if (DEBUG && d.timings) {
              foot.textContent = `Answered by ${d.model || 'model'}${d.escalated ? ' (escalated)' : ''} · total ${d.timings.total_ms || 0}ms`;
            }
          }
          toBottom();
        }
      }

      // Flush any remaining tokens in buffer
      if (buf.trim()) {
        const finalEv = parseSSEBlock(buf.trim());
        if (finalEv && finalEv.event === 'token') {
          const delta = finalEv.data.t || finalEv.data.delta || '';
          text += delta;
        }
      }
    }
  } catch (err) {
    if (err && err.name === 'AbortError') stopped = true;
    else fail('Network connection error: ' + err);
  }
  controller = null;
  dots.remove();

  // Final full markdown + LaTeX rendering pass
  if (text && !failed) {
    body.replaceChildren(renderMathAndMarkdown(text));
    if (stopped) foot.textContent = 'Stopped.';
    history.push({ role: 'user', content: q }, { role: 'assistant', content: text });
    if (history.length > 20) {
      history = history.slice(-20);
    }
    addCopyButton(actions, text);
    record(q, {
      role: 'assistant',
      text,
      route,
      notes,
      sources: shown.map(c => ({
        title: c.title || '', thesis_id: c.thesis_id || '', chapter: c.chapter || '', version: c.version || '',
        text: String(c.text || '').slice(0, 300), documents: docNumbers(c)
      }))
    }, Boolean(opts.regenerateToken));
  } else if (stopped) {
    foot.textContent = 'Stopped.';
  }

  if (cacheInfo && cacheInfo.regenerate_token && text) addRegenerateButton(foot, q, cacheInfo.regenerate_token);
  setBusy(false);
  toBottom();
  $('#q').focus();
}
