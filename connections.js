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
    managerType: null,
    managerGroupPrimaryId: null,
    managerConnectionId: null,
    managerContracts: [],
    managerSpotScopes: [],
    managerPendingAction: null,
    managerBusy: false,
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

  function normalizedAddress(account) {
    const value = account?.address || (account?.provider === 'hyperliquid' ? account.external_account_id : '');
    return String(value || '').trim().toLowerCase();
  }

  function chainForAccount(account) {
    return state.chains.find((chain) => String(chain.chain_id) === String(account.chain_id));
  }

  function walletGroups() {
    const groups = new Map();
    state.accounts.filter((account) => account.provider === 'evm' && normalizedAddress(account)).forEach((account) => {
      const key = `${account.portfolio_id}|${normalizedAddress(account)}`;
      if (!groups.has(key)) groups.set(key, { key, portfolioId: account.portfolio_id, address: normalizedAddress(account), evmAccounts: [], hyperAccount: null });
      groups.get(key).evmAccounts.push(account);
    });
    state.accounts.filter((account) => account.provider === 'hyperliquid' && normalizedAddress(account)).forEach((account) => {
      const group = groups.get(`${account.portfolio_id}|${normalizedAddress(account)}`);
      if (group) group.hyperAccount = account;
    });
    return [...groups.values()].map((group) => ({
      ...group,
      evmAccounts: group.evmAccounts.sort((left, right) => Number(left.chain_id || 0) - Number(right.chain_id || 0)),
      accounts: [...group.evmAccounts, ...(group.hyperAccount ? [group.hyperAccount] : [])],
    }));
  }

  function walletGroupByPrimary(accountId) {
    return walletGroups().find((group) => group.evmAccounts.some((account) => account.id === accountId)) || null;
  }

  function walletBaseLabel(group) {
    const account = group?.evmAccounts[0] || group?.hyperAccount;
    if (!account) return '链上钱包';
    const suffixes = [...state.chains.map((chain) => chain.name), 'Hyperliquid'].sort((left, right) => right.length - left.length);
    const suffix = suffixes.find((name) => account.label.endsWith(` · ${name}`));
    return suffix ? account.label.slice(0, -(suffix.length + 3)) : account.label;
  }

  function compactAddress(address) {
    return address?.length > 16 ? `${address.slice(0, 8)}…${address.slice(-6)}` : address || '—';
  }

  function aggregateRunStatus(runs) {
    const priority = { failed: 5, partial: 4, running: 3, succeeded: 2, never: 1 };
    return runs.reduce((current, run) => {
      const status = run?.status || 'never';
      return priority[status] > priority[current] ? status : current;
    }, 'never');
  }

  async function renderConnections() {
    const container = byId('connectionsList');
    const groups = walletGroups();
    const attachedHyperIds = new Set(groups.map((group) => group.hyperAccount?.id).filter(Boolean));
    const standalone = state.accounts.filter((account) => account.provider !== 'evm'
      && !attachedHyperIds.has(account.id)
      && state.connections.some((connection) => connection.account_id === account.id));
    const items = [...groups.map((group) => ({ type: 'wallet', group })), ...standalone.map((account) => ({ type: 'account', account }))];
    if (!items.length) {
      container.innerHTML = '<div class="empty-row">还没有可同步的账户。完成上方连接向导后会显示在这里。</div>';
      return;
    }
    container.innerHTML = '<div class="loading-row">正在读取最近同步状态…</div>';
    const rows = await Promise.all(items.map(async (item) => {
      if (item.type === 'wallet') {
        const runs = await Promise.all(item.group.accounts.map((account) => latestRun(account, state.connections.find((connection) => connection.account_id === account.id))));
        return { ...item, runs };
      }
      const connection = state.connections.find((candidate) => candidate.account_id === item.account.id);
      return { ...item, connection, run: await latestRun(item.account, connection) };
    }));
    container.innerHTML = rows.map((row) => {
      if (row.type === 'wallet') {
        const group = row.group;
        const status = aggregateRunStatus(row.runs);
        const statusText = { succeeded: '同步成功', partial: '部分完成', failed: '同步失败', running: '同步中', never: '尚未同步' }[status];
        const latestTime = row.runs.map((run) => run?.finished_at || run?.started_at).filter(Boolean).sort().at(-1);
        const networks = [
          ...group.evmAccounts.map((account) => chainForAccount(account)?.name || `Chain ${account.chain_id}`),
          ...(group.hyperAccount ? ['Hyperliquid'] : []),
        ];
        const inactive = group.accounts.filter((account) => !account.is_active).length;
        const primary = group.evmAccounts[0];
        return `<article class="connection-row" data-account-row="${primary.id}">
          <span class="source-mark evm">0x</span>
          <div class="connection-main"><b>${escapeHtml(walletBaseLabel(group))}</b><small title="${escapeHtml(group.address)}">${escapeHtml(compactAddress(group.address))} · ${group.accounts.length} 个链上范围</small></div>
          <div class="connection-status"><span class="sync-state ${escapeHtml(status)}">${escapeHtml(statusText)}</span><small>${escapeHtml(formatDate(latestTime))}</small></div>
          <div class="connection-meta"><b>${escapeHtml(networks.join(' · '))}</b><small>${inactive ? `${inactive} 个范围已停用；` : ''}同一地址统一管理，历史账本保留</small></div>
          <div class="row-actions"><button class="row-action" data-manage-wallet="${primary.id}" type="button">管理钱包</button><button class="row-action primary" data-resync-wallet="${primary.id}" type="button">全部同步</button></div>
        </article>`;
      }
      const { account, connection, run } = row;
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
          ${account.provider === 'binance' && connection ? `<button class="row-action" data-manage-binance="${connection.id}" type="button">管理交易对</button>` : ''}
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

  async function resyncWalletGroup(primaryAccountId, button) {
    const group = walletGroupByPrimary(primaryAccountId);
    if (!group) return;
    const activeAccounts = group.accounts.filter((account) => account.is_active);
    if (!activeAccounts.length) {
      setSystemState('这个钱包的所有读取范围都已停用。', 'error');
      return;
    }
    button.disabled = true;
    button.textContent = '同步中…';
    setSystemState(`${walletBaseLabel(group)} 正在同步 ${activeAccounts.length} 个链上范围…`);
    const failures = [];
    try {
      for (const account of activeAccounts) {
        try {
          const connection = state.connections.find((item) => item.account_id === account.id);
          const run = account.provider === 'evm'
            ? await BagsAuth.api(`/evm/accounts/${account.id}/sync`, { method: 'POST', body: '{}' })
            : connection
              ? await BagsAuth.api(`/perp-dex/connections/${connection.id}/sync`, { method: 'POST', body: JSON.stringify({ include_spot: true }) })
              : null;
          if (!run) throw new Error('缺少只读连接');
          await refreshPortfolioSnapshot(account.portfolio_id, run);
          if (run.status === 'failed') failures.push(`${account.label}：${run.error_message || '同步失败'}`);
        } catch (error) {
          failures.push(`${account.label}：${error.message}`);
        }
      }
      await refreshData();
      setSystemState(failures.length ? `钱包同步完成，但 ${failures.length} 个范围失败` : `${walletBaseLabel(group)} 全部同步完成`, failures.length ? 'error' : 'ready');
      if (failures.length) setMessage('managerMessage', failures.join('；'));
      dispatchEvent(new CustomEvent('bags:data-changed'));
    } finally {
      button.disabled = false;
      button.textContent = '全部同步';
    }
  }

  function setManagerMessage(message, success = false) {
    setMessage('managerMessage', message, success);
  }

  function setManagerBusy(busy) {
    state.managerBusy = busy;
    const panel = byId('connectionManager');
    panel.classList.toggle('manager-busy', busy);
    panel.querySelectorAll('button, input, select, textarea').forEach((control) => { control.disabled = busy; });
    byId('confirmManagerAuth').textContent = busy ? '正在处理…' : '验证并继续';
  }

  function closeManager() {
    if (state.managerBusy) return;
    byId('connectionManager').hidden = true;
    byId('managerAuth').hidden = true;
    state.managerType = null;
    state.managerGroupPrimaryId = null;
    state.managerConnectionId = null;
    state.managerPendingAction = null;
    byId('managerPassword').value = '';
    byId('managerTotp').value = '';
  }

  function showManagerView(type) {
    document.querySelectorAll('[data-manager-view]').forEach((view) => { view.hidden = view.dataset.managerView !== type; });
    byId('managerAuth').hidden = true;
    byId('connectionManager').hidden = false;
  }

  function renderWalletManager() {
    const group = walletGroupByPrimary(state.managerGroupPrimaryId);
    if (!group) {
      closeManager();
      return null;
    }
    byId('managerTitle').textContent = `管理 ${walletBaseLabel(group)}`;
    byId('managerLead').textContent = '同一地址按链保存为独立账户；可以继续加链、停用读取范围和补充代币合约。';
    byId('managerWalletAddress').textContent = group.address;
    byId('managerPortfolioName').textContent = state.portfolios.find((portfolio) => portfolio.id === group.portfolioId)?.name || '—';
    byId('managerWalletLabel').value = walletBaseLabel(group);
    byId('managerNetworks').innerHTML = group.accounts.map((account) => {
      const connection = state.connections.find((item) => item.account_id === account.id);
      const network = account.provider === 'hyperliquid' ? 'Hyperliquid' : chainForAccount(account)?.name || `Chain ${account.chain_id}`;
      const detail = account.provider === 'evm'
        ? `Chain ${account.chain_id} · ${account.is_active ? '读取中' : '已停用'}`
        : `${connection ? '公开地址连接' : '连接待补建'} · ${account.is_active ? '读取中' : '已停用'}`;
      return `<div class="manager-item"><div><b>${escapeHtml(network)}</b><small>${escapeHtml(detail)}</small></div><button class="row-action ${account.is_active ? '' : 'primary'}" data-toggle-account="${account.id}" data-next-active="${account.is_active ? 'false' : 'true'}" type="button">${account.is_active ? '停用' : '重新启用'}</button></div>`;
    }).join('');

    const existingChainIds = new Set(group.evmAccounts.map((account) => String(account.chain_id)));
    const available = state.chains.filter((chain) => chain.configured && !existingChainIds.has(String(chain.chain_id)));
    byId('managerAvailableChains').innerHTML = `<legend>新增 EVM 网络</legend>${available.length
      ? `<div class="manager-choice-grid">${available.map((chain) => `<label><input type="checkbox" name="managerChain" value="${escapeHtml(chain.key)}" /><span>${escapeHtml(chain.name)}</span></label>`).join('')}</div>`
      : '<div class="empty-row">已添加服务器当前配置的全部 EVM 网络。</div>'}`;
    byId('addWalletChains').hidden = !available.length;
    const hyperConnection = group.hyperAccount && state.connections.find((item) => item.account_id === group.hyperAccount.id);
    byId('addHyperliquidNetwork').hidden = Boolean(group.hyperAccount && hyperConnection);
    byId('addHyperliquidNetwork').textContent = group.hyperAccount ? '补建 Hyperliquid 连接' : '添加 Hyperliquid';

    const select = byId('managerContractChain');
    const previous = select.value;
    select.innerHTML = group.evmAccounts.map((account) => `<option value="${account.id}">${escapeHtml(chainForAccount(account)?.name || `Chain ${account.chain_id}`)}${account.is_active ? '' : '（已停用）'}</option>`).join('');
    if (group.evmAccounts.some((account) => account.id === previous)) select.value = previous;
    return group;
  }

  async function loadTrackedContracts() {
    const accountId = byId('managerContractChain').value;
    const container = byId('managerContracts');
    if (!accountId) {
      state.managerContracts = [];
      container.innerHTML = '<div class="empty-row">没有可管理的 EVM 网络。</div>';
      return;
    }
    container.innerHTML = '<div class="loading-row">正在读取合约…</div>';
    try {
      state.managerContracts = await BagsAuth.api(`/evm/accounts/${accountId}/tracked-contracts`);
      container.innerHTML = state.managerContracts.length ? state.managerContracts.map((contract) => `<div class="manager-item"><div><b>${escapeHtml(contract.label || compactAddress(contract.contract_address))}</b><small>${escapeHtml(contract.contract_address)} · ${contract.is_active ? '持续跟踪' : '已停用'}</small></div><button class="row-action ${contract.is_active ? '' : 'primary'}" data-toggle-contract="${contract.id}" data-next-active="${contract.is_active ? 'false' : 'true'}" type="button">${contract.is_active ? '停用' : '重新启用'}</button></div>`).join('') : '<div class="empty-row">这条链还没有手动跟踪的代币合约。</div>';
    } catch (error) {
      container.innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`;
    }
  }

  async function openWalletManager(primaryAccountId, scroll = true) {
    state.managerType = 'evm';
    state.managerGroupPrimaryId = primaryAccountId;
    state.managerConnectionId = null;
    showManagerView('evm');
    setManagerMessage('');
    if (!renderWalletManager()) return;
    await loadTrackedContracts();
    if (scroll) byId('connectionManager').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  }

  function renderSpotScopes() {
    const container = byId('managerSpotSymbols');
    const sourceLabels = { balance: '当前持仓发现', manual: '手动添加', existing: '已有成交恢复' };
    container.innerHTML = state.managerSpotScopes.length ? state.managerSpotScopes.map((scope) => `<div class="manager-item"><div><b>${escapeHtml(scope.symbol)}</b><small>${escapeHtml(sourceLabels[scope.discovery_source] || scope.discovery_source)} · ${scope.last_synced_at ? `上次同步 ${formatDate(scope.last_synced_at)}` : '尚未同步'} · ${scope.is_active ? '启用' : '停用'}</small></div><button class="row-action ${scope.is_active ? '' : 'primary'}" data-toggle-spot-scope="${scope.id}" data-next-active="${scope.is_active ? 'false' : 'true'}" type="button">${scope.is_active ? '停用' : '重新启用'}</button></div>`).join('') : '<div class="empty-row">尚未保存交易对。下次现货同步会根据当前非零持仓自动发现。</div>';
  }

  async function loadSpotScopes() {
    byId('managerSpotSymbols').innerHTML = '<div class="loading-row">正在读取交易对…</div>';
    try {
      state.managerSpotScopes = await BagsAuth.api(`/binance/connections/${state.managerConnectionId}/spot-symbols`);
      renderSpotScopes();
    } catch (error) {
      byId('managerSpotSymbols').innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`;
    }
  }

  async function openBinanceManager(connectionId, scroll = true) {
    const connection = state.connections.find((item) => item.id === connectionId && item.provider === 'binance');
    const account = state.accounts.find((item) => item.id === connection?.account_id);
    if (!connection || !account) return;
    state.managerType = 'binance';
    state.managerConnectionId = connectionId;
    state.managerGroupPrimaryId = null;
    showManagerView('binance');
    byId('managerTitle').textContent = `管理 ${account.label} 现货范围`;
    byId('managerLead').textContent = '自动发现与手动范围都会持久保存；停用不会删除已经导入的成交。';
    setManagerMessage('');
    await loadSpotScopes();
    if (scroll) byId('connectionManager').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  }

  function requestManagerAction(label, action) {
    state.managerPendingAction = { label, action };
    byId('managerAuthLead').textContent = `${label}前，需要重新验证当前管理员身份。`;
    byId('managerTotpField').hidden = !state.user?.two_factor_enabled;
    byId('managerTotp').value = '';
    byId('managerPassword').value = '';
    setMessage('managerAuthMessage', '');
    byId('managerAuth').hidden = false;
    byId('managerPassword').focus();
  }

  async function confirmManagerAction() {
    const pending = state.managerPendingAction;
    if (!pending || state.managerBusy) return;
    const password = byId('managerPassword').value;
    const totp = byId('managerTotp').value.trim();
    if (!password || (state.user?.two_factor_enabled && !/^\d{6}$/.test(totp))) {
      setMessage('managerAuthMessage', state.user?.two_factor_enabled ? '请输入当前密码和六位 TOTP 验证码。' : '请输入当前登录密码。');
      return;
    }
    setManagerBusy(true);
    setMessage('managerAuthMessage', '');
    try {
      await BagsAuth.api('/auth/sensitive/verify', { method: 'POST', body: JSON.stringify({ current_password: password, totp_code: totp || null }) });
      byId('managerAuth').hidden = true;
      const result = await pending.action();
      const message = typeof result === 'string' ? result : result?.message || `${pending.label}已完成。`;
      setManagerMessage(message, typeof result === 'object' ? result.success !== false : true);
      state.managerPendingAction = null;
      byId('managerPassword').value = '';
      byId('managerTotp').value = '';
    } catch (error) {
      if (!byId('managerAuth').hidden) setMessage('managerAuthMessage', error.message);
      else setManagerMessage(error.message);
    } finally {
      setManagerBusy(false);
    }
  }

  async function refreshOpenManager() {
    await refreshData();
    if (state.managerType === 'evm') {
      if (renderWalletManager()) await loadTrackedContracts();
    } else if (state.managerType === 'binance') {
      await loadSpotScopes();
    }
  }

  function saveWalletLabel() {
    const label = byId('managerWalletLabel').value.trim();
    if (!label) {
      byId('managerWalletLabel').focus();
      setManagerMessage('请输入钱包展示名称。');
      return;
    }
    requestManagerAction('保存钱包名称', async () => {
      const group = walletGroupByPrimary(state.managerGroupPrimaryId);
      if (!group) throw new Error('钱包已不存在，请刷新后重试。');
      const multiple = group.accounts.length > 1;
      for (const account of group.accounts) {
        const suffix = account.provider === 'hyperliquid' ? 'Hyperliquid' : chainForAccount(account)?.name || `Chain ${account.chain_id}`;
        const nextLabel = multiple ? `${label.slice(0, Math.max(1, 117 - suffix.length))} · ${suffix}` : label;
        await BagsAuth.api(`/accounts/${account.id}`, { method: 'PATCH', body: JSON.stringify({ label: nextLabel }) });
      }
      await refreshOpenManager();
      return '钱包名称已更新，同一地址下的链账户已统一。';
    });
  }

  function toggleWalletAccount(accountId, isActive) {
    requestManagerAction(isActive ? '重新启用读取范围' : '停用读取范围', async () => {
      await BagsAuth.api(`/accounts/${accountId}`, { method: 'PATCH', body: JSON.stringify({ is_active: isActive }) });
      await refreshOpenManager();
      return isActive ? '读取范围已重新启用。' : '读取范围已停用，既有原始数据和账本没有删除。';
    });
  }

  function addWalletChains() {
    const keys = [...document.querySelectorAll('input[name="managerChain"]:checked')].map((input) => input.value);
    if (!keys.length) {
      setManagerMessage('请至少选择一条要新增的 EVM 网络。');
      return;
    }
    requestManagerAction('添加 EVM 网络', async () => {
      const group = walletGroupByPrimary(state.managerGroupPrimaryId);
      if (!group) throw new Error('钱包已不存在，请刷新后重试。');
      const chains = state.chains.filter((chain) => keys.includes(chain.key) && chain.configured);
      const baseLabel = byId('managerWalletLabel').value.trim() || walletBaseLabel(group);
      const finalCount = group.accounts.length + chains.length;
      const failures = [];
      for (const chain of chains) {
        const account = await BagsAuth.api('/accounts', { method: 'POST', body: JSON.stringify({ portfolio_id: group.portfolioId, kind: 'wallet', provider: 'evm', label: evmAccountLabel(baseLabel, chain, finalCount), chain_id: chain.key, address: group.address }) });
        try {
          const run = await BagsAuth.api(`/evm/accounts/${account.id}/sync`, { method: 'POST', body: '{}' });
          await refreshPortfolioSnapshot(group.portfolioId, run);
          if (run.status === 'failed') failures.push(`${chain.name}：${run.error_message || '同步失败'}`);
        } catch (error) {
          failures.push(`${chain.name}：${error.message}`);
        }
      }
      await refreshOpenManager();
      dispatchEvent(new CustomEvent('bags:data-changed'));
      return failures.length
        ? { success: false, message: `网络已添加，但首次同步存在问题：${failures.join('；')}` }
        : `已添加并同步 ${chains.length} 条网络。`;
    });
  }

  function addHyperliquidNetwork() {
    requestManagerAction('添加 Hyperliquid', async () => {
      let group = walletGroupByPrimary(state.managerGroupPrimaryId);
      if (!group) throw new Error('钱包已不存在，请刷新后重试。');
      let account = group.hyperAccount;
      if (!account) {
        account = await BagsAuth.api('/accounts', { method: 'POST', body: JSON.stringify({ portfolio_id: group.portfolioId, kind: 'perp_dex', provider: 'hyperliquid', label: `${walletBaseLabel(group)} · Hyperliquid`, external_account_id: group.address, address: group.address }) });
      }
      let connection = state.connections.find((item) => item.account_id === account.id);
      if (!connection) {
        connection = await BagsAuth.api('/connections', { method: 'POST', body: JSON.stringify({ account_id: account.id, name: `${walletBaseLabel(group)} Public`, provider: 'hyperliquid', api_key: group.address, requested_permissions: ['read'] }) });
      }
      let syncError = null;
      try {
        const run = await BagsAuth.api(`/perp-dex/connections/${connection.id}/sync`, { method: 'POST', body: JSON.stringify({ include_spot: true }) });
        await refreshPortfolioSnapshot(group.portfolioId, run);
        if (run.status === 'failed') syncError = run.error_message || '首次同步失败';
      } catch (error) {
        syncError = error.message;
      }
      await refreshOpenManager();
      dispatchEvent(new CustomEvent('bags:data-changed'));
      return syncError ? { success: false, message: `Hyperliquid 已添加，但首次同步失败：${syncError}` } : 'Hyperliquid 已加入同一地址的钱包组。';
    });
  }

  function addTrackedContracts() {
    const contracts = splitValues(byId('managerContractAddresses').value).map((value) => value.toLowerCase());
    const accountId = byId('managerContractChain').value;
    if (!accountId || !contracts.length) {
      setManagerMessage('请选择网络并输入至少一个代币合约地址。');
      return;
    }
    requestManagerAction('保存代币合约', async () => {
      await BagsAuth.api(`/evm/accounts/${accountId}/tracked-contracts`, { method: 'POST', body: JSON.stringify({ contracts: contracts.map((contract_address) => ({ contract_address })) }) });
      byId('managerContractAddresses').value = '';
      await loadTrackedContracts();
      return `已保存 ${contracts.length} 个合约，后续普通同步会继续读取。`;
    });
  }

  function toggleTrackedContract(contractId, isActive) {
    const accountId = byId('managerContractChain').value;
    requestManagerAction(isActive ? '重新启用代币合约' : '停用代币合约', async () => {
      await BagsAuth.api(`/evm/accounts/${accountId}/tracked-contracts/${contractId}`, { method: 'PATCH', body: JSON.stringify({ is_active: isActive }) });
      await loadTrackedContracts();
      return isActive ? '代币合约已重新启用。' : '代币合约已停用，历史数据仍然保留。';
    });
  }

  function backfillWallet() {
    const accountId = byId('managerContractChain').value;
    const from = byId('managerFromBlock').value;
    const to = byId('managerToBlock').value;
    const hashes = splitValues(byId('managerTransactionHashes').value).map((value) => value.toLowerCase());
    if (!accountId || (!from && !to && !hashes.length)) {
      setManagerMessage('请选择网络，并填写区块范围或至少一个交易哈希。');
      return;
    }
    if (from && to && Number(to) < Number(from)) {
      setManagerMessage('结束区块不能小于起始区块。');
      return;
    }
    requestManagerAction('执行精确补扫', async () => {
      const payload = { transaction_hashes: hashes };
      if (from) payload.from_block = Number(from);
      if (to) payload.to_block = Number(to);
      const run = await BagsAuth.api(`/evm/accounts/${accountId}/sync`, { method: 'POST', body: JSON.stringify(payload) });
      const account = state.accounts.find((item) => item.id === accountId);
      if (account) await refreshPortfolioSnapshot(account.portfolio_id, run);
      await refreshData();
      dispatchEvent(new CustomEvent('bags:data-changed'));
      return run.status === 'failed'
        ? { success: false, message: run.error_message || '补扫失败，请查看同步状态。' }
        : `补扫完成：写入 ${run.stats_json?.raw_events_inserted || 0} 条新原始事件。`;
    });
  }

  function addSpotSymbols() {
    const symbols = splitValues(byId('managerSpotSymbolInput').value).map((value) => value.toUpperCase());
    if (!symbols.length || symbols.some((symbol) => !/^[A-Z0-9]{3,30}$/.test(symbol))) {
      setManagerMessage('请输入有效的 Binance 现货交易对，例如 BTCUSDT。');
      return;
    }
    requestManagerAction('添加 Binance 现货交易对', async () => {
      const connectionId = state.managerConnectionId;
      await BagsAuth.api(`/binance/connections/${connectionId}/spot-symbols`, { method: 'POST', body: JSON.stringify({ symbols }) });
      const payload = { products: ['spot'], spot_symbols: symbols };
      const historyStart = isoOrNull(byId('managerSpotHistoryStart').value);
      if (historyStart) payload.history_start = historyStart;
      let run;
      try {
        run = await BagsAuth.api(`/binance/connections/${connectionId}/sync`, { method: 'POST', body: JSON.stringify(payload) });
      } catch (error) {
        await loadSpotScopes();
        throw new Error(`交易对已保存，但补扫失败：${error.message}`);
      }
      byId('managerSpotSymbolInput').value = '';
      await refreshOpenManager();
      dispatchEvent(new CustomEvent('bags:data-changed'));
      return run.status === 'failed'
        ? { success: false, message: `交易对已保存，但补扫失败：${run.error_message || '请查看同步状态'}` }
        : `已保存并补扫 ${symbols.length} 个交易对。`;
    });
  }

  function toggleSpotScope(scopeId, isActive) {
    requestManagerAction(isActive ? '重新启用现货交易对' : '停用现货交易对', async () => {
      await BagsAuth.api(`/binance/connections/${state.managerConnectionId}/spot-symbols/${scopeId}`, { method: 'PATCH', body: JSON.stringify({ is_active: isActive }) });
      await loadSpotScopes();
      return isActive ? '交易对已重新启用。' : '交易对已停用，既有成交不会删除。';
    });
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
    byId('closeManager').addEventListener('click', closeManager);
    byId('saveWalletLabel').addEventListener('click', saveWalletLabel);
    byId('addWalletChains').addEventListener('click', addWalletChains);
    byId('addHyperliquidNetwork').addEventListener('click', addHyperliquidNetwork);
    byId('managerContractChain').addEventListener('change', loadTrackedContracts);
    byId('addTrackedContracts').addEventListener('click', addTrackedContracts);
    byId('backfillWallet').addEventListener('click', backfillWallet);
    byId('addSpotSymbols').addEventListener('click', addSpotSymbols);
    byId('cancelManagerAuth').addEventListener('click', () => {
      if (state.managerBusy) return;
      state.managerPendingAction = null;
      byId('managerAuth').hidden = true;
      byId('managerPassword').value = '';
      byId('managerTotp').value = '';
    });
    byId('confirmManagerAuth').addEventListener('click', confirmManagerAction);
    [byId('managerPassword'), byId('managerTotp')].forEach((input) => input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        confirmManagerAction();
      }
    }));
    document.querySelectorAll('[data-back]').forEach((button) => button.addEventListener('click', () => setStep(Number(button.dataset.back))));
    byId('connectionsList').addEventListener('click', (event) => {
      const sync = event.target.closest('[data-resync]');
      if (sync) resyncAccount(sync.dataset.resync, sync);
      const syncWallet = event.target.closest('[data-resync-wallet]');
      if (syncWallet) resyncWalletGroup(syncWallet.dataset.resyncWallet, syncWallet);
      const rotate = event.target.closest('[data-rotate]');
      if (rotate) startCredentialRotation(rotate.dataset.rotate);
      const wallet = event.target.closest('[data-manage-wallet]');
      if (wallet) openWalletManager(wallet.dataset.manageWallet);
      const binance = event.target.closest('[data-manage-binance]');
      if (binance) openBinanceManager(binance.dataset.manageBinance);
    });
    byId('connectionManager').addEventListener('click', (event) => {
      const account = event.target.closest('[data-toggle-account]');
      if (account) toggleWalletAccount(account.dataset.toggleAccount, account.dataset.nextActive === 'true');
      const contract = event.target.closest('[data-toggle-contract]');
      if (contract) toggleTrackedContract(contract.dataset.toggleContract, contract.dataset.nextActive === 'true');
      const scope = event.target.closest('[data-toggle-spot-scope]');
      if (scope) toggleSpotScope(scope.dataset.toggleSpotScope, scope.dataset.nextActive === 'true');
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !byId('connectionManager').hidden && !state.managerBusy) closeManager();
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
