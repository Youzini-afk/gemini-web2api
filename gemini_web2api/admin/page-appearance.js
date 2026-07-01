/* ──────────────────────────────────────────────────────────────────
 * page-appearance.js — Background customization, localStorage only.
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  var STORAGE_KEY = 'gemini_admin_bg';
  var ACTIVE_KEY = 'gemini_admin_bg_active'; // id of active preset, or 'custom' / 'none'

  // Preset backgrounds: gradient meshes designed to feel "Google-ish".
  var PRESETS = [
    { id: 'aurora',   name: '极光',   css: 'radial-gradient(1200px 800px at 15% 10%, rgba(66,133,244,0.28), transparent 55%), radial-gradient(900px 700px at 90% 20%, rgba(52,168,83,0.16), transparent 55%), radial-gradient(800px 800px at 70% 100%, rgba(251,188,5,0.12), transparent 60%), #0f1020' },
    { id: 'midnight', name: '午夜',   css: 'radial-gradient(1000px 700px at 20% 0%, rgba(23,42,138,0.45), transparent 60%), radial-gradient(900px 700px at 100% 100%, rgba(91,33,182,0.30), transparent 55%), #0a0a1a' },
    { id: 'ocean',    name: '深海',   css: 'radial-gradient(1100px 800px at 10% 100%, rgba(13,71,161,0.45), transparent 55%), radial-gradient(900px 700px at 90% 0%, rgba(0,188,212,0.20), transparent 55%), #0a1428' },
    { id: 'sunset',   name: '黄昏',   css: 'radial-gradient(1000px 700px at 80% 10%, rgba(251,188,5,0.22), transparent 55%), radial-gradient(900px 700px at 20% 90%, rgba(234,67,53,0.22), transparent 55%), #1a0f1e' },
    { id: 'mono',     name: '纯黑',   css: '#050510' },
    { id: 'slate',    name: '石板',   css: 'radial-gradient(900px 700px at 50% 0%, rgba(60,72,107,0.35), transparent 60%), #11131c' }
  ];

  function getStored() {
    try {
      return {
        bg: localStorage.getItem(STORAGE_KEY) || null,
        active: localStorage.getItem(ACTIVE_KEY) || null
      };
    } catch (e) { return { bg: null, active: null }; }
  }

  function storeActive(active) {
    try { localStorage.setItem(ACTIVE_KEY, active); } catch (e) {}
  }

  function storeBg(bg) {
    try { localStorage.setItem(STORAGE_KEY, bg); } catch (e) {}
  }

  /** Apply a background CSS value to the body and mark applied. */
  function applyBackground(bg) {
    if (bg) {
      document.body.style.background = bg;
      document.body.setAttribute('data-bg-applied', '1');
    } else {
      document.body.style.background = '';
      document.body.setAttribute('data-bg-applied', '0');
    }
  }

  function highlightActive(activeId) {
    $$('.preset-tile').forEach(function (tile) {
      tile.classList.toggle('active', tile.getAttribute('data-id') === activeId);
    });
  }

  function renderPresets() {
    var grid = $('#preset-grid');
    if (!grid) return;
    var stored = getStored();
    var html = PRESETS.map(function (p) {
      var isActive = stored.active === p.id;
      return (
        '<div class="preset-tile' + (isActive ? ' active' : '') + '" ' +
          'data-id="' + p.id + '" style="background:' + p.css + '" title="' + esc(p.name) + '">' +
          '<span>' + esc(p.name) + '</span>' +
        '</div>'
      );
    }).join('');
    grid.innerHTML = html;

    $$('.preset-tile', grid).forEach(function (tile) {
      tile.addEventListener('click', function () {
        var id = tile.getAttribute('data-id');
        var preset = PRESETS.filter(function (p) { return p.id === id; })[0];
        if (!preset) return;
        applyBackground(preset.css);
        storeBg(preset.css);
        storeActive(id);
        highlightActive(id);
        toast.success('已应用背景: ' + preset.name);
      });
    });
  }

  function restore() {
    var stored = getStored();
    if (stored.bg && stored.active && stored.active !== 'none') {
      applyBackground(stored.bg);
    }
  }

  function onApplyUrl() {
    var input = $('#bg-url');
    if (!input) return;
    var url = input.value.trim();
    if (!url) {
      toast.error('请输入图片 URL');
      return;
    }
    var bg = 'url("' + url.replace(/"/g, '') + '") center/cover no-repeat fixed, #0f1020';
    applyBackground(bg);
    storeBg(bg);
    storeActive('custom');
    highlightActive('custom');
    toast.success('已应用自定义背景');
  }

  function onFile(e) {
    var file = e.target.files && e.target.files[0];
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) {
      toast.error('图片过大 (>4MB),请使用更小的图片');
      e.target.value = '';
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var dataUrl = reader.result;
        var bg = 'url("' + dataUrl + '") center/cover no-repeat fixed, #0f1020';
        applyBackground(bg);
        storeBg(bg);
        storeActive('custom');
        highlightActive('custom');
        toast.success('已上传并应用背景');
      } catch (err) {
        toast.error('读取图片失败');
      }
    };
    reader.onerror = function () { toast.error('读取图片失败'); };
    reader.readAsDataURL(file);
  }

  function onReset() {
    if (!confirm('确定重置为默认背景吗?')) return;
    applyBackground(null);
    try {
      localStorage.removeItem(STORAGE_KEY);
      localStorage.removeItem(ACTIVE_KEY);
    } catch (e) {}
    var input = $('#bg-url');
    if (input) input.value = '';
    var file = $('#bg-file');
    if (file) file.value = '';
    highlightActive(null);
    toast.success('已重置为默认背景');
  }

  function init() {
    renderPresets();
    restore();
    var applyBtn = $('#apply-bg-btn');
    if (applyBtn) applyBtn.addEventListener('click', onApplyUrl);
    var fileInput = $('#bg-file');
    if (fileInput) fileInput.addEventListener('change', onFile);
    var resetBtn = $('#reset-bg-btn');
    if (resetBtn) resetBtn.addEventListener('click', onReset);
  }

  function load() {
    // Background is local-only; nothing to fetch from server.
    // Re-render presets in case DOM was reset.
    renderPresets();
    restore();
    return Promise.resolve();
  }

  // Restore background ASAP (before page becomes visible) to avoid flash.
  // Called once at script load.
  try { restore(); } catch (e) {}

  global.pageAppearance = { init: init, load: load };
})(window);
