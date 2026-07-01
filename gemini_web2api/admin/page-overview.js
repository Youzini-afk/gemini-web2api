/* ──────────────────────────────────────────────────────────────────
 * page-overview.js — Overview page: stat cards + history table.
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  var STAT_DEFS = [
    { key: 'version',     label: '版本',       accent: 'blue',   hint: '服务版本' },
    { key: 'uptime',      label: '运行时长',   accent: 'teal',   hint: '自启动以来', format: 'uptime' },
    { key: 'total_requests', label: '总请求数', accent: 'blue',   hint: '累计调用', format: 'num' },
    { key: 'success_count',  label: '成功',     accent: 'green',  hint: '成功请求', format: 'num' },
    { key: 'error_count',    label: '失败',     accent: 'red',    hint: '错误请求', format: 'num' },
    { key: 'key_count',      label: 'API 密钥', accent: 'yellow', hint: '已配置密钥', format: 'num' },
    { key: 'model_count',    label: '可用模型', accent: 'blue',   hint: '内置模型', format: 'num' }
  ];

  function renderStatCards(stats) {
    var grid = $('#stat-grid');
    if (!grid) return;
    var html = STAT_DEFS.map(function (def) {
      var raw = stats ? stats[def.key] : '—';
      var val = raw;
      if (raw !== '—') {
        if (def.format === 'uptime') val = formatUptime(raw);
        else if (def.format === 'num') val = formatNumber(raw);
        else val = esc(raw);
      }
      return (
        '<div class="stat-card accent-' + def.accent + '">' +
          '<div class="stat-label">' + esc(def.label) + '</div>' +
          '<div class="stat-value">' + (val === undefined || val === null ? '—' : val) + '</div>' +
          '<div class="stat-hint">' + esc(def.hint) + '</div>' +
        '</div>'
      );
    }).join('');
    grid.innerHTML = html;
  }

  function renderHistory(rows) {
    var tbody = $('#history-table tbody');
    if (!tbody) return;
    if (!rows || !rows.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="4">暂无请求记录</td></tr>';
      return;
    }
    // Newest first
    var list = rows.slice().reverse();
    var html = list.map(function (r) {
      var ok = r.success;
      return (
        '<tr>' +
          '<td class="mono">' + esc(r.time) + '</td>' +
          '<td class="mono">' + esc(r.path) + '</td>' +
          '<td><span class="pill ' + (ok ? 'pill-ok' : 'pill-err') + '">' + (ok ? '成功' : '失败') + '</span></td>' +
          '<td class="mono">' + formatNumber(r.latency) + ' ms</td>' +
        '</tr>'
      );
    }).join('');
    tbody.innerHTML = html;
  }

  function load() {
    var statsP = api.getStats().then(renderStatCards).catch(function (e) {
      renderStatCards(null);
      toast.error('加载统计失败: ' + e.message);
    });
    var histP = api.getHistory().then(renderHistory).catch(function (e) {
      renderHistory([]);
      if (e.status !== 401) toast.error('加载历史失败: ' + e.message);
    });
    return Promise.all([statsP, histP]);
  }

  function onReset() {
    if (!confirm('确定要重置统计数据吗?此操作不可撤销。')) return;
    api.resetStats().then(function () {
      toast.success('统计数据已重置');
      load();
    }).catch(function (e) {
      if (e.status !== 401) toast.error('重置失败: ' + e.message);
    });
  }

  function init() {
    var btn = $('#reset-stats-btn');
    if (btn) btn.addEventListener('click', onReset);
  }

  global.pageOverview = { init: init, load: load };
})(window);
