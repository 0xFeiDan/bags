document.addEventListener('DOMContentLoaded', async () => {
  const loginForm = document.getElementById('loginForm');
  const totpForm = document.getElementById('totpForm');
  const title = document.getElementById('formTitle');
  const lead = document.getElementById('formLead');
  const submit = document.getElementById('submitButton');
  const message = document.getElementById('message');
  let challenge = '';
  let registerMode = false;

  function showMessage(text, ok = false) {
    message.textContent = text;
    message.classList.toggle('ok', ok);
  }

  try {
    await BagsAuth.api('/auth/me');
    location.replace('/');
    return;
  } catch (error) {
    if (error.status && error.status !== 401) showMessage('无法连接后端服务，请确认 API 已启动。');
  }

  try {
    const status = await BagsAuth.api('/auth/bootstrap-status');
    registerMode = status.registration_available;
    if (registerMode) {
      title.textContent = '创建管理员账户';
      lead.textContent = '这是首次启动。创建后将关闭公开注册。';
      submit.textContent = '创建并进入';
      document.getElementById('rememberRow').hidden = true;
      document.getElementById('bootstrapRow').hidden = false;
      document.getElementById('password').autocomplete = 'new-password';
    }
  } catch (_) {
    showMessage('无法连接后端服务，请确认 API 已启动。');
  }

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    showMessage('');
    submit.disabled = true;
    const email = document.getElementById('email').value;
    const password = document.getElementById('password').value;
    try {
      const headers = registerMode ? { 'X-Bootstrap-Token': document.getElementById('bootstrapToken').value } : {};
      const result = await BagsAuth.api(registerMode ? '/auth/register' : '/auth/login', {
        method: 'POST',
        headers,
        body: JSON.stringify(registerMode ? { email, password } : { email, password, remember_me: document.getElementById('remember').checked }),
      });
      if (result.totp_required) {
        challenge = result.challenge;
        loginForm.hidden = true;
        totpForm.hidden = false;
        title.textContent = '输入动态验证码';
        lead.textContent = '密码验证通过，请完成第二步验证。';
        document.getElementById('totpCode').focus();
      } else {
        location.replace('/');
      }
    } catch (error) {
      showMessage(error.message);
    } finally {
      submit.disabled = false;
    }
  });

  totpForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    showMessage('');
    try {
      await BagsAuth.api('/auth/login/totp', { method: 'POST', body: JSON.stringify({ challenge, code: document.getElementById('totpCode').value }) });
      location.replace('/');
    } catch (error) {
      showMessage(error.message);
    }
  });

  document.getElementById('backButton').addEventListener('click', () => {
    challenge = '';
    totpForm.hidden = true;
    loginForm.hidden = false;
    title.textContent = '登录资产账本';
    lead.textContent = '使用管理员邮箱和密码继续。';
  });
});
