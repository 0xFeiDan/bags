(function () {
  const host = location.hostname === 'localhost' ? 'localhost' : '127.0.0.1';
  const localPreview = location.port === '4173';
  const API_BASE = window.BAGS_API_BASE || (localPreview ? `http://${host}:8000/api/v1` : `${location.origin}/api/v1`);

  function cookie(name) {
    const item = document.cookie.split('; ').find((part) => part.startsWith(`${name}=`));
    return item ? decodeURIComponent(item.slice(name.length + 1)) : '';
  }

  async function api(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const csrf = cookie('bags_csrf');
      if (csrf) headers.set('X-CSRF-Token', csrf);
    }
    const response = await fetch(`${API_BASE}${path}`, { ...options, method, headers, credentials: 'include' });
    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('application/json') ? await response.json() : { detail: await response.text() };
    if (!response.ok) {
      const error = new Error(data.detail || '请求失败，请稍后重试');
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  async function guard() {
    if (document.body.dataset.auth !== 'required') return;
    try {
      const user = await api('/auth/me');
      document.querySelectorAll('[data-current-email]').forEach((node) => { node.textContent = user.email; });
      document.body.classList.remove('auth-pending');
    } catch (error) {
      if (error.status === 401) {
        location.replace('/login.html');
        return;
      }
      document.body.classList.remove('auth-pending');
      document.body.innerHTML = '<main class="auth-fatal">后端服务暂时不可用，请确认 Bags API 已启动后刷新页面。</main>';
    }
  }

  window.BagsAuth = { api, cookie, API_BASE };
  document.addEventListener('DOMContentLoaded', guard);
})();
