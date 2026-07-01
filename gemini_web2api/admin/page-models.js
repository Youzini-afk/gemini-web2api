/* ──────────────────────────────────────────────────────────────────
 * page-models.js — Read-only display of available models.
 * Backend returns {models: [names], alias_map: {}}. The known built-in
 * models are enriched with a small client-side description map.
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  // Friendly descriptions / tags for known built-in Gemini models.
  // Kept in sync with gemini_web2api/models.py. Unknown models simply
  // show with a generic tag.
  var MODEL_INFO = {
    'gemini-3.5-flash':              { tag: 'FAST',   desc: '快速通用模型,适合大多数日常任务' },
    'gemini-3.5-flash-thinking':     { tag: 'THINK',  desc: '深度思考模式,最长输出约 20k 字符' },
    'gemini-3.5-flash-thinking-lite':{ tag: 'DYN',    desc: '动态思考,自适应推理深度' },
    'gemini-3.1-pro':                { tag: 'PRO',    desc: 'Pro 模型,需要 cookie 才能真正路由' },
    'gemini-3.1-pro-enhanced':       { tag: 'PRO+',   desc: 'Pro 增强输出 (实验性)' },
    'gemini-auto':                   { tag: 'AUTO',   desc: '自动选择合适的模型' },
    'gemini-flash-lite':             { tag: 'LITE',   desc: '轻量快速模型' }
  };

  function inferTag(name) {
    if (!name) return 'MODEL';
    if (name.indexOf('pro') !== -1) return 'PRO';
    if (name.indexOf('thinking') !== -1) return 'THINK';
    if (name.indexOf('lite') !== -1) return 'LITE';
    if (name.indexOf('auto') !== -1) return 'AUTO';
    if (name.indexOf('flash') !== -1) return 'FAST';
    return 'MODEL';
  }

  function render(data) {
    var grid = $('#model-grid');
    var countEl = $('#model-count');
    var models = (data && data.models) || [];
    var aliasMap = (data && data.alias_map) || {};

    if (countEl) countEl.textContent = models.length + ' 个模型';

    if (!grid) return;
    if (!models.length) {
      grid.innerHTML = '<p class="muted">暂无可用模型</p>';
      return;
    }

    var html = models.map(function (name) {
      var info = MODEL_INFO[name] || { tag: inferTag(name), desc: '通用模型' };
      var aliases = [];
      Object.keys(aliasMap).forEach(function (alias) {
        if (aliasMap[alias] === name) aliases.push(alias);
      });
      var aliasHtml = aliases.length
        ? '<div class="model-tag">别名: ' + aliases.map(esc).join(', ') + '</div>'
        : '<div class="model-tag">' + esc(info.tag) + '</div>';
      return (
        '<div class="model-card">' +
          '<div class="model-name">' + esc(name) + '</div>' +
          '<div class="model-desc">' + esc(info.desc) + '</div>' +
          aliasHtml +
        '</div>'
      );
    }).join('');
    grid.innerHTML = html;
  }

  function load() {
    return api.getModels().then(render).catch(function (e) {
      if (e.status !== 401) toast.error('加载模型失败: ' + e.message);
      render({ models: [], alias_map: {} });
    });
  }

  function init() {
    // Read-only page; no interactive controls beyond display.
  }

  global.pageModels = { init: init, load: load };
})(window);
