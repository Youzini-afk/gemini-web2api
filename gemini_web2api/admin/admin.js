/* ──────────────────────────────────────────────────────────────────
 * admin.js — App bootstrap: login flow, router, page wiring.
 * Exposes showLogin() globally for api.js to call on 401.
 * ────────────────────────────────────────────────────────────────── */

(function (global) {
  'use strict';

  // Page registry: route id → { title, page module }
  var PAGES = {
    overview:   { title: '概览',   module: 'pageOverview' },
    settings:   { title: '设置',   module: 'pageSettings' },
    keys:       { title: '密钥',   module: 'pageKeys' },
    models:     { title: '模型',   module: 'pageModels' },
    nodes:      { title: '节点',   module: 'pageNodes' },
    appearance: { title: '外观',   module: 'pageAppearance' }
  };
  var DEFAULT_ROUTE = 'overview';

  var state = {
    authed: false,
    route: null,
    pagesInitialized: {},
    pagesLoaded: {}
  };

  // ─── Login overlay ───────────────────────────────────────────────

  function showLogin(silent) {
    state.authed = false;
    var overlay = $('#login-overlay');
    var app = $('#app');
    if (overlay) overlay.classList.remove('hidden');
    if (app) app.classList.add('hidden');
    if (!silent) {
      var pw = $('#login-password');
      if (pw) { pw.value = ''; setTimeout(function () { pw.focus(); }, 50); }
    }
    var errEl = $('#login-error');
    if (errEl) errEl.textContent = silent ? '会话已过期,请重新登录' : '';
  }

  function hideLogin() {
    var overlay = $('#login-overlay');
    if (overlay) overlay.classList.add('hidden');
    var app = $('#app');
    if (app) app.classList.remove('hidden');
  }

  // Exposed for api.js 401 handler
  global.showLogin = showLogin;

  function onLoginSubmit(e) {
    e.preventDefault();
    var pwEl = $('#login-password');
    var errEl = $('#login-error');
    var btn = e.target.querySelector('button[type="submit"]');
    var pw = pwEl ? pwEl.value : '';
    if (!pw) {
      if (errEl) errEl.textContent = '请输入密码';
      return;
    }
    if (errEl) errEl.textContent = '';
    if (btn) { btn.disabled = true; btn.textContent = '登录中...'; }
    api.login(pw).then(function () {
      state.authed = true;
      hideLogin();
      toast.success('登录成功');
      // Route to current/overview and load
      var route = currentRouteFromHash() || DEFAULT_ROUTE;
      go(route, true);
    }).catch(function (e) {
      if (errEl) errEl.textContent = e.status === 401 ? '密码错误' : ('登录失败: ' + e.message);
      if (pwEl) pwEl.focus();
    }).then(function () {
      if (btn) { btn.disabled = false; btn.textContent = '登 录'; }
    });
  }

  function onLogout() {
    api.logout().catch(function () {}).then(function () {
      state.authed = false;
      showLogin(true);
    });
  }

  // ─── Router ──────────────────────────────────────────────────────

  function currentRouteFromHash() {
    var h = (location.hash || '').replace(/^#\/?/, '');
    return PAGES[h] ? h : '';
  }

  function setActiveNav(route) {
    $$('.nav-item[data-route]').forEach(function (el) {
      el.classList.toggle('active', el.getAttribute('data-route') === route);
    });
  }

  function showPageSection(route) {
    $$('.page').forEach(function (el) { el.classList.add('hidden'); });
    var sec = $('#page-' + route);
    if (sec) sec.classList.remove('hidden');
  }

  /**
   * Navigate to a route. Initializes + loads the page module on first visit.
   * @param {string} route
   * @param {boolean} forceLoad  reload even if already loaded
   */
  function go(route, forceLoad) {
    if (!PAGES[route]) route = DEFAULT_ROUTE;
    if (!state.authed) {
      // Remember intended route, but stay on login
      location.hash = '#/' + route;
      showLogin(true);
      return;
    }

    state.route = route;
    location.hash = '#/' + route;
    setActiveNav(route);
    showPageSection(route);

    var titleEl = $('#page-title');
    if (titleEl) titleEl.textContent = PAGES[route].title;

    // Init page module once
    if (!state.pagesInitialized[route]) {
      var modName = PAGES[route].module;
      var mod = global[modName];
      if (mod && typeof mod.init === 'function') {
        try { mod.init(); } catch (e) { console.error('init ' + route, e); }
      }
      state.pagesInitialized[route] = true;
    }

    // Load page data unless already loaded (and not forced)
    if (forceLoad || !state.pagesLoaded[route]) {
      var mod2 = global[PAGES[route].module];
      if (mod2 && typeof mod2.load === 'function') {
        mod2.load().then(function () {
          state.pagesLoaded[route] = true;
        }).catch(function (e) { console.error('load ' + route, e); });
      } else {
        state.pagesLoaded[route] = true;
      }
    }

    // Close mobile sidebar after navigation
    closeSidebar();
  }

  global.go = go;

  // ─── Mobile sidebar ─────────────────────────────────────────────

  function openSidebar() {
    var sb = $('#sidebar');
    var bd = $('#sidebar-backdrop');
    if (sb) sb.classList.add('open');
    if (bd) bd.classList.remove('hidden');
  }

  function closeSidebar() {
    var sb = $('#sidebar');
    var bd = $('#sidebar-backdrop');
    if (sb) sb.classList.remove('open');
    if (bd) bd.classList.add('hidden');
  }

  // ─── Bootstrap ───────────────────────────────────────────────────

  function bindStaticEvents() {
    var loginForm = $('#login-form');
    if (loginForm) loginForm.addEventListener('submit', onLoginSubmit);

    var logoutBtn = $('#logout-btn');
    if (logoutBtn) logoutBtn.addEventListener('click', onLogout);

    // Nav clicks → go()
    $$('.nav-item[data-route]').forEach(function (el) {
      el.addEventListener('click', function (e) {
        e.preventDefault();
        var route = el.getAttribute('data-route');
        go(route);
      });
    });

    // Mobile menu toggle
    var mt = $('#menu-toggle');
    if (mt) mt.addEventListener('click', openSidebar);
    var bd = $('#sidebar-backdrop');
    if (bd) bd.addEventListener('click', closeSidebar);

    // Hash routing (back/forward)
    global.addEventListener('hashchange', function () {
      var route = currentRouteFromHash();
      if (route && route !== state.route) go(route);
    });

    // Enter key in password field
    var pw = $('#login-password');
    if (pw) {
      pw.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          var form = $('#login-form');
          if (form) form.dispatchEvent(new Event('submit', { cancelable: true }));
        }
      });
    }
  }

  function bootstrap() {
    bindStaticEvents();
    // Restore background early (appearance page also restores on load,
    // but doing it here too avoids a flash if scripts load slowly).
    try { global.pageAppearance && global.pageAppearance.init && global.pageAppearance.init(); } catch (e) {}

    // Check auth on load
    api.checkAuth().then(function (data) {
      if (data && data.authenticated) {
        state.authed = true;
        hideLogin();
        var route = currentRouteFromHash() || DEFAULT_ROUTE;
        go(route, true);
      } else {
        showLogin(false);
      }
    }).catch(function () {
      // Network error — show login so user can retry
      showLogin(false);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }

})(window);
