document.addEventListener('DOMContentLoaded', async () => {
  let user;
  const byId = (id) => document.getElementById(id);
  const message = (id, text, ok = false) => { const node = byId(id); node.textContent = text; node.classList.toggle('ok', ok); };

  async function refreshUser() {
    user = await BagsAuth.api('/auth/me');
    document.querySelectorAll('[data-current-email]').forEach((node) => { node.textContent = user.email; });
    byId('verifyTotpRow').hidden = !user.two_factor_enabled;
    byId('totpStatus').textContent = user.two_factor_enabled ? '已启用' : '未启用';
    byId('totpStatus').classList.toggle('off', !user.two_factor_enabled);
    byId('totpSetup').hidden = user.two_factor_enabled;
    byId('totpDisable').hidden = !user.two_factor_enabled;
  }

  async function refreshSessions() {
    const sessions = await BagsAuth.api('/auth/sessions');
    byId('sessions').innerHTML = sessions.map((item) => `<div class="session"><div><b>${item.current ? '当前设备' : '已登录设备'}</b><span>${escapeHtml(item.user_agent || '未知浏览器')}</span><span>${escapeHtml(item.ip_address || '未知 IP')} · 到期 ${new Date(item.expires_at).toLocaleString()}</span></div>${item.current ? '' : `<button class="danger revoke" data-id="${item.id}" type="button">退出</button>`}</div>`).join('') || '<p class="lede">没有有效会话。</p>';
    document.querySelectorAll('.revoke').forEach((button) => button.addEventListener('click', async () => {
      try { await BagsAuth.api(`/auth/sessions/${button.dataset.id}`, { method: 'DELETE' }); await refreshSessions(); message('sessionMessage', '设备已退出。', true); }
      catch (error) { message('sessionMessage', error.message); }
    }));
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  }

  try { await refreshUser(); await refreshSessions(); } catch (_) { return; }

  byId('verifyForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await BagsAuth.api('/auth/sensitive/verify', { method: 'POST', body: JSON.stringify({ current_password: byId('currentPassword').value, totp_code: byId('verifyTotp').value || null }) });
      byId('currentPassword').value = ''; byId('verifyTotp').value = '';
      message('verifyMessage', '验证通过，10 分钟内有效。', true);
    } catch (error) { message('verifyMessage', error.message); }
  });

  byId('totpSetup').addEventListener('click', async () => {
    try {
      const result = await BagsAuth.api('/auth/totp/setup', { method: 'POST' });
      byId('totpSecret').textContent = `密钥：${result.secret}`;
      byId('totpUri').textContent = `验证器 URI：${result.provisioning_uri}`;
      byId('totpEnroll').hidden = false;
      message('totpMessage', '请将密钥或 URI 添加到验证器，再输入验证码确认。', true);
    } catch (error) { message('totpMessage', error.message); }
  });

  byId('totpConfirm').addEventListener('click', async () => {
    try {
      await BagsAuth.api('/auth/totp/confirm', { method: 'POST', body: JSON.stringify({ code: byId('totpConfirmCode').value }) });
      byId('totpEnroll').hidden = true; await refreshUser(); message('totpMessage', '双因素验证已启用。', true);
    } catch (error) { message('totpMessage', error.message); }
  });

  byId('totpDisable').addEventListener('click', async () => {
    try { await BagsAuth.api('/auth/totp', { method: 'DELETE' }); await refreshUser(); message('totpMessage', '双因素验证已关闭。', true); }
    catch (error) { message('totpMessage', error.message); }
  });

  byId('passwordForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try { await BagsAuth.api('/auth/password', { method: 'POST', body: JSON.stringify({ new_password: byId('newPassword').value }) }); byId('newPassword').value = ''; message('passwordMessage', '密码已更新。', true); await refreshSessions(); }
    catch (error) { message('passwordMessage', error.message); }
  });

  byId('emailForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await BagsAuth.api('/auth/email', { method: 'POST', body: JSON.stringify({ new_email: byId('newEmail').value }) });
      byId('newEmail').value = ''; await refreshUser(); message('accountMessage', '登录邮箱已更新。', true);
    } catch (error) { message('accountMessage', error.message); }
  });

  byId('logoutOthers').addEventListener('click', async () => {
    try { await BagsAuth.api('/auth/sessions/logout-others', { method: 'POST' }); await refreshSessions(); message('sessionMessage', '其他设备已全部退出。', true); }
    catch (error) { message('sessionMessage', error.message); }
  });

  byId('logout').addEventListener('click', async () => {
    try { await BagsAuth.api('/auth/logout', { method: 'POST' }); location.replace('/login.html'); }
    catch (error) { message('accountMessage', error.message); }
  });
});
