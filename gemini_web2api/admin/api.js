/* ──────────────────────────────────────────────────────────────────
 * api.js — Admin API client.
 * Wraps every fetch; on 401 calls showLogin() (defined in admin.js).
 * Cookie-based auth uses credentials: 'same-origin'.
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  var BASE = '/api/admin';

  /** Extract a human error message from any failed response. */
  function extractError(data, status, fallback) {
    if (data && data.error) {
      if (typeof data.error === 'string') return data.error;
      if (data.error.message) return data.error.message;
    }
    if (status === 401) return '未授权,请重新登录';
    if (status === 403) return '跨域请求被拒绝';
    if (status === 404) return '未找到资源';
    if (status >= 500) return '服务端错误 (' + status + ')';
    return fallback || ('请求失败 (' + status + ')');
  }

  /**
   * Core fetch wrapper.
   * @param {string} path  path under /api/admin/
   * @param {object} opts  method, body, query
   * @returns {Promise<{ok, status, data}>}
   */
  function request(path, opts) {
    opts = opts || {};
    var method = opts.method || 'GET';
    var url = BASE + path;

    if (opts.query) {
      var qs = [];
      Object.keys(opts.query).forEach(function (k) {
        if (opts.query[k] === undefined || opts.query[k] === null) return;
        qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(opts.query[k]));
      });
      if (qs.length) url += '?' + qs.join('&');
    }

    var fetchOpts = {
      method: method,
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    };

    if (opts.body !== undefined && method !== 'GET' && method !== 'HEAD') {
      fetchOpts.headers['Content-Type'] = 'application/json';
      fetchOpts.body = JSON.stringify(opts.body);
    }

    return fetch(url, fetchOpts).then(function (res) {
      // Try to parse JSON, tolerate empty bodies
      var text = '';
      return res.text().then(function (t) {
        text = t;
        if (!text) return null;
        try { return JSON.parse(text); }
        catch (e) { return null; }
      }).then(function (data) {
        if (!res.ok) {
          var msg = extractError(data, res.status, opts.fallback);
          var err = new Error(msg);
          err.status = res.status;
          err.data = data;
          // Auth expired → show login overlay
          if (res.status === 401 && typeof global.showLogin === 'function') {
            global.showLogin(true);
          }
          throw err;
        }
        return { ok: true, status: res.status, data: data };
      });
    }).catch(function (err) {
      // Network / abort errors
      if (err.status) throw err;
      var netErr = new Error('网络错误: ' + (err.message || '无法连接服务'));
      netErr.status = 0;
      throw netErr;
    });
  }

  // ─── Endpoint wrappers ───────────────────────────────────────────

  var api = {
    request: request,

    checkAuth: function () {
      return request('/check-auth').then(function (r) { return r.data; });
    },
    login: function (password) {
      return request('/login', { method: 'POST', body: { password: password } }).then(function (r) { return r.data; });
    },
    logout: function () {
      return request('/logout', { method: 'POST' }).then(function (r) { return r.data; });
    },

    getSettings: function () {
      return request('/settings').then(function (r) { return r.data; });
    },
    putSettings: function (changes) {
      return request('/settings', { method: 'PUT', body: changes }).then(function (r) { return r.data; });
    },

    getStats: function () {
      return request('/stats').then(function (r) { return r.data; });
    },
    resetStats: function () {
      return request('/stats/reset', { method: 'POST' }).then(function (r) { return r.data; });
    },
    getHistory: function () {
      return request('/history').then(function (r) { return r.data; });
    },

    getKeys: function () {
      return request('/keys').then(function (r) { return r.data; });
    },
    addKey: function (name, key, description) {
      return request('/keys', { method: 'POST', body: { name: name, key: key, description: description } }).then(function (r) { return r.data; });
    },
    deleteKey: function (name) {
      return request('/keys/' + encodeURIComponent(name), { method: 'DELETE' }).then(function (r) { return r.data; });
    },

    getModels: function () {
      return request('/models').then(function (r) { return r.data; });
    },
    putModels: function (payload) {
      return request('/models', { method: 'PUT', body: payload || {} }).then(function (r) { return r.data; });
    },

    // ─── Nodes ───────────────────────────────────────────────────
    getNodes: function () {
      return request('/nodes').then(function (r) { return r.data; });
    },
    deleteNode: function (rawUri) {
      return request('/nodes', { method: 'DELETE', body: { raw_uri: rawUri } }).then(function (r) { return r.data; });
    },
    testNode: function (rawUri, timeoutSec) {
      return request('/nodes/test', { method: 'POST', body: { raw_uri: rawUri, timeout_seconds: timeoutSec || 10 } }).then(function (r) { return r.data; });
    },
    enableNode: function (rawUri) {
      return request('/nodes/enable', { method: 'POST', body: { raw_uri: rawUri } }).then(function (r) { return r.data; });
    },
    disableNode: function (rawUri) {
      return request('/nodes/disable', { method: 'POST', body: { raw_uri: rawUri } }).then(function (r) { return r.data; });
    },
    importNodes: function (text) {
      return request('/nodes/import', { method: 'POST', body: { text: text } }).then(function (r) { return r.data; });
    },
    batchEnable: function (uris) {
      return request('/nodes/batch-enable', { method: 'POST', body: { uris: uris } }).then(function (r) { return r.data; });
    },
    batchDisable: function (uris) {
      return request('/nodes/batch-disable', { method: 'POST', body: { uris: uris } }).then(function (r) { return r.data; });
    },
    batchDelete: function (uris) {
      return request('/nodes/batch-delete', { method: 'POST', body: { uris: uris } }).then(function (r) { return r.data; });
    },
    dedupNodes: function () {
      return request('/nodes/dedup', { method: 'POST' }).then(function (r) { return r.data; });
    },
    deleteDisabledNodes: function () {
      return request('/nodes/disabled', { method: 'DELETE' }).then(function (r) { return r.data; });
    },

    // ─── Subscriptions ──────────────────────────────────────────
    getSubscriptions: function () {
      return request('/subscriptions').then(function (r) { return r.data; });
    },
    fetchSubscription: function (payload) {
      return request('/subscriptions/fetch', { method: 'POST', body: payload }).then(function (r) { return r.data; });
    },
    refreshSubscription: function (id) {
      return request('/subscriptions/refresh', { method: 'POST', body: { id: id } }).then(function (r) { return r.data; });
    },
    refreshAllSubscriptions: function () {
      return request('/subscriptions/refresh-all', { method: 'POST' }).then(function (r) { return r.data; });
    },
    updateSubscription: function (payload) {
      return request('/subscriptions', { method: 'PUT', body: payload }).then(function (r) { return r.data; });
    },
    deleteSubscription: function (id, deleteNodes) {
      return request('/subscriptions', { method: 'DELETE', body: { id: id, delete_nodes: deleteNodes } }).then(function (r) { return r.data; });
    },

    // ─── Mihomo ─────────────────────────────────────────────────
    getMihomoStatus: function () {
      return request('/mihomo/status').then(function (r) { return r.data; });
    },
    startMihomo: function () {
      return request('/mihomo/start', { method: 'POST' }).then(function (r) { return r.data; });
    },
    stopMihomo: function () {
      return request('/mihomo/stop', { method: 'POST' }).then(function (r) { return r.data; });
    },
    switchMihomo: function (rawUri) {
      return request('/mihomo/switch', { method: 'POST', body: { raw_uri: rawUri } }).then(function (r) { return r.data; });
    }
  };

  global.api = api;
  global.extractError = extractError;
})(window);
