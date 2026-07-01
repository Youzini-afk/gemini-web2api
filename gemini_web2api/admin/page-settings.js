/* ──────────────────────────────────────────────────────────────────
 * page-settings.js — Settings form: load, track changes, save (PUT).
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  // All known settings fields, kept in sync with backend SETTINGS_FIELDS.
  var FIELDS = [
    'retry_attempts', 'retry_delay_sec', 'request_timeout_sec',
    'default_model', 'log_requests', 'gemini_bl',
    'auth_user', 'xsrf_token', 'proxy', 'cookie_file'
  ];

  var original = {};     // last loaded values (string form)
  var dirty = false;

  function fieldInputs() {
    return $$('[data-field]').filter(function (el) {
      return FIELDS.indexOf(el.getAttribute('data-field')) !== -1;
    });
  }

  function getInputValue(el) {
    if (el.type === 'checkbox') return el.checked;
    if (el.type === 'number') {
      var raw = el.value.trim();
      if (raw === '') return null;
      var n = Number(raw);
      return Number.isFinite(n) ? n : raw;
    }
    var v = el.value;
    return v === '' ? null : v;
  }

  function setInputValue(el, value) {
    if (el.type === 'checkbox') {
      el.checked = !!value;
    } else if (el.type === 'number') {
      el.value = (value === null || value === undefined) ? '' : value;
    } else {
      el.value = (value === null || value === undefined) ? '' : String(value);
    }
  }

  function checkDirty() {
    var changed = false;
    var inputs = fieldInputs();
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      var field = el.getAttribute('data-field');
      var cur = getInputValue(el);
      var curStr = (cur === null || cur === undefined) ? '' : (typeof cur === 'boolean' ? (cur ? 'true' : 'false') : String(cur));
      if (curStr !== original[field]) { changed = true; break; }
    }
    dirty = changed;
    var hint = $('#settings-dirty');
    if (hint) hint.classList.toggle('hidden', !dirty);
    var btn = $('#save-settings-btn');
    if (btn) btn.disabled = !dirty;
  }

  function load() {
    return api.getSettings().then(function (data) {
      original = {};
      fieldInputs().forEach(function (el) {
        var field = el.getAttribute('data-field');
        var v = data ? data[field] : null;
        setInputValue(el, v);
        var str = (v === null || v === undefined) ? '' : (typeof v === 'boolean' ? (v ? 'true' : 'false') : String(v));
        original[field] = str;
      });
      checkDirty();
    }).catch(function (e) {
      if (e.status !== 401) toast.error('加载设置失败: ' + e.message);
    });
  }

  function save() {
    var changes = {};
    var inputs = fieldInputs();
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      var field = el.getAttribute('data-field');
      var cur = getInputValue(el);
      var curStr = (cur === null || cur === undefined) ? '' : (typeof cur === 'boolean' ? (cur ? 'true' : 'false') : String(cur));
      if (curStr !== original[field]) {
        changes[field] = cur;
      }
    }
    if (Object.keys(changes).length === 0) {
      toast.info('没有需要保存的更改');
      return Promise.resolve();
    }
    var btn = $('#save-settings-btn');
    if (btn) { btn.disabled = true; btn.textContent = '保存中...'; }
    return api.putSettings(changes).then(function (res) {
      // Re-snapshot from current values
      original = {};
      fieldInputs().forEach(function (el) {
        var field = el.getAttribute('data-field');
        var v = getInputValue(el);
        original[field] = (v === null || v === undefined) ? '' : (typeof v === 'boolean' ? (v ? 'true' : 'false') : String(v));
      });
      checkDirty();
      var n = (res && res.changed && res.changed.length) || Object.keys(changes).length;
      toast.success('已保存 ' + n + ' 项更改');
    }).catch(function (e) {
      if (e.status !== 401) toast.error('保存失败: ' + e.message);
    }).then(function () {
      if (btn) { btn.disabled = false; btn.textContent = '保存设置'; }
    });
  }

  function init() {
    fieldInputs().forEach(function (el) {
      var ev = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'input';
      el.addEventListener(ev, debounce(checkDirty, 120));
    });
    var btn = $('#save-settings-btn');
    if (btn) btn.addEventListener('click', save);
  }

  global.pageSettings = { init: init, load: load };
})(window);
