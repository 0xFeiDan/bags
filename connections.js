(function () {
  'use strict';

  const state = {
    step: 1,
    source: 'binance',
    mode: 'create',
    editConnectionId: null,
    user: null,
    portfolios: [],
    accounts: [],
    connections: [],
    chains: [],
  };

  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);

  function setMessage(id, message, success = false) {
    const node = byId(id);
    node.textContent = message || '';
    node.classList.toggle('success', success);
  }

  function setSystemState(message, kind = '') {
    const node = byId('systemState');
    node.className = `system-state ${kind}`.trim();
    node.querySelector('span:last-child').textContent = message;
  }

  function setBusy(busy, label = '') {
    document.body.classList.toggle('wizard-busy', busy);
    const button = byId('connectNow');
    button.disabled = busy;
    button.textContent = busy ? (label || '正在处理…') : '验证并开始同步';
  }

  function setStep(step) {
    state.step = step;
    document.querySelectorAll('[data-step]').forEach((section) => {
      const active = Number(section.dataset.step) === step;
      section.hidden = !active;
      section.classList.toggle('active', active);
    });
    document.querySelectorAll('[data-step-indicator]').forEach((item) => {
      const itemStep = Number(item.dataset.stepIndicator);
      item.classList.toggle('active', itemStep === step);
      item.classList.toggle('done', itemStep < step);
      if (itemStep === step) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
    byId('connectionWizard').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  }

  function selectedPortfolio() {
    return byId('portfolioSelect').value;
  }

  function populatePortfolios(preferredId = null) {
    const select = byId('portfolioSelect');
    const previous = preferredId || select.value;
    select.innerHTML = `${state.portfolios.map((portfolio) => `<option value="${portfolio.id}">${escapeHtml(portfolio.name)} · ${escapeHtml(portfolio.base_currency)}</option>`).join('')}<option value="__new__">创建新的 Portfolio</option>`;
    if (previous && [...select.options].some((option) => option.value === previous)) select.value = previous;
    else select.value = state.portfolios[0]?.id || '__new__';
    updatePortfolioMode();
  }

  function updatePortfolioMode() {
    const creating = selectedPortfolio() === '__new__';
    byId('newPortfolioField').hidden = !creating;
    byId('portfolioName').required = creating;
  }

  function populateChains() {
    const configured = state.chains.filter((chain) => chain.configured);
    byId('evmChain').innerHTML = configured.length
      ? configured.map((chain) => `<option value="${escapeHtml(chain.key)}">${escapeHtml(chain.name)} · ${escapeHtml(chain.native_symbol)}</option>`).join('')
      : '<option value="">服务器尚未配置 EVM RPC</option>';
    byId('evmChain').disabled = !configured.length;
    const source = byId('evmSourceOption');
    source.classList.toggle('unavailable', !configured.length);
    source.querySelector('input').disabled = !configured.length;
    byId('evmSourceStatus').textContent = configured.length ? `${configured.length} 条网络可用` : 'RPC 未配置';
    byId('evmChainHelp').textContent = configured.length
      ? `可用网络：${configured.map((chain) => chain.name).join('、')}`
      : '请先在服务器 .env 配置至少一个 EVM_*_RPC_URL，再重启 API。';
  }

  function updateSourceForms() {
    document.querySelectorAll('.source-option').forEach((option) => {
      option.classList.toggle('selected', option.querySelector('input').value === state.source);
    });
    document.querySelectorAll('[data-source-form]').forEach((form) => {
      const active = form.dataset.sourceForm === state.source;
      form.hidden = !active;
      form.querySelectorAll('input, select, textarea').forEach((control) => { control.disabled = !active; });
    });
    const labels = {
      binance: '将验证只读权限，并按你选择的产品读取余额、成交、资金费和转账历史。',
      bybit: '使用 Bybit V5 读取 Unified 余额、Linear / Inverse 仓位、成交和资金流水。',
      bitget: '使用 Bitget V2 读取 Spot、三类合约、充值提现、成交和资金账单。',
      hyperliquid: '只读取公开地址对应的账户权益、仓位、成交、Funding 和 Spot 余额。',
      evm: '只通过服务器 RPC 查询公开地址，不需要也不接受任何签名凭据。',
    };
    byId('detailsLead').textContent = state.mode === 'update'
      ? `输入新的 ${sourceName()} 只读凭据。旧凭据会被新的加密值替换。`
      : labels[state.source];
    if (state.mode === 'update') {
      const label = byId(`${state.source}Label`);
      if (label) label.disabled = true;
    }
  }

  function splitValues(value) {
    return [...new Set(String(value || '').split(/[\s,]+/).map((item) => item.trim()).filter(Boolean))];
  }

  function isoOrNull(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }

  function getProducts(source = state.source) {
    return [...document.querySelectorAll(`input[name="${source}Product"]:checked`)].map((input) => input.value);
  }

  function sourceName(source = state.source) {
    return { binance: 'Binance', bybit: 'Bybit', bitget: 'Bitget', hyperliquid: 'Hyperliquid', evm: 'EVM Wallet' }[source] || source;
  }

  function connectionSummary() {
    if (['binance', 'bybit', 'bitget'].includes(state.source)) {
      const names = {
        binance: { spot: 'Spot', usdm: 'USD-M', coinm: 'COIN-M' },
        bybit: { spot: 'Spot', linear: 'Linear', inverse: 'Inverse' },
        bitget: { spot: 'Spot', 'usdt-futures': 'USDT Futures', 'usdc-futures': 'USDC Futures', 'coin-futures': 'COIN Futures' },
      };
      return {
        label: byId(`${state.source}Label`).value.trim(),
        scope: getProducts().map((product) => names[state.source][product]).join(' · '),
      };
    }
    if (state.source === 'hyperliquid') {
      return {
        label: byId('hyperliquidLabel').value.trim(),
        scope: byId('hyperliquidSpot').checked ? 'Perp · Spot · Funding' : 'Perp · Funding',
      };
    }
    const chain = state.chains.find((item) => item.key === byId('evmChain').value);
    return { label: byId('evmLabel').value.trim(), scope: chain?.name || 'EVM' };
  }

  function showReview() {
    const summary = connectionSummary();
    const portfolio = state.mode === 'update'
      ? state.portfolios.find((item) => item.id === state.accounts.find((account) => account.id === state.connections.find((connection) => connection.id === state.editConnectionId)?.account_id)?.portfolio_id)?.name
      : (selectedPortfolio() === '__new__' ? byId('portfolioName').value.trim() : state.portfolios.find((item) => item.id === selectedPortfolio())?.name);
    byId('connectionReview').innerHTML = [
      ['数据来源', sourceName()],
      ['账户', summary.label],
      ['读取范围', summary.scope],
      ['Portfolio', portfolio || '—'],
      ['权限', '只读'],
      ['操作', state.mode === 'update' ? '替换凭据并同步' : '创建并首次同步'],
    ].map(([label, value]) => `<span><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></span>`).join('');
    byId('verifyTotpField').hidden = !state.user?.two_factor_enabled;
    byId('verifyTotp').required = Boolean(state.user?.two_factor_enabled);
  }

  function validatePortfolio() {
    if (selectedPortfolio() !== '__new__') return true;
    const name = byId('portfolioName').value.trim();
    if (!name) {
      byId('portfolioName').focus();
      byId('portfolioName').reportValidity();
      return false;
    }
    return true;
  }

  function validateDetails() {
    const form = byId('detailsForm');
    if (!form.reportValidity()) return false;
    if (['binance', 'bybit', 'bitget'].includes(state.source) && !getProducts().length) {
      setMessage('detailsMessage', `至少选择一个 ${sourceName()} 产品。`);
      return false;
    }
    if (state.source === 'evm' && !byId('evmChain').value) {
      setMessage('detailsMessage', '服务器尚未配置可用的 EVM RPC。');
      return false;
    }
    if (state.source === 'evm') {
      const from = byId('evmFromBlock').value;
      const to = byId('evmToBlock').value;
      if (from && to && Number(from) > Number(to)) {
        setMessage('detailsMessage', '起始区块不能大于结束区块。');
        return false;
      }
    }
    setMessage('detailsMessage', '');
    return true;
  }

  async function ensurePortfolio() {
    if (selectedPortfolio() !== '__new__') return selectedPortfolio();
    const portfolio = await BagsAuth.api('/portfolios', {
      method: 'POST',
      body: JSON.stringify({ name: byId('portfolioName').value.trim(), base_currency: 'USD' }),
    });
    state.portfolios.push(portfolio);
    populatePortfolios(portfolio.id);
    return portfolio.id;
  }

  function binanceSyncPayload() {
    const payload = {
      products: getProducts(),
      spot_symbols: splitValues(byId('spotSymbols').value),
      usdm_symbols: splitValues(byId('usdmSymbols').value),
      coinm_pairs: splitValues(byId('coinmPairs').value),
    };
    const historyStart = isoOrNull(byId('binanceHistoryStart').value);
    if (historyStart) payload.history_start = historyStart;
    return payload;
  }

  function bybitSyncPayload() {
    const payload = {
      products: getProducts('bybit'),
      spot_symbols: splitValues(byId('bybitSpotSymbols').value),
    };
    const linearSettleCoins = splitValues(byId('bybitLinearCoins').value);
    const inverseSettleCoins = splitValues(byId('bybitInverseCoins').value);
    if (linearSettleCoins.length) payload.linear_settle_coins = linearSettleCoins;
    if (inverseSettleCoins.length) payload.inverse_settle_coins = inverseSettleCoins;
    const historyStart = isoOrNull(byId('bybitHistoryStart').value);
    if (historyStart) payload.history_start = historyStart;
    return payload;
  }

  function bitgetSyncPayload() {
    const payload = { products: getProducts('bitget'), spot_symbols: splitValues(byId('bitgetSpotSymbols').value) };
    const historyStart = isoOrNull(byId('bitgetHistoryStart').value);
    if (historyStart) payload.history_start = historyStart;
    return payload;
  }

  function exchangeSyncPayload(provider) {
    return provider === 'binance' ? binanceSyncPayload() : provider === 'bybit' ? bybitSyncPayload() : bitgetSyncPayload();
  }

  function exchangeSyncPath(provider, connectionId) {
    return provider === 'binance'
      ? `/binance/connections/${connectionId}/sync`
      : `/exchanges/${provider}/connections/${connectionId}/sync`;
  }

  async function createAndSync(portfolioId) {
    if (state.source === 'binance') {
      const account = await BagsAuth.api('/accounts', {
        method: 'POST',
        body: JSON.stringify({
          portfolio_id: portfolioId,
          kind: 'exchange',
          provider: 'binance',
          label: byId('binanceLabel').value.trim(),
        }),
      });
      const connection = await BagsAuth.api('/connections', {
        method: 'POST',
        body: JSON.stringify({
          account_id: account.id,
          name: byId('binanceConnectionName').value.trim(),
          provider: 'binance',
          api_key: byId('binanceApiKey').value,
          api_secret: byId('binanceApiSecret').value,
          requested_permissions: ['read'],
        }),
      });
      return BagsAuth.api(`/binance/connections/${connection.id}/sync`, { method: 'POST', body: JSON.stringify(binanceSyncPayload()) });
    }

    if (state.source === 'bybit' || state.source === 'bitget') {
      const provider = state.source;
      const account = await BagsAuth.api('/accounts', {
        method: 'POST',
        body: JSON.stringify({ portfolio_id: portfolioId, kind: 'exchange', provider, label: byId(`${provider}Label`).value.trim() }),
      });
      const connectionPayload = {
        account_id: account.id,
        name: byId(`${provider}ConnectionName`).value.trim(),
        provider,
        api_key: byId(`${provider}ApiKey`).value,
        api_secret: byId(`${provider}ApiSecret`).value,
        requested_permissions: ['read'],
      };
      if (provider === 'bitget') connectionPayload.passphrase = byId('bitgetPassphrase').value;
      const connection = await BagsAuth.api('/connections', { method: 'POST', body: JSON.stringify(connectionPayload) });
      return BagsAuth.api(exchangeSyncPath(provider, connection.id), { method: 'POST', body: JSON.stringify(exchangeSyncPayload(provider)) });
    }

    if (state.source === 'hyperliquid') {
      const address = byId('hyperliquidAddress').value.trim().toLowerCase();
      const account = await BagsAuth.api('/accounts', {
        method: 'POST',
        body: JSON.stringify({
          portfolio_id: portfolioId,
          kind: 'perp_dex',
          provider: 'hyperliquid',
          label: byId('hyperliquidLabel').value.trim(),
          external_account_id: address,
          address,
        }),
      });
      const connection = await BagsAuth.api('/connections', {
        method: 'POST',
        body: JSON.stringify({
          account_id: account.id,
          name: `${byId('hyperliquidLabel').value.trim()} Public`,
          provider: 'hyperliquid',
          api_key: address,
          requested_permissions: ['read'],
        }),
      });
      const payload = { include_spot: byId('hyperliquidSpot').checked };
      const historyStart = isoOrNull(byId('hyperliquidHistoryStart').value);
      if (historyStart) payload.history_start = historyStart;
      return BagsAuth.api(`/perp-dex/connections/${connection.id}/sync`, { method: 'POST', body: JSON.stringify(payload) });
    }

    const account = await BagsAuth.api('/accounts', {
      method: 'POST',
      body: JSON.stringify({
        portfolio_id: portfolioId,
        kind: 'wallet',
        provider: 'evm',
        label: byId('evmLabel').value.trim(),
        chain_id: byId('evmChain').value,
        address: byId('evmAddress').value.trim().toLowerCase(),
      }),
    });
    const payload = {
      token_contracts: splitValues(byId('evmContracts').value),
      transaction_hashes: splitValues(byId('evmTransactions').value),
    };
    if (byId('evmFromBlock').value) payload.from_block = Number(byId('evmFromBlock').value);
    if (byId('evmToBlock').value) payload.to_block = Number(byId('evmToBlock').value);
    return BagsAuth.api(`/evm/accounts/${account.id}/sync`, { method: 'POST', body: JSON.stringify(payload) });
  }

  async function updateAndSync() {
    const connection = state.connections.find((item) => item.id === state.editConnectionId);
    if (!connection) throw new Error('找不到需要更新的连接，请刷新页面后重试。');
    const provider = connection.provider;
    const update = {
      name: byId(`${provider}ConnectionName`).value.trim(),
      api_key: byId(`${provider}ApiKey`).value,
      api_secret: byId(`${provider}ApiSecret`).value,
      is_enabled: true,
    };
    if (provider === 'bitget') update.passphrase = byId('bitgetPassphrase').value;
    await BagsAuth.api(`/connections/${connection.id}`, {
      method: 'PATCH',
      body: JSON.stringify(update),
    });
    return BagsAuth.api(exchangeSyncPath(provider, connection.id), { method: 'POST', body: JSON.stringify(exchangeSyncPayload(provider)) });
  }

  function statLabel(key) {
    return {
      raw_created: '新增原始事件', raw_existing: '已存在事件', ledger_created: '新增账本事件',
      balances_created: '余额快照', positions_created: '仓位快照', equity_created: '权益快照', transfers_created: '转账事件',
      token_balances_created: '代币余额', transactions_scanned: '扫描交易', blocks_scanned: '扫描区块',
    }[key] || key.replaceAll('_', ' ');
  }

  function renderResult(run, fallbackError = '') {
    const status = run?.status || 'failed';
    const failed = status === 'failed';
    const partial = status === 'partial';
    const title = failed ? '连接已保存，但同步失败' : partial ? '同步完成，存在覆盖缺口' : '首次同步完成';
    const message = run?.error_message || fallbackError || (partial ? '部分来源未完成，原始失败范围已保留，可稍后重试。' : '真实数据已经写入不可变原始事件和标准化账本。');
    const stats = Object.entries(run?.stats_json || {}).slice(0, 8);
    const warnings = [...(run?.warnings_json || []), ...(run?.failed_ranges_json?.length ? [`${run.failed_ranges_json.length} 个链上区块范围失败`] : [])];
    byId('syncResult').innerHTML = `
      <div class="result-hero ${failed ? 'failed' : partial ? 'partial' : ''}">
        <span class="result-icon" aria-hidden="true">${failed ? '!' : partial ? '△' : '✓'}</span>
        <div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(message)}</p></div>
      </div>
      ${stats.length ? `<div class="result-stats">${stats.map(([key, value]) => `<div class="result-stat"><small>${escapeHtml(statLabel(key))}</small><b>${escapeHtml(typeof value === 'object' ? JSON.stringify(value) : value)}</b></div>`).join('')}</div>` : ''}
      ${warnings.length ? `<ul class="warnings">${warnings.map((warning) => `<li>${escapeHtml(typeof warning === 'string' ? warning : JSON.stringify(warning))}</li>`).join('')}</ul>` : ''}`;
    setStep(4);
  }

  function clearSensitiveInputs() {
    ['verifyPassword', 'verifyTotp', 'binanceApiKey', 'binanceApiSecret', 'bybitApiKey', 'bybitApiSecret', 'bitgetApiKey', 'bitgetApiSecret', 'bitgetPassphrase'].forEach((id) => { byId(id).value = ''; });
  }

  async function submitConnection() {
    const password = byId('verifyPassword').value;
    const totp = byId('verifyTotp').value.trim();
    if (!password || (state.user?.two_factor_enabled && !/^\d{6}$/.test(totp))) {
      setMessage('verifyMessage', state.user?.two_factor_enabled ? '请输入当前密码和六位 TOTP 验证码。' : '请输入当前登录密码。');
      return;
    }

    let stage = 'verify';
    setBusy(true, '正在验证身份…');
    setMessage('verifyMessage', '');
    try {
      await BagsAuth.api('/auth/sensitive/verify', {
        method: 'POST',
        body: JSON.stringify({ current_password: password, totp_code: totp || null }),
      });
      stage = 'sync';
      setBusy(true, state.mode === 'update' ? '正在更新并同步…' : '正在创建并同步…');
      const run = state.mode === 'update'
        ? await updateAndSync()
        : await createAndSync(await ensurePortfolio());
      clearSensitiveInputs();
      renderResult(run);
      await refreshData();
      dispatchEvent(new CustomEvent('bags:data-changed'));
    } catch (error) {
      clearSensitiveInputs();
      if (stage === 'verify') {
        setMessage('verifyMessage', error.message);
      } else {
        renderResult(null, error.message);
        await refreshData().catch(() => {});
      }
    } finally {
      setBusy(false);
    }
  }

  function formatDate(value) {
    if (!value) return '尚未同步';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false });
  }

  async function latestRun(account, connection) {
    try {
      let path;
      if (account.provider === 'binance' && connection) path = `/binance/connections/${connection.id}/sync-runs?limit=1`;
      else if (['bybit', 'bitget'].includes(account.provider) && connection) path = `/exchanges/${account.provider}/connections/${connection.id}/sync-runs?limit=1`;
      else if (account.provider === 'hyperliquid' && connection) path = `/perp-dex/connections/${connection.id}/sync-runs?limit=1`;
      else if (account.provider === 'evm') path = `/evm/accounts/${account.id}/sync-runs?limit=1`;
      else return null;
      return (await BagsAuth.api(path))[0] || null;
    } catch (_) {
      return null;
    }
  }

  function providerMark(provider) {
    if (provider === 'binance') return ['BN', 'binance'];
    if (provider === 'bybit') return ['BY', 'bybit'];
    if (provider === 'bitget') return ['BG', 'bitget'];
    return provider === 'hyperliquid' ? ['HL', 'hyperliquid'] : ['0x', 'evm'];
  }

  async function renderConnections() {
    const container = byId('connectionsList');
    const manageable = state.accounts.filter((account) => account.provider === 'evm' || state.connections.some((connection) => connection.account_id === account.id));
    if (!manageable.length) {
      container.innerHTML = '<div class="empty-row">还没有可同步的账户。完成上方连接向导后会显示在这里。</div>';
      return;
    }
    container.innerHTML = '<div class="loading-row">正在读取最近同步状态…</div>';
    const rows = await Promise.all(manageable.map(async (account) => {
      const connection = state.connections.find((item) => item.account_id === account.id);
      const run = await latestRun(account, connection);
      return { account, connection, run };
    }));
    container.innerHTML = rows.map(({ account, connection, run }) => {
      const [mark, markClass] = providerMark(account.provider);
      const status = run?.status || 'never';
      const statusText = { succeeded: '同步成功', partial: '部分完成', failed: '同步失败', running: '同步中', never: '尚未同步' }[status];
      const detail = run?.error_message || (run?.warnings_json?.[0] ? String(run.warnings_json[0]) : `${account.chain_id ? `Chain ${account.chain_id} · ` : ''}${connection?.is_enabled === false ? '连接已停用' : '只读连接'}`);
      return `<article class="connection-row" data-account-row="${account.id}">
        <span class="source-mark ${markClass}">${mark}</span>
        <div class="connection-main"><b>${escapeHtml(account.label)}</b><small>${escapeHtml(account.provider)} · ${escapeHtml(account.kind)}</small></div>
        <div class="connection-status"><span class="sync-state ${escapeHtml(status)}">${escapeHtml(statusText)}</span><small>${escapeHtml(formatDate(run?.finished_at || run?.started_at))}</small></div>
        <div class="connection-meta"><b>${escapeHtml(connection?.name || '公开链上账户')}</b><small>${escapeHtml(detail)}</small></div>
        <div class="row-actions">
          ${['binance', 'bybit', 'bitget'].includes(account.provider) && connection ? `<button class="row-action" data-rotate="${connection.id}" type="button">更新密钥</button>` : ''}
          <button class="row-action primary" data-resync="${account.id}" type="button">重新同步</button>
        </div>
      </article>`;
    }).join('');
  }

  async function refreshData() {
    const [user, portfolios, accounts, connections, chains] = await Promise.all([
      BagsAuth.api('/auth/me'), BagsAuth.api('/portfolios'), BagsAuth.api('/accounts'), BagsAuth.api('/connections'), BagsAuth.api('/evm/chains'),
    ]);
    state.user = user;
    state.portfolios = portfolios;
    state.accounts = accounts;
    state.connections = connections;
    state.chains = chains;
    populatePortfolios();
    populateChains();
    await renderConnections();
    const configured = chains.filter((chain) => chain.configured).length;
    setSystemState(`API 正常 · ${accounts.length} 个账户 · ${configured} 条 EVM 网络可用`, 'ready');
  }

  async function resyncAccount(accountId, button) {
    const account = state.accounts.find((item) => item.id === accountId);
    const connection = state.connections.find((item) => item.account_id === accountId);
    if (!account) return;
    button.disabled = true;
    button.textContent = '同步中…';
    setSystemState(`${account.label} 正在同步…`);
    try {
      let run;
      if (account.provider === 'binance' && connection) {
        run = await BagsAuth.api(`/binance/connections/${connection.id}/sync`, { method: 'POST', body: JSON.stringify({ products: ['spot', 'usdm', 'coinm'] }) });
      } else if (account.provider === 'bybit' && connection) {
        run = await BagsAuth.api(exchangeSyncPath('bybit', connection.id), { method: 'POST', body: JSON.stringify({ products: ['spot', 'linear', 'inverse'] }) });
      } else if (account.provider === 'bitget' && connection) {
        run = await BagsAuth.api(exchangeSyncPath('bitget', connection.id), { method: 'POST', body: JSON.stringify({ products: ['spot', 'usdt-futures', 'usdc-futures', 'coin-futures'] }) });
      } else if (account.provider === 'hyperliquid' && connection) {
        run = await BagsAuth.api(`/perp-dex/connections/${connection.id}/sync`, { method: 'POST', body: JSON.stringify({ include_spot: true }) });
      } else if (account.provider === 'evm') {
        run = await BagsAuth.api(`/evm/accounts/${account.id}/sync`, { method: 'POST', body: '{}' });
      } else {
        throw new Error('这个账户缺少可用的只读连接。');
      }
      await refreshData();
      setSystemState(run.status === 'failed' ? `${account.label} 同步失败，请查看状态详情` : `${account.label} 同步完成`, run.status === 'failed' ? 'error' : 'ready');
      dispatchEvent(new CustomEvent('bags:data-changed'));
    } catch (error) {
      setSystemState(`${account.label}：${error.message}`, 'error');
    } finally {
      button.disabled = false;
      button.textContent = '重新同步';
    }
  }

  function startCredentialRotation(connectionId) {
    const connection = state.connections.find((item) => item.id === connectionId);
    const account = state.accounts.find((item) => item.id === connection?.account_id);
    if (!connection || !account || !['binance', 'bybit', 'bitget'].includes(connection.provider)) return;
    state.mode = 'update';
    state.editConnectionId = connectionId;
    state.source = connection.provider;
    document.querySelector(`input[name="source"][value="${connection.provider}"]`).checked = true;
    byId(`${connection.provider}Label`).value = account.label;
    byId(`${connection.provider}ConnectionName`).value = connection.name;
    byId(`${connection.provider}ApiKey`).value = '';
    byId(`${connection.provider}ApiSecret`).value = '';
    if (connection.provider === 'bitget') byId('bitgetPassphrase').value = '';
    updateSourceForms();
    setMessage('detailsMessage', `请输入新的 ${sourceName()} 只读凭据。旧值不会显示。`, true);
    setStep(2);
  }

  function resetWizard() {
    state.mode = 'create';
    state.editConnectionId = null;
    state.source = 'binance';
    document.querySelector('input[name="source"][value="binance"]').checked = true;
    ['binanceLabel', 'bybitLabel', 'bitgetLabel'].forEach((id) => { byId(id).disabled = false; });
    byId('detailsForm').reset();
    document.querySelectorAll('input[name="binanceProduct"]').forEach((input) => { input.checked = true; });
    document.querySelectorAll('input[name="bybitProduct"], input[name="bitgetProduct"]').forEach((input) => { input.checked = true; });
    byId('hyperliquidSpot').checked = true;
    setMessage('detailsMessage', '');
    setMessage('verifyMessage', '');
    updateSourceForms();
    setStep(1);
  }

  document.addEventListener('DOMContentLoaded', async () => {
    document.querySelectorAll('input[name="source"]').forEach((input) => input.addEventListener('change', () => {
      state.source = input.value;
      updateSourceForms();
    }));
    byId('portfolioSelect').addEventListener('change', updatePortfolioMode);
    byId('toDetails').addEventListener('click', () => { if (validatePortfolio()) { updateSourceForms(); setStep(2); } });
    byId('toVerify').addEventListener('click', () => { if (validateDetails()) { showReview(); setStep(3); } });
    byId('connectNow').addEventListener('click', submitConnection);
    byId('connectAnother').addEventListener('click', resetWizard);
    byId('refreshConnections').addEventListener('click', async () => {
      setSystemState('正在刷新连接状态…');
      try { await refreshData(); } catch (error) { setSystemState(error.message, 'error'); }
    });
    document.querySelectorAll('[data-back]').forEach((button) => button.addEventListener('click', () => setStep(Number(button.dataset.back))));
    byId('connectionsList').addEventListener('click', (event) => {
      const sync = event.target.closest('[data-resync]');
      if (sync) resyncAccount(sync.dataset.resync, sync);
      const rotate = event.target.closest('[data-rotate]');
      if (rotate) startCredentialRotation(rotate.dataset.rotate);
    });
    updateSourceForms();
    try {
      await refreshData();
    } catch (error) {
      setSystemState(`连接能力检查失败：${error.message}`, 'error');
      byId('connectionsList').innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`;
    }
  });
})();
