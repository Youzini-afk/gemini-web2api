/* ──────────────────────────────────────────────────────────────────
 * utils.js — shared helpers: DOM, escape, toast, formatting
 * All functions exposed on window (global scope) for inline handlers.
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  /** Query selector shorthand. */
  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  /** Query selector all shorthand. */
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  /** HTML-escape a string for safe innerHTML insertion. */
  function esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** Format seconds as a human uptime string. */
  function formatUptime(sec) {
    sec = Number(sec) || 0;
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const parts = [];
    if (d) parts.push(d + '天');
    if (h || d) parts.push(h + '时');
    if (m || h || d) parts.push(m + '分');
    parts.push(s + '秒');
    return parts.join(' ');
  }

  /** Format a latency value (ms) with thousand separators. */
  function formatNumber(n) {
    n = Number(n) || 0;
    return n.toLocaleString('en-US');
  }

  /** Generate a random API key string (sk- + 32 hex chars). */
  function generateApiKey() {
    var bytes = new Uint8Array(24);
    (global.crypto || {}).getRandomValues ? global.crypto.getRandomValues(bytes) : bytes.forEach(function (_, i) { bytes[i] = Math.floor(Math.random() * 256); });
    var hex = '';
    for (var i = 0; i < bytes.length; i++) {
      hex += ('0' + bytes[i].toString(16)).slice(-2);
    }
    return 'sk-' + hex;
  }

  /** Copy text to clipboard with a fallback. Returns a Promise. */
  function copyText(text) {
    if (global.navigator && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error('copy failed'));
      } catch (e) { reject(e); }
    });
  }

  // ─── Toast notifications ──────────────────────────────────────────

  var ICONS = { ok: '✓', err: '✕', warn: '!', info: 'i' };

  function toast(message, type, timeout) {
    type = type || 'info';
    timeout = timeout === undefined ? 3000 : timeout;
    var container = $('#toast-container');
    if (!container) return;
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.innerHTML =
      '<span class="toast-icon">' + (ICONS[type] || ICONS.info) + '</span>' +
      '<span>' + esc(message) + '</span>';
    container.appendChild(el);
    if (timeout > 0) {
      setTimeout(function () { dismiss(el); }, timeout);
    }
    return el;
  }

  function dismiss(el) {
    if (!el || el._dismissed) return;
    el._dismissed = true;
    el.classList.add('out');
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 260);
  }

  toast.success = function (m, t) { return toast(m, 'ok', t); };
  toast.error = function (m, t) { return toast(m, 'err', t || 4000); };
  toast.warn = function (m, t) { return toast(m, 'warn', t); };
  toast.info = function (m, t) { return toast(m, 'info', t); };

  // ─── Debounce ────────────────────────────────────────────────────

  function debounce(fn, wait) {
    var t;
    return function () {
      var ctx = this, args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, wait);
    };
  }

  // ─── Export to global ────────────────────────────────────────────
  global.$ = $;
  global.$$ = $$;
  global.esc = esc;
  global.formatUptime = formatUptime;
  global.formatNumber = formatNumber;
  global.generateApiKey = generateApiKey;
  global.copyText = copyText;
  global.toast = toast;
  global.debounce = debounce;
})(window);
