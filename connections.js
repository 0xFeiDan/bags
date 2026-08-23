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
    zerionStatus: null,
    zerionSources: new Map(),
    zerionRuns: new Map(),
    zerionPendingAction: null,
    zerionBusy: false,
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

  function selectedEvmChains() {
    const selected = new Set(
      [...document.querySelectorAll('input[name="evmChain"]:checked')].map((input) => input.value),
    );
    return state.chains.filter((chain) => chain.configured && selected.has(chain.key));
  }

  function populateChains() {
    const configured = state.chains.filter((chain) => chain.configured);
    const previous = new Set(selectedEvmChains().map((chain) => chain.key));
    if (!previous.size && configured.length) previous.add(configured[0].key);
    byId('evmChains').innerHTML = configured.length
      ? configured.map((chain) => `<label class="chain-option"><input type="checkbox" name="evmChain" value="${escapeHtml(chain.key)}" ${previous.has(chain.key) ? 'checked' : ''} /><span><b>${escapeHtml(chain.name)}</b><small>Chain ${escapeHtml(chain.chain_id)} · ${escapeHtml(chain.native_symbol)}</small></span></label>`).join('')
      : '<div class="empty-row">服务器尚未配置 EVM RPC</div>';
    const source = byId('evmSourceOption');
    source.classList.toggle('unavailable', !configured.length);
    source.querySelector('input').disabled = !configured.length;
    byId('evmSourceStatus').textContent = configured.length ? `${configured.length} 条网络可用` : 'RPC 未配置';
    byId('evmChainHelp').textContent = configured.length
      ? '勾选需要读取的网络；每条链会建立独立账户并依次同步。'
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
    const chains = selectedEvmChains();
    return { label: byId('evmLabel').value.trim(), scope: chains.map((chain) => chain.name).join(' · ') || '未选择网络' };
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
      ['操作', state.mode === 'update'
        ? '替换凭据并同步'
        : state.source === 'evm' && selectedEvmChains().length > 1
          ? `创建 ${selectedEvmChains().length} 个链账户并依次同步`
          : '创建并首次同步'],
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
    if (state.source === 'evm') {
      const chains = selectedEvmChains();
      if (!chains.length) {
        setMessage('detailsMessage', '至少选择一条已经配置 RPC 的 EVM 网络。');
        byId('evmChains').querySelector('input')?.focus();
        return false;
      }
      const from = byId('evmFromBlock').value;
      const to = byId('evmToBlock').value;
      if (from && to && Number(from) > Number(to)) {
        setMessage('detailsMessage', '起始区块不能大于结束区块。');
        return false;
      }
      const hasBoundedBackfill = Boolean(
        from || to || splitValues(byId('evmContracts').value).length || splitValues(byId('evmTransactions').value).length,
      );
      if (chains.length > 1 && hasBoundedBackfill) {
        setMessage('detailsMessage', '多链同步不能共用同一组区块、合约或交易哈希。请先留空边界回填；需要精确回填时再逐链操作。');
        return false;
      }
      const address = byId('evmAddress').value.trim().toLowerCase();
      const portfolioId = selectedPortfolio();
      if (portfolioId !== '__new__') {
        const duplicates = chains.filter((chain) => state.accounts.some((account) => (
          account.portfolio_id === portfolioId
          && account.provider === 'evm'
          && account.chain_id === chain.chain_id
          && String(account.address || '').toLowerCase() === address
        )));
        if (duplicates.length) {
          setMessage('detailsMessage', `该地址已连接：${duplicates.map((chain) => chain.name).join('、')}。请取消这些网络后继续。`);
          return false;
        }
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

  function evmSyncPayload() {
    const payload = {
      token_contracts: splitValues(byId('evmContracts').value),
      transaction_hashes: splitValues(byId('evmTransactions').value),
    };
    if (byId('evmFromBlock').value) payload.from_block = Number(byId('evmFromBlock').value);
    if (byId('evmToBlock').value) payload.to_block = Number(byId('evmToBlock').value);
    return payload;
  }

  function evmAccountLabel(baseLabel, chain, chainCount) {
    if (chainCount === 1) return baseLabel;
    const suffix = ` · ${chain.name}`;
    return `${baseLabel.slice(0, Math.max(1, 120 - suffix.length))}${suffix}`;
  }

  function aggregateEvmRuns(results) {
    const failed = results.filter((result) => result.error || result.run?.status === 'failed');
    const partial = results.filter((result) => result.run?.status === 'partial');
    const succeeded = results.length - failed.length;
    const stats = {
      networks_requested: results.length,
      networks_succeeded: succeeded,
      networks_failed: failed.length,
    };
    results.forEach(({ run }) => {
      Object.entries(run?.stats_json || {}).forEach(([key, value]) => {
        if (typeof value === 'number') stats[key] = (stats[key] || 0) + value;
      });
    });
    const warnings = results.flatMap(({ chain, run, error }) => {
      const messages = [
        ...(run?.warnings_json || []),
        ...(run?.failed_ranges_json?.length ? [`${run.failed_ranges_json.length} 个区块范围失败`] : []),
      ];
      if (error || run?.error_message) messages.unshift(error?.message || run.error_message);
      return messages.map((message) => `${chain.name}：${typeof message === 'string' ? message : JSON.stringify(message)}`);
    });
    return {
      status: failed.length === results.length ? 'failed' : (failed.length || partial.length ? 'partial' : 'succeeded'),
      stats_json: stats,
      warnings_json: warnings,
      error_message: failed.length === results.length ? '所选网络均未同步成功，请查看各网络错误。' : null,
      summary_message: `${succeeded}/${results.length} 条网络已完成首次同步。每条链均保存为独立只读账户。`,
      multichain: results.length > 1,
    };
  }

  async function createEvmAccountsAndSync(portfolioId) {
    const chains = selectedEvmChains();
    const address = byId('evmAddress').value.trim().toLowerCase();
    const baseLabel = byId('evmLabel').value.trim();
    const payload = evmSyncPayload();
    const results = [];
    for (const chain of chains) {
      try {
        const account = await BagsAuth.api('/accounts', {
          method: 'POST',
          body: JSON.stringify({
            portfolio_id: portfolioId,
            kind: 'wallet',
            provider: 'evm',
            label: evmAccountLabel(baseLabel, chain, chains.length),
            chain_id: chain.key,
            address,
          }),
        });
        const run = await BagsAuth.api(`/evm/accounts/${account.id}/sync`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        results.push({ chain, run });
      } catch (error) {
        results.push({ chain, error });
      }
    }
    return aggregateEvmRuns(results);
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

    return createEvmAccountsAndSync(portfolioId);
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
      dashboard_snapshot_created: '仪表盘快照',
      prices_created: '市场价格',
      networks_requested: '选择网络', networks_succeeded: '同步成功网络', networks_failed: '同步失败网络',
    }[key] || key.replaceAll('_', ' ');
  }

  async function refreshPortfolioSnapshot(portfolioId, run) {
    if (!run || run.status === 'failed') return run;
    try {
      await BagsAuth.api(`/dashboard/portfolios/${portfolioId}/snapshots`, {
        method: 'POST',
        body: '{}',
      });
      run.stats_json = { ...(run.stats_json || {}), dashboard_snapshot_created: 1 };
    } catch (error) {
      run.status = 'partial';
      run.warnings_json = [
        ...(run.warnings_json || []),
        `真实数据已同步，但仪表盘快照生成失败：${error.message}`,
      ];
    }
    return run;
  }

  function renderResult(run, fallbackError = '') {
    const status = run?.status || 'failed';
    const failed = status === 'failed';
    const partial = status === 'partial';
    const title = run?.multichain
      ? (failed ? '多链连接失败' : partial ? '多链同步部分完成' : '多链首次同步完成')
      : (failed ? '连接已保存，但同步失败' : partial ? '同步完成，存在覆盖缺口' : '首次同步完成');
    const message = run?.error_message || fallbackError || run?.summary_message || (partial ? '部分来源未完成，原始失败范围已保留，可稍后重试。' : '真实数据已经写入不可变原始事件和标准化账本。');
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
      const evmChainCount = state.source === 'evm' ? selectedEvmChains().length : 0;
      setBusy(
        true,
        state.mode === 'update'
          ? '正在更新并同步…'
          : evmChainCount > 1
            ? `正在依次同步 ${evmChainCount} 条网络…`
            : '正在创建并同步…',
      );
      let portfolioId;
      let run;
      if (state.mode === 'update') {
        const connection = state.connections.find((item) => item.id === state.editConnectionId);
        const account = state.accounts.find((item) => item.id === connection?.account_id);
        if (!account) throw new Error('找不到连接所属的 Portfolio，请刷新页面后重试。');
        portfolioId = account.portfolio_id;
        run = await updateAndSync();
      } else {
        portfolioId = await ensurePortfolio();
        run = await createAndSync(portfolioId);
      }
      await refreshPortfolioSnapshot(portfolioId, run);
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

  function compactAddress(value) {
    const address = String(value || '');
    return address.length > 18 ? `${address.slice(0, 8)}…${address.slice(-6)}` : address || '地址未知';
  }

  function zerionEvmAccounts() {
    return state.accounts.filter((account) => account.provider === 'evm');
  }

  function setZerionMessage(message, kind = '') {
    const node = byId('zerionMessage');
    node.hidden = !message;
    node.textContent = message || '';
    node.className = `zerion-message ${kind}`.trim();
  }

  function zerionRunStatus(run) {
    if (!run) return ['never', '尚未同步'];
    return [run.status || 'never', {
      succeeded: 'Shadow 同步成功',
      partial: 'Shadow 部分完成',
      failed: 'Shadow 同步失败',
      running: 'Shadow 同步中',
    }[run.status] || '状态未知'];
  }

  function remainingCooldownMinutes(value) {
    const timestamp = Date.parse(value || '');
    if (!Number.isFinite(timestamp)) return 0;
    return Math.max(0, Math.ceil((timestamp - Date.now()) / 60000));
  }

  function renderZerionPanel() {
    const status = state.zerionStatus;
    const configured = Boolean(status?.configured);
    const badge = byId('zerionStatusBadge');
    badge.className = `zerion-badge ${configured ? 'configured' : 'unconfigured'}`;
    badge.textContent = configured ? '服务器已配置' : '服务器未配置';

    byId('zerionOverview').innerHTML = status ? `
      <div class="zerion-metric"><small>请求频率</small><b>${escapeHtml(status.requests_per_second_limit)} 次 / 秒</b></div>
      <div class="zerion-metric"><small>每日预算</small><b>${escapeHtml(status.daily_request_budget)} / ${escapeHtml(status.daily_request_limit)}</b></div>
      <div class="zerion-metric"><small>单次同步上限</small><b>${escapeHtml(status.max_requests_per_run)} 次请求</b></div>
      <div class="zerion-metric"><small>账户同步间隔</small><b>至少 ${escapeHtml(Math.ceil(status.min_sync_interval_seconds / 60))} 分钟</b></div>
    ` : '<div class="empty-row">暂时无法读取 Zerion 服务状态。</div>';

    const accounts = zerionEvmAccounts();
    if (!accounts.length) {
      byId('zerionAccounts').innerHTML = '<div class="empty-row">还没有 EVM 地址。请先通过上方连接向导添加公开钱包地址。</div>';
      return;
    }

    byId('zerionAccounts').innerHTML = accounts.map((account) => {
      const source = state.zerionSources.get(account.id) || null;
      const run = state.zerionRuns.get(account.id) || null;
      const enabled = Boolean(source?.is_enabled);
      const cooldownMinutes = remainingCooldownMinutes(source?.next_sync_after);
      const [runStatus, runStatusText] = zerionRunStatus(run);
      const stats = run?.stats_json || {};
      const rawCount = Number(stats.raw_created || 0) + Number(stats.raw_existing || 0);
      const runDetail = run
        ? `${Number(stats.request_count || run.request_count || 0)} 请求 · ${rawCount} Raw · Ledger ${Number(stats.ledger_created || 0)}`
        : '尚无 Zerion Shadow 运行记录';
      const sourceTitle = enabled ? 'Shadow 已启用' : source ? 'Shadow 已停用' : '尚未配置';
      const sourceDetail = enabled
        ? `仅写 RawEvent${cooldownMinutes ? ` · 冷却 ${cooldownMinutes} 分钟` : ''}`
        : configured ? '可启用为影子数据源' : '需要服务器环境变量';
      const syncDisabled = state.zerionBusy || !configured || !enabled || cooldownMinutes > 0;
      const toggleDisabled = state.zerionBusy || (!configured && !enabled);
      return `<article class="zerion-row" data-zerion-account="${escapeHtml(account.id)}">
        <span class="source-mark zerion" aria-hidden="true">ZR</span>
        <div class="zerion-account-main"><b>${escapeHtml(account.label)}</b><small>${escapeHtml(account.chain_id ? `Chain ${account.chain_id} · ` : '')}${escapeHtml(compactAddress(account.address))}</small></div>
        <div class="zerion-source-state"><b>${escapeHtml(sourceTitle)}</b><small>${escapeHtml(sourceDetail)}</small></div>
        <div class="zerion-run-state"><b class="${escapeHtml(runStatus)}">${escapeHtml(runStatusText)}</b><small>${escapeHtml(runDetail)} · ${escapeHtml(formatDate(run?.finished_at || run?.started_at))}</small></div>
        <div class="zerion-actions">
          <button class="row-action" data-zerion-action="${enabled ? 'disable' : 'enable'}" data-account-id="${escapeHtml(account.id)}" type="button" ${toggleDisabled ? 'disabled' : ''}>${enabled ? '停用' : '启用'}</button>
          <button class="row-action primary" data-zerion-action="sync" data-account-id="${escapeHtml(account.id)}" type="button" ${syncDisabled ? 'disabled' : ''}>${cooldownMinutes ? `${cooldownMinutes} 分钟后` : 'Shadow 同步'}</button>
        </div>
      </article>`;
    }).join('');
  }

  async function refreshZerionData() {
    try {
      state.zerionStatus = await BagsAuth.api('/zerion/status');
      const rows = await Promise.all(zerionEvmAccounts().map(async (account) => {
        let source = null;
        let run = null;
        try {
          source = await BagsAuth.api(`/zerion/accounts/${account.id}/source`);
        } catch (error) {
          if (error.status !== 404) throw error;
        }
        try {
          run = (await BagsAuth.api(`/zerion/accounts/${account.id}/sync-runs?limit=1`))[0] || null;
        } catch (error) {
          if (error.status !== 404) throw error;
        }
        return { accountId: account.id, source, run };
      }));
      state.zerionSources = new Map(rows.map((row) => [row.accountId, row.source]));
      state.zerionRuns = new Map(rows.map((row) => [row.accountId, row.run]));
      renderZerionPanel();
    } catch (error) {
      state.zerionStatus = null;
      state.zerionSources = new Map();
      state.zerionRuns = new Map();
      renderZerionPanel();
      setZerionMessage(`Zerion 状态读取失败：${error.message}`, 'error');
    }
  }

  function closeZerionAuth() {
    state.zerionPendingAction = null;
    byId('zerionAuthPanel').hidden = true;
    byId('zerionVerifyPassword').value = '';
    byId('zerionVerifyTotp').value = '';
    setMessage('zerionAuthMessage', '');
  }

  function openZerionAuth(type, accountId) {
    if (state.zerionBusy) return;
    const account = state.accounts.find((item) => item.id === accountId && item.provider === 'evm');
    const source = state.zerionSources.get(accountId);
    if (!account) return;
    if (type === 'sync' && !source?.is_enabled) {
      setZerionMessage('请先启用该账户的 Zerion Shadow 数据源。', 'error');
      return;
    }
    const labels = {
      enable: ['启用 Zerion Shadow', `为“${account.label}”启用只写 RawEvent 的影子数据源。`],
      disable: ['停用 Zerion Shadow', `停用“${account.label}”的 Zerion 数据源，不删除已有 RawEvent。`],
      sync: ['运行 Zerion Shadow 同步', `同步“${account.label}”，结果只进入 RawEvent，不写入 Ledger。`],
    };
    const [title, description] = labels[type] || labels.sync;
    state.zerionPendingAction = { type, accountId };
    byId('zerionAuthTitle').textContent = title;
    byId('zerionAuthDescription').textContent = description;
    byId('confirmZerionAction').textContent = { enable: '验证并启用', disable: '验证并停用', sync: '验证并同步' }[type];
    const needsTotp = Boolean(state.user?.two_factor_enabled);
    byId('zerionVerifyTotpField').hidden = !needsTotp;
    byId('zerionAuthPanel').querySelector('.zerion-auth-fields').classList.toggle('with-totp', needsTotp);
    byId('zerionAuthPanel').hidden = false;
    setMessage('zerionAuthMessage', '');
    byId('zerionVerifyPassword').focus({ preventScroll: true });
    byId('zerionAuthPanel').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
  }

  function zerionErrorMessage(error) {
    if (error.status === 429) return '该账户仍在同步冷却期，请等待倒计时结束后重试。';
    if (error.status === 503) return '服务器尚未配置 Zerion。请先在 .env 中填写密钥并启用服务。';
    return error.message;
  }

  async function confirmZerionAction() {
    const pending = state.zerionPendingAction;
    if (!pending || state.zerionBusy) return;
    const password = byId('zerionVerifyPassword').value;
    const totp = byId('zerionVerifyTotp').value.trim();
    if (!password || (state.user?.two_factor_enabled && !/^\d{6}$/.test(totp))) {
      setMessage('zerionAuthMessage', state.user?.two_factor_enabled ? '请输入当前密码和六位 TOTP 验证码。' : '请输入当前登录密码。');
      return;
    }

    state.zerionBusy = true;
    const button = byId('confirmZerionAction');
    button.disabled = true;
    button.textContent = '正在验证…';
    setMessage('zerionAuthMessage', '');
    renderZerionPanel();
    try {
      await BagsAuth.api('/auth/sensitive/verify', {
        method: 'POST',
        body: JSON.stringify({ current_password: password, totp_code: totp || null }),
      });
      button.textContent = pending.type === 'sync' ? '正在同步…' : '正在保存…';
      let result = null;
      if (pending.type === 'sync') {
        result = await BagsAuth.api(`/zerion/accounts/${pending.accountId}/shadow-sync`, { method: 'POST', body: '{}' });
      } else {
        result = await BagsAuth.api(`/zerion/accounts/${pending.accountId}/source`, {
          method: 'PUT',
          body: JSON.stringify({ is_enabled: pending.type === 'enable', mode: pending.type === 'enable' ? 'shadow' : 'disabled' }),
        });
      }
      closeZerionAuth();
      state.zerionBusy = false;
      await refreshZerionData();
      if (pending.type === 'sync') {
        const stats = result.stats_json || {};
        const rawCount = Number(stats.raw_created || 0) + Number(stats.raw_existing || 0);
        setZerionMessage(`Shadow 同步${result.status === 'failed' ? '失败' : '完成'}：${Number(stats.request_count || result.request_count || 0)} 次请求，${rawCount} 条 RawEvent，Ledger 新增 ${Number(stats.ledger_created || 0)}。`, result.status === 'failed' ? 'error' : 'success');
      } else {
        setZerionMessage(pending.type === 'enable' ? 'Zerion Shadow 已启用。现在可以手动运行首次同步。' : 'Zerion Shadow 已停用，历史 RawEvent 已保留。', 'success');
      }
      dispatchEvent(new CustomEvent('bags:data-changed'));
    } catch (error) {
      setMessage('zerionAuthMessage', zerionErrorMessage(error));
    } finally {
      state.zerionBusy = false;
      button.disabled = false;
      if (state.zerionPendingAction) button.textContent = { enable: '验证并启用', disable: '验证并停用', sync: '验证并同步' }[state.zerionPendingAction.type];
      renderZerionPanel();
      byId('zerionVerifyPassword').value = '';
      byId('zerionVerifyTotp').value = '';
    }
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
    await refreshZerionData();
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
      await refreshPortfolioSnapshot(account.portfolio_id, run);
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
    populateChains();
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
    byId('zerionAccounts').addEventListener('click', (event) => {
      const action = event.target.closest('[data-zerion-action]');
      if (action && !action.disabled) openZerionAuth(action.dataset.zerionAction, action.dataset.accountId);
    });
    byId('cancelZerionAction').addEventListener('click', closeZerionAuth);
    byId('confirmZerionAction').addEventListener('click', confirmZerionAction);
    [byId('zerionVerifyPassword'), byId('zerionVerifyTotp')].forEach((input) => input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        confirmZerionAction();
      }
    }));
    updateSourceForms();
    try {
      await refreshData();
    } catch (error) {
      setSystemState(`连接能力检查失败：${error.message}`, 'error');
      byId('connectionsList').innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`;
    }
  });
})();
