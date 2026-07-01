/* ──────────────────────────────────────────────────────────────────
 * page-keys.js — API key management: list, add (with copy-on-create),
 * delete.
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  function renderKeys(data) {
    var tbody = $('#keys-table tbody');
    if (!tbody) return;
    var keys = (data && data.keys) || [];
    if (!keys.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="4">还没有 API 密钥</td></tr>';
      return;
    }
    var html = keys.map(function (k) {
      return (
        '<tr>' +
          '<td>' + esc(k.name) + '</td>' +
          '<td class="mono">' + esc(k.key) + '</td>' +
          '<td>' + esc(k.description || '') + '</td>' +
          '<td><button class="btn btn-danger btn-sm" data-del="' + esc(k.name) + '">删除</button></td>' +
        '</tr>'
      );
    }).join('');
    tbody.innerHTML = html;

    // Wire delete buttons
    $$('[data-del]', tbody).forEach(function (btn) {
      btn.addEventListener('click', function () {
        var name = btn.getAttribute('data-del');
        if (!confirm('确定删除密钥 "' + name + '" 吗?')) return;
        api.deleteKey(name).then(function () {
          toast.success('已删除密钥 ' + name);
          load();
        }).catch(function (e) {
          if (e.status !== 401) toast.error('删除失败: ' + e.message);
        });
      });
    });
  }

  function load() {
    return api.getKeys().then(renderKeys).catch(function (e) {
      if (e.status !== 401) toast.error('加载密钥失败: ' + e.message);
      renderKeys({ keys: [] });
    });
  }

  function showNewKeyResult(key) {
    var box = $('#new-key-result');
    if (!box) return;
    box.classList.remove('hidden');
    box.innerHTML =
      '<span class="toast-icon" style="color:var(--green)">✓</span>' +
      '<span class="key-val" id="new-key-val">' + esc(key) + '</span>' +
      '<button class="copy-btn" id="copy-new-key">复制</button>';
    var copyBtn = $('#copy-new-key', box);
    copyBtn.addEventListener('click', function () {
      copyText(key).then(function () {
        toast.success('已复制到剪贴板');
        copyBtn.textContent = '已复制';
        setTimeout(function () { copyBtn.textContent = '复制'; }, 1500);
      }).catch(function () { toast.error('复制失败'); });
    });
  }

  function onAdd(e) {
    e.preventDefault();
    var nameEl = $('#new-key-name');
    var keyEl = $('#new-key-value');
    var descEl = $('#new-key-desc');
    var name = nameEl.value.trim();
    var key = keyEl.value.trim();
    var desc = descEl.value.trim();

    if (!name) {
      toast.error('请填写密钥名称');
      nameEl.focus();
      return;
    }
    if (key && key.indexOf('sk-') !== 0) {
      toast.error('密钥必须以 "sk-" 开头');
      keyEl.focus();
      return;
    }

    var btn = e.target.querySelector('button[type="submit"]');
    if (btn) { btn.disabled = true; btn.textContent = '创建中...'; }
    api.addKey(name, key, desc).then(function (res) {
      var newKey = (res && res.key) || '';
      toast.success('密钥 "' + name + '" 已创建');
      nameEl.value = '';
      keyEl.value = '';
      descEl.value = '';
      if (newKey) showNewKeyResult(newKey);
      load();
    }).catch(function (e) {
      if (e.status !== 401) toast.error('创建失败: ' + e.message);
    }).then(function () {
      if (btn) { btn.disabled = false; btn.textContent = '创建密钥'; }
    });
  }

  function init() {
    var form = $('#add-key-form');
    if (form) form.addEventListener('submit', onAdd);
    var keyInput = $('#new-key-value');
    if (keyInput) {
      keyInput.addEventListener('dblclick', function () {
        if (!keyInput.value.trim()) keyInput.value = generateApiKey();
      });
    }
  }

  global.pageKeys = { init: init, load: load };
})(window);
