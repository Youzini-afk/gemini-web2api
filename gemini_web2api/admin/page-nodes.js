/* ──────────────────────────────────────────────────────────────────
 * page-nodes.js — Node pool management: import, list, test, enable/disable,
 * delete, subscriptions, mihomo control.
 * ────────────────────────────────────────────────────────────────── */

var _allNodes = [];
var _nodeStats = {};
var _subscriptions = [];
var _nodeSearch = '';
var _nodePage = 1;
var _nodePageSize = 50;
var _selectedNodes = new Set();
var _nodeSearchTimer = null;

function loadNodes() {
  return api.getNodes().then(function (data) {
    _allNodes = data.nodes || [];
    _nodeStats = data.stats || {};
    renderNodeStats();
    renderNodesTable();
  }).catch(function (e) {
    toast('节点列表加载失败: ' + e.message, 'err');
  });
}

function loadSubscriptions() {
  return api.getSubscriptions().then(function (data) {
    _subscriptions = data.subscriptions || [];
    renderSubscriptions();
  }).catch(function (e) {
    toast('订阅源加载失败: ' + e.message, 'err');
  });
}

function loadMihomoStatus() {
  return api.getMihomoStatus().then(function (data) {
    var text = document.getElementById('mihomo-status-text');
    var info = document.getElementById('mihomo-info');
    var startBtn = document.getElementById('mihomo-start-btn');
    var stopBtn = document.getElementById('mihomo-stop-btn');
    if (!data.available) {
      text.textContent = '未安装';
      text.style.color = '#ea4335';
      info.textContent = 'mihomo 二进制未找到，代理节点功能不可用。配置文件中的 proxy 仍可使用。';
      startBtn.disabled = true;
      stopBtn.disabled = true;
    } else if (data.running) {
      text.textContent = '运行中';
      text.style.color = '#34a853';
      info.textContent = data.local_proxy || '';
      startBtn.disabled = true;
      stopBtn.disabled = false;
    } else {
      text.textContent = '已停止';
      text.style.color = '#fbbc05';
      info.textContent = '';
      startBtn.disabled = false;
      stopBtn.disabled = true;
    }
  }).catch(function (e) {
    var text = document.getElementById('mihomo-status-text');
    if (text) text.textContent = '状态获取失败';
  });
}

function renderNodeStats() {
  var s = _nodeStats;
  var text = document.getElementById('node-stats-text');
  if (text) {
    text.textContent = '总数 ' + (s.total || 0) + ' / 启用 ' + (s.enabled || 0) +
      ' / 禁用 ' + (s.disabled || 0) + ' / 冷却 ' + (s.cooling || 0) +
      ' / 可用 ' + (s.available || 0);
  }
  var chips = document.getElementById('node-chips');
  if (chips) {
    var items = [
      { label: '总数', val: s.total || 0, color: '#4285f4' },
      { label: '启用', val: s.enabled || 0, color: '#34a853' },
      { label: '禁用', val: s.disabled || 0, color: '#ea4335' },
      { label: '冷却中', val: s.cooling || 0, color: '#fbbc05' },
      { label: '可用', val: s.available || 0, color: '#34a853' },
    ];
    chips.innerHTML = items.map(function (c) {
      return '<span class="chip" style="border-color:' + c.color + ';color:' + c.color + '">' +
        c.label + ' ' + c.val + '</span>';
    }).join('');
  }
}

function renderNodesTable() {
  var tbody = document.getElementById('nodes-tbody');
  if (!tbody) return;

  var filtered = _allNodes;
  if (_nodeSearch) {
    var q = _nodeSearch.toLowerCase();
    filtered = filtered.filter(function (n) {
      return (n.name || '').toLowerCase().indexOf(q) >= 0 ||
        (n.raw_uri || '').toLowerCase().indexOf(q) >= 0 ||
        (n.protocol || '').toLowerCase().indexOf(q) >= 0;
    });
  }

  var total = filtered.length;
  var totalPages = Math.max(1, Math.ceil(total / _nodePageSize));
  if (_nodePage > totalPages) _nodePage = totalPages;
  var start = (_nodePage - 1) * _nodePageSize;
  var pageNodes = filtered.slice(start, start + _nodePageSize);

  if (pageNodes.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">暂无节点</td></tr>';
  } else {
    tbody.innerHTML = pageNodes.map(function (n) {
      var checked = _selectedNodes.has(n.raw_uri) ? 'checked' : '';
      var status = n.disabled ? '<span class="badge badge-red">禁用</span>' :
        (n.health && n.health.cooldown_until > Date.now() / 1000 ?
          '<span class="badge badge-yellow">冷却</span>' :
          '<span class="badge badge-green">启用</span>');
      var latency = (n.health && n.health.last_test_latency) ?
        n.health.last_test_latency + 'ms' : '-';
      var source = n.source_id ? '订阅' : '手动';
      var addr = '';
      try {
        var m = (n.raw_uri || '').match(/@([^:?#]+)/);
        if (m) addr = m[1];
      } catch (e) {}
      return '<tr>' +
        '<td><input type="checkbox" class="node-cb" data-uri="' + esc(n.raw_uri) + '" ' + checked + ' /></td>' +
        '<td>' + esc(n.name || '') + '</td>' +
        '<td>' + esc(n.protocol || '') + '</td>' +
        '<td>' + esc(addr) + '</td>' +
        '<td>' + status + '</td>' +
        '<td>' + latency + '</td>' +
        '<td>' + source + '</td>' +
        '<td class="row-actions">' +
          '<button class="btn-mini" onclick="testNode(\'' + esc(n.raw_uri) + '\')">测试</button>' +
          (n.disabled ?
            '<button class="btn-mini" onclick="enableNode(\'' + esc(n.raw_uri) + '\')">启用</button>' :
            '<button class="btn-mini" onclick="disableNode(\'' + esc(n.raw_uri) + '\')">禁用</button>') +
          '<button class="btn-mini btn-danger" onclick="deleteNode(\'' + esc(n.raw_uri) + '\')">删除</button>' +
        '</td>' +
        '</tr>';
    }).join('');
  }

  // Pagination
  var pag = document.getElementById('node-pagination');
  if (pag) {
    pag.innerHTML = '';
    if (totalPages > 1) {
      for (var i = 1; i <= totalPages; i++) {
        var btn = document.createElement('button');
        btn.className = 'page-btn' + (i === _nodePage ? ' active' : '');
        btn.textContent = i;
        btn.onclick = (function (p) { return function () { _nodePage = p; renderNodesTable(); }; })(i);
        pag.appendChild(btn);
      }
      var info = document.createElement('span');
      info.className = 'muted page-info';
      info.textContent = ' 第 ' + _nodePage + '/' + totalPages + ' 页 (显示 ' + (start + 1) + '-' + Math.min(start + _nodePageSize, total) + ', 共 ' + total + ')';
      pag.appendChild(info);
    }
  }
}

function renderSubscriptions() {
  var container = document.getElementById('subscriptions-list');
  var count = document.getElementById('sub-count');
  if (count) count.textContent = _subscriptions.length + ' 个';
  if (!container) return;

  if (_subscriptions.length === 0) {
    container.innerHTML = '<p class="muted">暂无订阅源。在上方导入框粘贴订阅 URL 并点击导入即可创建。</p>';
    return;
  }

  container.innerHTML = _subscriptions.map(function (s) {
    var status = s.last_status === 'success' ? '<span class="badge badge-green">成功</span>' :
      s.last_status === 'error' ? '<span class="badge badge-red">失败</span>' :
      '<span class="badge badge-yellow">待刷新</span>';
    var nodeCount = s.last_node_count || 0;
    var autoBadge = s.auto_refresh ? '<span class="badge badge-blue">自动</span>' : '<span class="badge badge-grey">手动</span>';
    var urlShort = (s.url || '').substring(0, 60) + ((s.url || '').length > 60 ? '...' : '');
    return '<div class="sub-item">' +
      '<div class="sub-head">' +
        '<strong>' + esc(s.name || urlShort) + '</strong> ' + autoBadge + ' ' + status +
        '<span class="muted"> 节点 ' + nodeCount + '</span>' +
      '</div>' +
      '<div class="muted sub-url">' + esc(s.url || '') + '</div>' +
      (s.last_error ? '<div class="sub-error">错误: ' + esc(s.last_error) + '</div>' : '') +
      '<div class="form-actions sub-actions">' +
        '<button class="btn-mini" onclick="refreshSub(\'' + esc(s.id) + '\')">刷新</button>' +
        '<button class="btn-mini" onclick="deleteSub(\'' + esc(s.id) + '\', false)">仅删源</button>' +
        '<button class="btn-mini btn-danger" onclick="deleteSub(\'' + esc(s.id) + '\', true)">删源和节点</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

// ─── Actions ──────────────────────────────────────────────────────

function initNodesPage() {
  // Mihomo controls
  var startBtn = document.getElementById('mihomo-start-btn');
  var stopBtn = document.getElementById('mihomo-stop-btn');
  var restartBtn = document.getElementById('mihomo-restart-btn');
  if (startBtn) startBtn.onclick = function () {
    api.startMihomo().then(function (r) {
      toast(r.ok ? 'Mihomo: ' + r.message : '启动失败: ' + r.message, r.ok ? 'ok' : 'err');
      loadMihomoStatus();
    }).catch(function (e) { toast('启动失败: ' + e.message, 'err'); });
  };
  if (stopBtn) stopBtn.onclick = function () {
    api.stopMihomo().then(function () {
      toast('Mihomo 已停止', 'ok');
      loadMihomoStatus();
    }).catch(function (e) { toast('停止失败: ' + e.message, 'err'); });
  };
  if (restartBtn) restartBtn.onclick = function () {
    api.stopMihomo().then(function () {
      return api.startMihomo();
    }).then(function (r) {
      toast(r.ok ? 'Mihomo 已重启' : '重启失败: ' + r.message, r.ok ? 'ok' : 'err');
      loadMihomoStatus();
    }).catch(function (e) { toast('重启失败: ' + e.message, 'err'); });
  };

  // Manual import (pasted Clash YAML / node URI only — NOT subscription URLs,
  // which are managed in the dedicated 订阅源同步 card above).
  var singleImportBtn = document.getElementById('single-node-import-btn');
  if (singleImportBtn) singleImportBtn.onclick = function () {
    var input = document.getElementById('single-node-uri');
    var resultEl = document.getElementById('single-node-import-result');
    var uri = (input && input.value || '').trim();
    if (!uri) return toast('请粘贴单个节点 URI', 'warn');
    if (!/^(ss|vmess|vless|trojan|hysteria2|hy2):\/\//i.test(uri)) {
      return toast('只支持 ss/vmess/vless/trojan/hy2 节点 URI', 'warn');
    }
    singleImportBtn.disabled = true;
    if (resultEl) resultEl.textContent = '添加中...';
    api.importNodes(uri).then(function (r) {
      toast('单个节点添加完成: 新增 ' + r.added + ', 跳过 ' + r.skipped, 'ok');
      if (resultEl) resultEl.textContent = '新增 ' + r.added + ', 跳过 ' + r.skipped;
      if (input && r.added > 0) input.value = '';
      loadNodes();
    }).catch(function (e) {
      toast('添加失败: ' + e.message, 'err');
      if (resultEl) resultEl.textContent = '失败: ' + e.message;
    }).then(function () {
      singleImportBtn.disabled = false;
    });
  };

  var importBtn = document.getElementById('import-btn');
  if (importBtn) importBtn.onclick = function () {
    var text = (document.getElementById('import-text') || {}).value || '';
    if (!text.trim()) return toast('请粘贴内容', 'warn');
    var resultEl = document.getElementById('import-result');
    if (resultEl) resultEl.textContent = '导入中...';
    api.importNodes(text).then(function (r) {
      toast('导入完成: 新增 ' + r.added + ', 跳过 ' + r.skipped, 'ok');
      if (resultEl) resultEl.textContent = '新增 ' + r.added + ', 跳过 ' + r.skipped;
      loadNodes();
    }).catch(function (e) {
      toast('导入失败: ' + e.message, 'err');
      if (resultEl) resultEl.textContent = '失败: ' + e.message;
    });
  };

  // Subscription source sync form (add/update source + fetch)
  var subForm = document.getElementById('sub-sync-form');
  if (subForm) subForm.onsubmit = function (e) {
    e.preventDefault();
    var urlInput = document.getElementById('sub-url-input');
    var nameInput = document.getElementById('sub-name-input');
    var autoInput = document.getElementById('sub-auto-refresh-input');
    var intervalInput = document.getElementById('sub-refresh-interval-input');
    var btn = document.getElementById('sub-save-refresh-btn');
    var resultEl = document.getElementById('sub-save-result');
    if (!urlInput) return;
    var url = (urlInput.value || '').trim();
    if (!url) return toast('请输入订阅 URL', 'warn');
    if (!/^https?:\/\//i.test(url)) return toast('URL 须以 http:// 或 https:// 开头', 'warn');
    var name = (nameInput && nameInput.value || '').trim();
    var autoRefresh = !!(autoInput && autoInput.checked);
    var interval = parseInt((intervalInput && intervalInput.value) || '360', 10);
    if (isNaN(interval) || interval < 10) interval = 10;
    if (interval > 10080) interval = 10080;
    if (btn) btn.disabled = true;
    if (resultEl) resultEl.textContent = '拉取中...';
    api.fetchSubscription({
      url: url,
      name: name,
      auto_refresh: autoRefresh,
      refresh_interval_minutes: interval,
      adopt_existing: true
    }).then(function (r) {
      var added = (r.result && r.result.added) || 0;
      var updated = (r.result && r.result.updated) || 0;
      var adopted = (r.result && r.result.adopted) || 0;
      toast('订阅拉取成功', 'ok');
      if (resultEl) resultEl.textContent = '新增 ' + added + ' / 更新 ' + updated + ' / 认领 ' + adopted;
      // Reset for next add but keep URL for convenience
      if (nameInput) nameInput.value = '';
      loadNodes(); loadSubscriptions();
    }).catch(function (e) {
      toast('订阅拉取失败: ' + e.message, 'err');
      if (resultEl) resultEl.textContent = '失败: ' + e.message;
    }).then(function () {
      if (btn) btn.disabled = false;
    });
  };

  // Search
  var search = document.getElementById('node-search');
  if (search) search.oninput = function () {
    clearTimeout(_nodeSearchTimer);
    _nodeSearchTimer = setTimeout(function () {
      _nodeSearch = search.value.trim();
      _nodePage = 1;
      renderNodesTable();
    }, 200);
  };

  // Select all
  var selectAll = document.getElementById('node-select-all');
  if (selectAll) selectAll.onchange = function () {
    var cbs = document.querySelectorAll('.node-cb');
    cbs.forEach(function (cb) {
      if (selectAll.checked) _selectedNodes.add(cb.dataset.uri);
      else _selectedNodes.delete(cb.dataset.uri);
      cb.checked = selectAll.checked;
    });
  };

  // Batch actions
  var dedupBtn = document.getElementById('node-dedup-btn');
  if (dedupBtn) dedupBtn.onclick = function () {
    api.dedupNodes().then(function (r) {
      toast('去重完成: 删除 ' + r.removed + ' 个重复', 'ok');
      loadNodes();
    }).catch(function (e) { toast('去重失败: ' + e.message, 'err'); });
  };

  var delDisabledBtn = document.getElementById('node-del-disabled-btn');
  if (delDisabledBtn) delDisabledBtn.onclick = function () {
    if (!confirm('确定删除所有已禁用节点?')) return;
    api.deleteDisabledNodes().then(function (r) {
      toast('删除 ' + r.removed + ' 个禁用节点', 'ok');
      loadNodes();
    }).catch(function (e) { toast('删除失败: ' + e.message, 'err'); });
  };

  var batchEnableBtn = document.getElementById('node-batch-enable-btn');
  if (batchEnableBtn) batchEnableBtn.onclick = function () {
    var uris = Array.from(_selectedNodes);
    if (!uris.length) return toast('请先选中节点', 'warn');
    api.batchEnable(uris).then(function (r) {
      toast('启用 ' + r.changed + ' 个节点', 'ok');
      _selectedNodes.clear(); loadNodes();
    }).catch(function (e) { toast('批量启用失败: ' + e.message, 'err'); });
  };

  var batchDisableBtn = document.getElementById('node-batch-disable-btn');
  if (batchDisableBtn) batchDisableBtn.onclick = function () {
    var uris = Array.from(_selectedNodes);
    if (!uris.length) return toast('请先选中节点', 'warn');
    api.batchDisable(uris).then(function (r) {
      toast('禁用 ' + r.changed + ' 个节点', 'ok');
      _selectedNodes.clear(); loadNodes();
    }).catch(function (e) { toast('批量禁用失败: ' + e.message, 'err'); });
  };

  var batchDeleteBtn = document.getElementById('node-batch-delete-btn');
  if (batchDeleteBtn) batchDeleteBtn.onclick = function () {
    var uris = Array.from(_selectedNodes);
    if (!uris.length) return toast('请先选中节点', 'warn');
    if (!confirm('确定删除选中的 ' + uris.length + ' 个节点?')) return;
    api.batchDelete(uris).then(function (r) {
      toast('删除 ' + r.removed + ' 个节点', 'ok');
      _selectedNodes.clear(); loadNodes();
    }).catch(function (e) { toast('批量删除失败: ' + e.message, 'err'); });
  };

  // Data loading is handled by the router via pageNodes.load().
}

function loadNodesPage() {
  return Promise.all([loadMihomoStatus(), loadNodes(), loadSubscriptions()]);
}

function testNode(rawUri) {
  toast('测试中...');
  api.testNode(rawUri).then(function (r) {
    if (r.success) {
      toast('测试成功: ' + r.latency_ms + 'ms', 'ok');
    } else {
      toast('测试失败: ' + (r.error || '未知'), 'err');
    }
    loadNodes();
  }).catch(function (e) { toast('测试失败: ' + e.message, 'err'); });
}

function enableNode(rawUri) {
  api.enableNode(rawUri).then(function () {
    toast('已启用', 'ok'); loadNodes();
  }).catch(function (e) { toast('启用失败: ' + e.message, 'err'); });
}

function disableNode(rawUri) {
  api.disableNode(rawUri).then(function () {
    toast('已禁用', 'ok'); loadNodes();
  }).catch(function (e) { toast('禁用失败: ' + e.message, 'err'); });
}

function deleteNode(rawUri) {
  if (!confirm('确定删除此节点?')) return;
  api.deleteNode(rawUri).then(function () {
    toast('已删除', 'ok'); loadNodes();
  }).catch(function (e) { toast('删除失败: ' + e.message, 'err'); });
}

function refreshSub(id) {
  toast('刷新中...');
  api.refreshSubscription(id).then(function (r) {
    toast('刷新成功: 新增 ' + (r.result.added || 0) + ' / 更新 ' + (r.result.updated || 0), 'ok');
    loadNodes(); loadSubscriptions();
  }).catch(function (e) { toast('刷新失败: ' + e.message, 'err'); loadSubscriptions(); });
}

function deleteSub(id, deleteNodes) {
  var msg = deleteNodes ? '确定删除此订阅源及其所有节点?' : '确定删除此订阅源? (节点保留为手动)';
  if (!confirm(msg)) return;
  api.deleteSubscription(id, deleteNodes).then(function () {
    toast(deleteNodes ? '已删除订阅源和节点' : '已删除订阅源', 'ok');
    loadNodes(); loadSubscriptions();
  }).catch(function (e) { toast('删除失败: ' + e.message, 'err'); });
}

window.pageNodes = { init: initNodesPage, load: loadNodesPage };
