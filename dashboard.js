(function () {
  'use strict';

  const state = { summary: null, candidates: [], groups: [], events: [], portfolioId: null };
  const currencyFormatter = new Intl.NumberFormat('zh-CN', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const numberFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 8 });
  const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  });

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    })[character]);
  }

  function decimal(value) {
    if (value === null || value === undefined || value === '') return null;
    const parsed = Number(value);
    // Never render a financial value that JavaScript cannot represent safely.
    // The API keeps full Decimal precision; unavailable is safer than silently
    // showing a rounded balance above Number.MAX_SAFE_INTEGER.
    return Number.isFinite(parsed) && Math.abs(parsed) <= Number.MAX_SAFE_INTEGER ? parsed : null;
  }

  function decimalParts(value) {
    const match = String(value ?? '').trim().match(/^([+-]?)(\d+)(?:\.(\d+))?$/);
    if (!match) return null;
    return { negative: match[1] === '-', integer: match[2], fraction: match[3] || '' };
  }

  function groupedInteger(value) {
    try {
      return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(BigInt(value));
    } catch (_) {
      return null;
    }
  }

  function decimalText(value, fractionDigits) {
    const parts = decimalParts(value);
    if (!parts) return null;
    const integer = groupedInteger(parts.integer);
    if (integer === null) return null;
    const fraction = parts.fraction.slice(0, fractionDigits).replace(/0+$/, '');
    return `${parts.negative ? '−' : ''}${integer}${fraction ? `.${fraction}` : ''}`;
  }

  function currency(value, signed = false) {
    const parts = decimalParts(value);
    const formatted = decimalText(value, 2);
    if (!parts || formatted === null) return '—';
    const unsigned = `$${formatted.replace('−', '')}`;
    if (!signed) return parts.negative ? `−${unsigned}` : unsigned;
    return parts.negative ? `−${unsigned}` : (parts.integer !== '0' || /[1-9]/.test(parts.fraction) ? `+${unsigned}` : '$0');
  }

  function quantity(value) {
    return decimalText(value, 8) ?? '—';
  }

  function percentage(value, signed = false) {
    const parsed = decimal(value);
    if (parsed === null) return '—';
    const prefix = signed && parsed > 0 ? '+' : parsed < 0 ? '−' : '';
    return `${prefix}${Math.abs(parsed).toFixed(2)}%`;
  }

  function classFor(value) {
    const parsed = decimal(value);
    return parsed === null || parsed === 0 ? '' : parsed > 0 ? 'gain' : 'loss';
  }

  function formatDate(value) {
    if (!value) return '尚未同步';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '尚未同步' : dateFormatter.format(date);
  }

  function initials(label) {
    const parts = String(label || '').trim().split(/\s+/).filter(Boolean);
    return (parts.length > 1 ? parts.map((part) => part[0]).join('') : (parts[0] || '—').slice(0, 2)).toUpperCase();
  }

  function assetClass(symbol) {
    const normalized = String(symbol || '').toLowerCase();
    return ['btc', 'eth', 'sol'].includes(normalized) ? normalized : '';
  }

  function primeLoadingState() {
    document.querySelector('.mode').textContent = '正在读取真实账本';
    document.querySelector('.snapshot').innerHTML = '<i aria-hidden="true"></i>同步 Dashboard 数据…';
    document.querySelector('.account-chip').innerHTML = '<i aria-hidden="true"></i>正在读取账户';
    ['netWorth', 'netDelta', 'performanceValue', 'performancePct'].forEach((id) => {
      const node = document.getElementById(id);
      if (node) node.textContent = '—';
    });
    document.getElementById('netCaption').textContent = '净值、成本与现金流正在聚合';
    const ledgerBody = document.querySelector('.ledger tbody');
    if (ledgerBody) ledgerBody.innerHTML = '<tr><td colspan="6" class="live-empty">正在生成真实资产汇总…</td></tr>';
    const sourceBody = document.querySelector('.sources .source-body');
    if (sourceBody) sourceBody.innerHTML = '<div class="live-empty">正在读取账户构成…</div>';
    const flowBody = document.querySelector('.flow .flow-body');
    if (flowBody) flowBody.innerHTML = '<div class="live-empty">正在读取最近的转账记录…</div>';
    const queueList = document.querySelector('.queue-list');
    if (queueList) queueList.innerHTML = '<div class="live-empty">正在检查待确认事项…</div>';
    resetStaticMetrics('正在读取真实数据');
  }

  function renderEmpty(message) {
    document.querySelector('.mode').textContent = '尚无可展示数据';
    document.querySelector('.snapshot').innerHTML = '<i aria-hidden="true"></i>等待首次成本重算';
    document.getElementById('netWorth').textContent = '—';
    document.getElementById('netDelta').textContent = '—';
    document.getElementById('netCaption').textContent = message;
    document.getElementById('performanceValue').textContent = '—';
    document.getElementById('performancePct').textContent = '—';
    document.querySelector('.ledger tbody').innerHTML = `<tr><td colspan="6" class="live-empty">${escapeHtml(message)}</td></tr>`;
    document.querySelector('.sources .source-body').innerHTML = `<div class="live-empty">${escapeHtml(message)}</div>`;
    document.querySelector('.flow .flow-body').innerHTML = '<div class="live-empty">完成交易同步和转账匹配后，这里会显示真实资金路径。</div>';
    document.querySelector('.queue-list').innerHTML = '<div class="live-empty">当前没有可读取的审核队列。</div>';
    resetStaticMetrics('尚无真实数据');
    clearCharts();
    renderLiveRoute();
  }

  function resetStaticMetrics(statusText) {
    document.querySelector('.account-chip').innerHTML = '<i aria-hidden="true"></i>尚未连接账户';
    const score = document.querySelector('.health .score');
    if (score) score.style.background = 'conic-gradient(var(--green) 0 0deg,#273041 0deg)';
    const scoreValue = document.querySelector('.health .score b');
    if (scoreValue) scoreValue.textContent = '—';
    const scoreLabel = document.querySelector('.health .score small');
    if (scoreLabel) scoreLabel.textContent = '成本覆盖';
    const scoreText = document.querySelector('.health .score-text');
    if (scoreText) scoreText.innerHTML = `<b>${escapeHtml(statusText)}</b><span>同步后才会计算账本健康度</span>`;
    document.querySelectorAll('.health-list b').forEach((node) => { node.textContent = '—'; });
    const reviewButton = document.querySelector('.review-btn');
    if (reviewButton) {
      reviewButton.textContent = '暂无待审核事项';
      reviewButton.dataset.toast = '同步真实流水后才会生成审核事项';
    }
    const badge = document.querySelector('.nav-badge');
    if (badge) {
      badge.textContent = '0';
      badge.setAttribute('aria-label', '0 笔待审核');
    }
    const riskNumber = document.querySelector('.risk-number');
    if (riskNumber) riskNumber.textContent = '—';
    const riskCopy = document.querySelector('.risk p');
    if (riskCopy) riskCopy.textContent = '同步衍生品账户后计算';
    const riskBar = document.querySelector('.risk-bar');
    if (riskBar) riskBar.setAttribute('aria-label', '保证金使用率暂无数据');
    const riskBarValue = document.querySelector('.risk-bar span');
    if (riskBarValue) riskBarValue.style.width = '0%';
    const riskNote = document.querySelector('.risk-note');
    if (riskNote) riskNote.textContent = 'NET EXPOSURE · —';
    document.querySelectorAll('.performance-foot b').forEach((node) => {
      node.textContent = '—';
      node.className = '';
    });
    const flowStatus = document.querySelector('.flow .status');
    if (flowStatus) {
      flowStatus.textContent = '尚无数据';
      flowStatus.className = 'status';
    }
    const queueStatus = document.querySelector('.queue .status');
    if (queueStatus) {
      queueStatus.textContent = '0 项';
      queueStatus.className = 'status';
    }
    window.dashboardPeriods = Object.fromEntries(['1D', '30D', 'ALL'].map((key) => [key, {
      net: '—', delta: '—', caption: '等待真实账本数据', pnl: '—', pct: '—',
    }]));
  }

  function clearCharts() {
    document.querySelectorAll('.spark, .performance-chart svg').forEach((svg) => {
      svg.querySelectorAll('path.area, path.line').forEach((path) => path.setAttribute('d', ''));
      const circle = svg.querySelector('circle');
      if (circle) circle.setAttribute('r', '0');
      svg.setAttribute('aria-label', '暂无真实净值历史');
    });
    document.querySelectorAll('.performance-chart text').forEach((label, index) => {
      label.textContent = index === 0 ? '等待首次快照' : '';
    });
  }

  function configurePeriods(summary) {
    const periods = Object.fromEntries(summary.periods.map((period) => [period.key, period]));
    const net = summary.total_net_worth_usd === null ? '待补价格' : currency(summary.total_net_worth_usd);
    const labels = { '1D': '今日', '30D': '过去 30 日', ALL: '全周期' };
    window.dashboardPeriods = {};
    ['1D', '30D', 'ALL'].forEach((key) => {
      const period = periods[key];
      const pnl = period?.complete ? currency(period.pnl_usd, true) : '—';
      const pct = period?.complete ? percentage(period.return_percent, true) : '—';
      window.dashboardPeriods[key] = {
        net,
        delta: pct,
        caption: period?.complete ? `${labels[key]}投资回报 ${pnl}` : `${labels[key]}缺少期初快照或完整估值`,
        pnl,
        pct: period?.complete ? `${pct} / ${labels[key]}` : `数据不足 / ${labels[key]}`,
      };
    });
    const active = document.querySelector('[data-period].active') || document.querySelector('[data-period="30D"]');
    if (active) active.click();
  }

  function renderHome(summary, groups, candidates) {
    document.querySelector('.mode').textContent = summary.health.valuation_complete ? '真实账本 · 估值完整' : '真实账本 · 部分估值';
    document.querySelector('.snapshot').innerHTML = `<i aria-hidden="true"></i>成本快照 · ${escapeHtml(formatDate(summary.as_of))}`;
    document.querySelector('.account-chip').innerHTML = `<i aria-hidden="true"></i>${summary.accounts.length} 个账户已连接`;
    configurePeriods(summary);
    renderHealth(summary);
    renderRisk(summary);
    renderPerformance(summary);
    renderAssets(summary.assets, '.ledger tbody');
    renderAccountSources(summary.accounts, summary.as_of);
    renderTransfer(summary, groups, candidates);
    renderQueue(summary, candidates);
    updateCharts(summary.nav_history);
  }

  function renderHealth(summary) {
    const score = summary.health.cost_coverage_percent;
    const scoreNode = document.querySelector('.health .score b');
    scoreNode.textContent = score === null ? '—' : decimal(score).toFixed(1);
    document.querySelector('.health .score small').textContent = '成本覆盖';
    const degrees = score === null ? 0 : Math.max(0, Math.min(360, decimal(score) * 3.6));
    document.querySelector('.health .score').style.background = `conic-gradient(var(--green) 0 ${degrees}deg,#273041 ${degrees}deg)`;
    const scoreText = document.querySelector('.health .score-text');
    scoreText.innerHTML = summary.health.valuation_complete
      ? '<b>当前估值完整</b><span>成本、价格与账户权益可用于汇总</span>'
      : '<b>仍有数据待补</b><span>总净值不会用缺失值伪装为零</span>';
    const values = document.querySelectorAll('.health-list b');
    if (values[0]) values[0].textContent = summary.health.balance_difference_count
      ? `${summary.health.balance_difference_count} 项`
      : '0 项';
    if (values[1]) values[1].textContent = String(summary.health.unknown_deposits);
    if (values[2]) values[2].textContent = String(summary.health.pending_transfer_reviews);
    const labels = document.querySelectorAll('.health-list span');
    if (labels[0]) labels[0].childNodes[0].textContent = '余额差异 ';
    if (labels[1]) labels[1].childNodes[0].textContent = '未知存入 ';
    if (labels[2]) labels[2].childNodes[0].textContent = '待审核转账 ';
    const reviewButton = document.querySelector('.review-btn');
    reviewButton.textContent = summary.health.pending_transfer_reviews ? `审核 ${summary.health.pending_transfer_reviews} 笔转账` : '转账队列已清理';
    reviewButton.dataset.toast = summary.health.warnings[0] || '当前没有需要处理的转账审核';
    const badge = document.querySelector('.nav-badge');
    if (badge) {
      badge.textContent = String(summary.health.pending_transfer_reviews);
      badge.setAttribute('aria-label', `${summary.health.pending_transfer_reviews} 笔待审核`);
    }
  }

  function renderRisk(summary) {
    const margin = decimal(summary.margin_usage_percent);
    document.querySelector('.risk-number').textContent = margin === null ? '—' : `${margin.toFixed(2)}%`;
    document.querySelector('.risk p').textContent = margin === null ? '暂无可用保证金数据' : '基于当前衍生品权益计算';
    const bar = document.querySelector('.risk-bar span');
    bar.style.width = `${margin === null ? 0 : Math.max(0, Math.min(100, margin))}%`;
    document.querySelector('.risk-bar').setAttribute('aria-label', margin === null ? '保证金使用率未知' : `保证金使用率 ${margin.toFixed(2)}%`);
    document.querySelector('.risk-note').textContent = `NET EXPOSURE · ${currency(summary.net_exposure_usd, true)}`;
  }

  function renderPerformance(summary) {
    const values = document.querySelectorAll('.performance-foot b');
    const data = [
      [summary.realized_pnl_usd, true],
      [summary.unrealized_pnl_usd, true],
      [summary.fee_expense_usd === null ? null : -decimal(summary.fee_expense_usd), true],
      [summary.funding_pnl_usd, true],
    ];
    values.forEach((node, index) => {
      const value = data[index]?.[0];
      node.textContent = currency(value, true);
      node.className = classFor(value);
    });
  }

  function assetRows(assets) {
    if (!assets.length) return '<tr><td colspan="6" class="live-empty">暂无持仓。完成同步与成本重算后会自动出现。</td></tr>';
    return assets.slice(0, 50).map((asset) => {
      const mark = escapeHtml(String(asset.symbol || '?').slice(0, 1).toUpperCase());
      const pnlClass = classFor(asset.unrealized_pnl_usd);
      const mode = asset.manual_cost_usd !== null ? 'Manual' : 'Calculated';
      return `<tr><td><div class="asset"><span class="asset-mark ${assetClass(asset.symbol)}" aria-hidden="true">${mark}</span><span>${escapeHtml(asset.name)}<small>${escapeHtml(asset.symbol)} · ${asset.account_count} 个账户 · ${asset.open_lot_count} Lots</small></span></div></td><td class="n">${quantity(asset.quantity)}</td><td class="n">${currency(asset.effective_cost_usd)}</td><td class="n">${currency(asset.market_value_usd)}</td><td class="n ${pnlClass}">${currency(asset.unrealized_pnl_usd, true)}</td><td class="n"><span class="cost-mode">${mode}</span></td></tr>`;
    }).join('');
  }

  function renderAssets(assets, selector) {
    const body = document.querySelector(selector);
    if (body) body.innerHTML = assetRows(assets);
  }

  function renderAccountSources(accounts, asOf) {
    const body = document.querySelector('.sources .source-body');
    if (!accounts.length) {
      body.innerHTML = '<div class="live-empty">尚未连接账户。</div>';
      return;
    }
    const total = accounts.reduce((sum, account) => sum + (decimal(account.total_equity_usd) || 0), 0);
    const rows = accounts.slice(0, 6).map((account) => `<div class="source-row"><span class="source-logo">${escapeHtml(initials(account.label))}</span><span class="source-name">${escapeHtml(account.label)}<small>${escapeHtml(account.provider)} · ${escapeHtml(account.kind)}</small></span><span class="source-value">${currency(account.total_equity_usd)}</span></div>`).join('');
    const colors = ['var(--blue)', 'var(--violet)', 'var(--green)', 'var(--gold)', 'var(--red)', 'var(--muted)'];
    const meter = accounts.slice(0, 6).map((account, index) => {
      const width = total > 0 ? ((decimal(account.total_equity_usd) || 0) / total) * 100 : 0;
      return `<span style="width:${width.toFixed(3)}%;background:${colors[index]}"></span>`;
    }).join('');
    body.innerHTML = `${rows}<div class="source-meter" aria-label="账户净值构成">${meter}</div><div class="source-foot"><span>Dashboard 截止时间</span><b style="font-family:var(--mono);font-weight:500;color:var(--muted)">${escapeHtml(formatDate(asOf))}</b></div>`;
  }

  function accountLabel(summary, id) {
    return summary.accounts.find((account) => account.account_id === id)?.label || '未知账户';
  }

  function assetSymbol(summary, id) {
    return summary.assets.find((asset) => asset.asset_id === id)?.symbol || '资产';
  }

  function arrowIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 12h15M15 7l5 5-5 5" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  function renderTransfer(summary, groups, candidates) {
    const panel = document.querySelector('.flow');
    const body = panel.querySelector('.flow-body');
    body.classList.add('live-transfer');
    const status = panel.querySelector('.status');
    const group = groups[0];
    const candidate = candidates[0];
    if (group) {
      const source = accountLabel(summary, group.source_account_id);
      const destination = accountLabel(summary, group.destination_account_id);
      const sourceSymbol = assetSymbol(summary, group.source_asset_id);
      const destinationSymbol = assetSymbol(summary, group.destination_asset_id);
      status.textContent = group.status === 'confirmed' ? '已人工确认' : '已自动匹配';
      status.className = 'status review';
      body.innerHTML = `<div class="flow-node"><div class="node-label"><i class="node-dot" aria-hidden="true"></i>Source</div><div class="node-name">${escapeHtml(source)}</div><div class="node-data">−${quantity(group.source_amount)} ${escapeHtml(sourceSymbol)}</div></div><div class="arrow" aria-hidden="true">${arrowIcon()}</div><div class="flow-node"><div class="node-label"><i class="node-dot wallet" aria-hidden="true"></i>Destination</div><div class="node-name">${escapeHtml(destination)}</div><div class="node-data">+${quantity(group.destination_amount)} ${escapeHtml(destinationSymbol)}</div></div><div class="flow-facts"><div class="fact"><span>继承成本</span><b>${currency(group.original_cost_basis)}</b></div><div class="fact"><span>转账手续费</span><b>${quantity(group.fee_amount)} ${escapeHtml(assetSymbol(summary, group.fee_asset_id))}</b></div><div class="fact"><span>已实现 PnL</span><b class="green">$0.00</b></div></div>`;
      return;
    }
    if (candidate) {
      status.textContent = `需要人工确认 · ${candidate.score} 分`;
      status.className = 'status review';
      body.innerHTML = `<div class="flow-node"><div class="node-label"><i class="node-dot" aria-hidden="true"></i>Source</div><div class="node-name">${escapeHtml(accountLabel(summary, candidate.source_account_id))}</div><div class="node-data">−${quantity(candidate.source_amount)} ${escapeHtml(assetSymbol(summary, candidate.source_asset_id))}</div></div><div class="arrow" aria-hidden="true">${arrowIcon()}</div><div class="flow-node"><div class="node-label"><i class="node-dot wallet" aria-hidden="true"></i>Candidate</div><div class="node-name">${escapeHtml(accountLabel(summary, candidate.destination_account_id))}</div><div class="node-data">+${quantity(candidate.destination_amount)} ${escapeHtml(assetSymbol(summary, candidate.destination_asset_id))}</div></div><div class="flow-facts"><div class="fact"><span>置信度</span><b>${candidate.score} / 100</b></div><div class="fact"><span>预计手续费</span><b>${quantity(candidate.estimated_fee_amount)}</b></div><div class="fact"><span>状态</span><b>等待审核</b></div></div>`;
      return;
    }
    status.textContent = '没有待处理转账';
    body.innerHTML = '<div class="live-empty">当前没有已匹配或待审核的转账记录。</div>';
  }

  function renderQueue(summary, candidates) {
    const panel = document.querySelector('.queue');
    panel.querySelector('.status').textContent = `${summary.health.pending_transfer_reviews + summary.health.unknown_deposits} 项`;
    const list = panel.querySelector('.queue-list');
    const items = candidates.slice(0, 3).map((candidate) => `<div class="queue-item"><span class="queue-icon" aria-hidden="true">${arrowIcon()}</span><div><div class="queue-title">${escapeHtml(accountLabel(summary, candidate.source_account_id))} → ${escapeHtml(accountLabel(summary, candidate.destination_account_id))}</div><div class="queue-meta">${quantity(candidate.source_amount)} ${escapeHtml(assetSymbol(summary, candidate.source_asset_id))} · 预计费用 ${quantity(candidate.estimated_fee_amount)}</div></div><div class="queue-right"><b>${candidate.score} / 100</b><span>匹配置信度</span></div></div>`);
    if (summary.health.unknown_deposits) {
      items.push(`<div class="queue-item"><span class="queue-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M12 4v16M4 12h16" stroke="currentColor" stroke-linecap="round"/></svg></span><div><div class="queue-title">未知来源存入</div><div class="queue-meta">${summary.health.unknown_deposits} 笔需要分类或补充成本</div></div><div class="queue-right"><b>分类</b><span>需要来源</span></div></div>`);
    }
    if (summary.health.balance_difference_count) {
      items.push(`<div class="queue-item"><span class="queue-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M4 18.5h16M6.5 15l3-4 3 2 5-6" stroke="currentColor" stroke-linecap="round"/></svg></span><div><div class="queue-title">余额与账本存在差异</div><div class="queue-meta">${summary.health.balance_difference_count} 项 · 最大 ${percentage(summary.health.max_balance_difference_percent)}</div></div><div class="queue-right"><b>核对</b><span>超过 0.1%</span></div></div>`);
    }
    list.innerHTML = items.length ? `${items.join('')}<button class="review-link" type="button" data-toast="请进入转账审核或成本账本处理对应事项">查看处理入口</button>` : '<div class="live-empty">没有需要人工确认的事项。</div>';
  }

  function pathForHistory(history, width, height) {
    const rows = history.filter((row) => decimal(row.total_nav) !== null);
    if (!rows.length) return null;
    const values = rows.map((row) => decimal(row.total_nav));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const points = values.map((value, index) => {
      const x = rows.length === 1 ? width : (index / (rows.length - 1)) * width;
      const y = height - 8 - ((value - min) / span) * (height - 20);
      return [x, y];
    });
    const line = points.map(([x, y], index) => `${index ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ');
    return { line, area: `${line} L${width} ${height} L0 ${height}Z`, rows };
  }

  function updateCharts(history) {
    const spark = pathForHistory(history, 440, 126);
    const performance = pathForHistory(history, 690, 160);
    const apply = (selector, data) => {
      const svg = document.querySelector(selector);
      if (!svg) return;
      if (!data) {
        svg.querySelectorAll('path.area, path.line').forEach((path) => path.setAttribute('d', ''));
        const emptyCircle = svg.querySelector('circle');
        if (emptyCircle) emptyCircle.setAttribute('r', '0');
        svg.setAttribute('aria-label', '暂无真实净值历史');
        return;
      }
      svg.querySelector('path.line')?.setAttribute('d', data.line);
      svg.querySelector('path.area')?.setAttribute('d', data.area);
      const circle = svg.querySelector('circle');
      const last = data.line.match(/([\d.]+) ([\d.]+)$/);
      if (circle && last) {
        circle.setAttribute('cx', last[1]);
        circle.setAttribute('cy', last[2]);
        circle.setAttribute('r', '4');
      }
      svg.setAttribute('aria-label', `真实净值曲线，共 ${data.rows.length} 个快照`);
    };
    apply('.spark', spark);
    apply('.performance-chart svg', performance);
    const labels = document.querySelectorAll('.performance-chart text');
    if (performance && labels.length) {
      labels[0].textContent = formatDate(performance.rows[0].as_of).split(' ')[0];
      labels[labels.length - 1].textContent = '当前';
    }
  }

  function metric(label, value, note, valueClass = '') {
    return `<div class="metric"><label>${escapeHtml(label)}</label><strong class="${valueClass}">${escapeHtml(value)}</strong><span>${escapeHtml(note)}</span></div>`;
  }

  function allocationList(items) {
    if (!items.length) return '<div class="live-empty">暂无可估值数据。</div>';
    const colors = ['green', 'violet', 'blue', 'gold'];
    return `<div class="allocation-list">${items.slice(0, 8).map((item, index) => `<div class="allocation-row"><div><b>${escapeHtml(item.label)}</b><small>${percentage(item.percentage)}</small></div><div class="bar ${colors[index % colors.length]}"><span style="width:${Math.max(0, Math.min(100, decimal(item.percentage) || 0))}%"></span></div><span class="amount">${currency(item.value_usd)}</span></div>`).join('')}</div>`;
  }

  function accountList(accounts) {
    if (!accounts.length) return '<div class="live-empty">尚未连接账户。</div>';
    return `<div class="account-list">${accounts.map((account) => `<div class="account-row"><span class="account-logo">${escapeHtml(initials(account.label))}</span><span><b>${escapeHtml(account.label)}</b><small>${escapeHtml(account.provider)} · ${escapeHtml(account.kind)} · ${escapeHtml(formatDate(account.last_synced_at))}</small></span><span><strong>${currency(account.total_equity_usd)}</strong><span class="tag" style="color:${account.valuation_complete ? 'var(--green)' : 'var(--gold)'}">${account.valuation_complete ? '已估值' : '数据待补'}</span></span></div>`).join('')}</div>`;
  }

  function historyChart(history) {
    const path = pathForHistory(history, 700, 164);
    if (!path) return '<div class="live-empty">至少保存一个 Portfolio Snapshot 后才会生成净值曲线。</div>';
    return `<div class="route-chart"><svg viewBox="0 0 700 185" role="img" aria-label="真实历史净值曲线"><defs><linearGradient id="liveRouteFill" x1="0" x2="0" y1="0" y2="1"><stop stop-color="#8c9dff" stop-opacity=".26"/><stop offset="1" stop-color="#8c9dff" stop-opacity="0"/></linearGradient></defs><path class="grid" d="M0 28H700M0 73H700M0 118H700M0 163H700"/><path class="area" fill="url(#liveRouteFill)" d="${path.area}"/><path class="line" d="${path.line}"/><text x="0" y="181">${escapeHtml(formatDate(path.rows[0].as_of).split(' ')[0])}</text><text x="650" y="181">当前</text></svg></div>`;
  }

  function exposureList(summary) {
    if (!summary.exposures.length) return '<div class="live-empty">当前没有 Spot 或 Perp 敞口。</div>';
    const max = Math.max(...summary.exposures.map((row) => Math.max(decimal(row.gross_long_usd) || 0, decimal(row.gross_short_usd) || 0)), 1);
    return summary.exposures.slice(0, 20).map((row) => {
      const longWidth = ((decimal(row.gross_long_usd) || 0) / max) * 48;
      const shortWidth = ((decimal(row.gross_short_usd) || 0) / max) * 48;
      return `<div class="exposure-row"><span><b>${escapeHtml(row.symbol)}</b><small>净数量 ${quantity(row.net_quantity)}</small></span><div class="exposure-track"><span class="long" style="width:${longWidth}%"></span><span class="short" style="width:${shortWidth}%"></span></div><span class="n">${currency(row.gross_long_usd)} L</span><span class="n loss">${currency(row.gross_short_usd)} S</span></div>`;
    }).join('');
  }

  function renderRouteEmpty(route) {
    const routeView = document.getElementById('routeView');
    if (!routeView || route === 'dashboard' || route === 'settings') return;
    const labels = {
      portfolio: ['资产组合', '创建 Portfolio 并同步账户后，这里会显示真实净值与构成。'],
      assets: ['资产', '同步完成并重算成本后，这里会显示真实持仓。'],
      accounts: ['账户', '连接只读账户后，这里会显示同步状态与账户权益。'],
      transfers: ['转账审核', '同步真实充值、提现和链上流水后，这里会生成匹配候选。'],
      transactions: ['交易流水', '账户同步完成后，这里会显示标准化账本事件。'],
      ledger: ['成本账本', '先同步真实交易，再执行成本重算。'],
      pnl: ['盈亏分析', '至少需要真实成本结果和 Portfolio Snapshot 才能计算期间盈亏。'],
      exposure: ['净敞口', '同步现货与衍生品账户后，这里会显示真实敞口。'],
      analytics: ['分析', '保存真实净值快照后，这里会生成分析视图。'],
    };
    const [title, copy] = labels[route] || ['等待真实数据', '完成账户连接和同步后再展示数据。'];
    const action = ['accounts', 'portfolio', 'assets'].includes(route)
      ? '<div class="route-actions"><a class="action primary" href="/connections.html">连接只读账户</a></div>'
      : '';
    routeView.innerHTML = `<div class="route-header"><div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(copy)}</p></div>${action}</div><div class="view-grid"><article class="view-panel span-12"><div class="live-empty">当前没有真实数据。所有数值会在账户同步完成后出现。</div></article></div>`;
  }

  function renderTransferRoute(summary) {
    const routeView = document.getElementById('routeView');
    const groups = state.groups;
    const candidates = state.candidates;
    const rows = candidates.map((candidate) => `<tr><td>${escapeHtml(accountLabel(summary, candidate.source_account_id))}</td><td>${escapeHtml(accountLabel(summary, candidate.destination_account_id))}</td><td class="n">${quantity(candidate.source_amount)} ${escapeHtml(assetSymbol(summary, candidate.source_asset_id))}</td><td class="n">${candidate.score} / 100</td><td class="n">${escapeHtml(candidate.status)}</td></tr>`).join('');
    routeView.innerHTML = `<div class="route-header"><div><h1>转账审核</h1><p>只展示由真实账本流水产生的匹配结果。</p></div></div><div class="metric-grid">${metric('待审核', String(candidates.length), '真实匹配候选')}${metric('已匹配', String(groups.length), '真实 Transfer Groups')}</div><div class="view-grid"><article class="view-panel span-12"><header><div><h2>待审核队列</h2><p>确认后才会携带成本，不产生已实现盈亏。</p></div></header>${rows ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>来源</th><th>去向</th><th class="n">数量</th><th class="n">置信度</th><th class="n">状态</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="live-empty">当前没有真实转账匹配候选。</div>'}</article></div>`;
  }

  function renderTransactionRoute() {
    const routeView = document.getElementById('routeView');
    const rows = state.events.map((event) => `<tr><td>${escapeHtml(formatDate(event.occurred_at))}</td><td>${escapeHtml(event.source)}</td><td>${escapeHtml(event.event_type)}</td><td class="n">${event.entries.length}</td><td class="n">${escapeHtml(event.status)}</td></tr>`).join('');
    routeView.innerHTML = `<div class="route-header"><div><h1>交易流水</h1><p>只展示同步后写入标准化账本的真实事件。</p></div></div><div class="view-grid"><article class="view-panel span-12"><header><div><h2>标准化事件</h2><p>Raw Event 与 Ledger Event 分层保留。</p></div></header>${rows ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>来源</th><th>事件</th><th class="n">分录数</th><th class="n">状态</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="live-empty">当前没有真实账本事件。请先连接账户并同步。</div>'}</article></div>`;
  }

  function renderLiveRoute() {
    const summary = state.summary;
    const route = location.hash.replace('#', '') || 'dashboard';
    if (route === 'dashboard' || route === 'settings') return;
    if (!summary) {
      renderRouteEmpty(route);
      return;
    }
    const routeView = document.getElementById('routeView');
    const calculated = summary.assets.reduce((sum, asset) => sum + (decimal(asset.calculated_cost_usd) || 0), 0);
    const effective = summary.assets.reduce((sum, asset) => sum + (decimal(asset.effective_cost_usd) || 0), 0);
    const lots = summary.assets.reduce((sum, asset) => sum + asset.open_lot_count, 0);
    const period = Object.fromEntries(summary.periods.map((item) => [item.key, item]));
    if (route === 'portfolio') {
      routeView.innerHTML = `<div class="route-header"><div><h1>资产组合</h1><p>真实净值已按 Spot、Cash、Perp 与 DeFi 分层；内部转账不影响总 NAV。</p></div></div><div class="metric-grid">${metric('Total net worth', currency(summary.total_net_worth_usd), summary.health.valuation_complete ? '估值完整' : '存在缺失价格')}${metric('Spot + Cash', currency(decimal(summary.spot_value_usd) + decimal(summary.cash_usd)), '现货与现金')}${metric('Perp equity', currency(summary.perp_equity_usd), '衍生品账户权益')}${metric('30D investment PnL', currency(period['30D']?.pnl_usd, true), '已排除外部资金流', classFor(period['30D']?.pnl_usd))}</div><div class="view-grid"><article class="view-panel span-4"><header><div><h2>产品构成</h2><p>按净值口径</p></div></header>${allocationList(summary.product_allocation)}</article><article class="view-panel span-4"><header><div><h2>账户构成</h2><p>当前账户权益</p></div></header>${allocationList(summary.account_allocation)}</article><article class="view-panel span-4"><header><div><h2>链与平台</h2><p>链上与交易所分布</p></div></header>${allocationList(summary.chain_allocation)}</article><article class="view-panel span-12"><header><div><h2>持仓汇总</h2><p>Effective Cost 是未实现盈亏的唯一成本口径。</p></div></header><div class="table-wrap"><table class="data-table"><thead><tr><th>资产</th><th class="n">持有</th><th class="n">有效成本</th><th class="n">市值</th><th class="n">未实现 PnL</th><th class="n">模式</th></tr></thead><tbody>${assetRows(summary.assets)}</tbody></table></div></article></div>`;
    } else if (route === 'assets') {
      routeView.innerHTML = `<div class="route-header"><div><h1>资产</h1><p>统一 Asset ID 后的真实持仓、成本、价格和账户分布。</p></div></div><div class="metric-grid">${metric('Tracked assets', String(summary.assets.length), '当前有余额的资产')}${metric('Cost coverage', percentage(summary.health.cost_coverage_percent), `${summary.health.valued_positions} / ${summary.health.total_positions} 项`)}${metric('Unknown deposits', String(summary.health.unknown_deposits), '等待分类或补充成本')}${metric('Open cost lots', String(lots), '可追溯到原始事件')}</div><div class="view-grid"><article class="view-panel span-12"><header><div><h2>资产账本</h2><p>按真实市场价值排序。</p></div></header><div class="table-wrap"><table class="data-table"><thead><tr><th>资产</th><th class="n">持有</th><th class="n">有效成本</th><th class="n">市值</th><th class="n">未实现 PnL</th><th class="n">模式</th></tr></thead><tbody>${assetRows(summary.assets)}</tbody></table></div></article></div>`;
    } else if (route === 'accounts') {
      const complete = summary.accounts.filter((account) => account.valuation_complete).length;
      routeView.innerHTML = `<div class="route-header"><div><h1>账户</h1><p>账户权益、同步时间与估值完整性来自真实同步记录。</p></div><div class="route-actions"><a class="action primary" href="/connections.html">添加账户</a></div></div><div class="metric-grid">${metric('Connected', String(summary.accounts.length), '只读账户')}${metric('Valued', String(complete), '已完成美元估值')}${metric('Needs attention', String(summary.accounts.length - complete), '数据或价格待补')}${metric('Total equity', currency(summary.total_net_worth_usd), 'Portfolio NAV')}</div><div class="view-grid"><article class="view-panel span-12"><header><div><h2>账户清单</h2><p>Spot、Cash 与 Perp Equity 已分层聚合。</p></div></header>${accountList(summary.accounts)}</article></div>`;
    } else if (route === 'transfers') {
      renderTransferRoute(summary);
    } else if (route === 'transactions') {
      renderTransactionRoute();
    } else if (route === 'ledger') {
      routeView.innerHTML = `<div class="route-header"><div><h1>成本账本</h1><p>Calculated Cost、Manual Override 与原始数据三层保留。</p></div></div><div class="metric-grid">${metric('Effective cost', currency(effective), '当前盈亏口径')}${metric('Calculated cost', currency(calculated), '由交易流水重建')}${metric('Manual delta', currency(effective - calculated, true), '人工覆盖影响', classFor(effective - calculated))}${metric('Open cost lots', String(lots), escapeHtml(summary.cost_method))}</div><div class="view-grid"><article class="view-panel span-12"><header><div><h2>有效成本</h2><p>不修改任何 Raw Event。</p></div></header><div class="table-wrap"><table class="data-table"><thead><tr><th>资产</th><th class="n">持有</th><th class="n">有效成本</th><th class="n">市值</th><th class="n">未实现 PnL</th><th class="n">模式</th></tr></thead><tbody>${assetRows(summary.assets)}</tbody></table></div></article></div>`;
    } else if (route === 'pnl') {
      routeView.innerHTML = `<div class="route-header"><div><h1>盈亏分析</h1><p>期间收益使用 Ending NAV − Starting NAV − Net External Flow。</p></div></div><div class="metric-grid">${metric('Today PnL', currency(period['1D']?.pnl_usd, true), period['1D']?.complete ? '已排除外部现金流' : '需要期初快照', classFor(period['1D']?.pnl_usd))}${metric('7D PnL', currency(period['7D']?.pnl_usd, true), period['7D']?.complete ? '已排除外部现金流' : '需要历史快照', classFor(period['7D']?.pnl_usd))}${metric('30D PnL', currency(period['30D']?.pnl_usd, true), period['30D']?.complete ? '已排除外部现金流' : '需要历史快照', classFor(period['30D']?.pnl_usd))}${metric('All time PnL', currency(summary.all_time_pnl_usd, true), 'Realized + Unrealized', classFor(summary.all_time_pnl_usd))}</div><div class="view-grid"><article class="view-panel span-7"><header><div><h2>净值历史</h2><p>Portfolio Snapshots</p></div></header>${historyChart(summary.nav_history)}</article><article class="view-panel span-5"><header><div><h2>PnL 构成</h2><p>Spot 与 Perp 保持独立计算</p></div></header><div class="split-stat"><div><small>Realized</small><b class="${classFor(summary.realized_pnl_usd)}">${currency(summary.realized_pnl_usd, true)}</b></div><div><small>Unrealized</small><b class="${classFor(summary.unrealized_pnl_usd)}">${currency(summary.unrealized_pnl_usd, true)}</b></div><div><small>Fees</small><b class="loss">${currency(summary.fee_expense_usd === null ? null : -decimal(summary.fee_expense_usd), true)}</b></div><div><small>Funding</small><b class="${classFor(summary.funding_pnl_usd)}">${currency(summary.funding_pnl_usd, true)}</b></div></div></article></div>`;
    } else if (route === 'exposure') {
      routeView.innerHTML = `<div class="route-header"><div><h1>净敞口</h1><p>Spot 与 Perp 的 Gross Long、Gross Short 和 Net Exposure。</p></div></div><div class="metric-grid">${metric('Gross long', currency(summary.gross_long_usd), 'Spot + Perp Long')}${metric('Gross short', currency(summary.gross_short_usd), 'Perp Short')}${metric('Net exposure', currency(summary.net_exposure_usd, true), 'Long − Short', classFor(summary.net_exposure_usd))}${metric('Margin usage', percentage(summary.margin_usage_percent), '衍生品权益口径')}</div><div class="view-grid"><article class="view-panel span-12"><header><div><h2>按资产敞口</h2><p>中心线左侧为 Short，右侧为 Long。</p></div></header>${exposureList(summary)}</article></div>`;
    } else if (route === 'analytics') {
      routeView.innerHTML = `<div class="route-header"><div><h1>分析</h1><p>历史净值和账户贡献均来自 Portfolio Snapshots 与当前账户权益。</p></div></div><div class="view-grid"><article class="view-panel span-7"><header><div><h2>净值与投资回报</h2><p>外部现金流已从期间表现中剔除。</p></div></header>${historyChart(summary.nav_history)}</article><article class="view-panel span-5"><header><div><h2>账户构成</h2><p>当前净值贡献，不等同于期间 PnL 归因。</p></div></header>${allocationList(summary.account_allocation)}</article></div>`;
    }
  }

  async function loadDashboard() {
    primeLoadingState();
    try {
      const portfolios = await window.BagsAuth.api('/portfolios');
      if (!portfolios.length) {
        renderEmpty('请先创建 Portfolio，并完成账户同步与成本重算。');
        return;
      }
      const saved = localStorage.getItem('bags_portfolio_id');
      const portfolio = portfolios.find((item) => item.id === saved) || portfolios[0];
      state.portfolioId = portfolio.id;
      localStorage.setItem('bags_portfolio_id', portfolio.id);
      const summary = await window.BagsAuth.api(`/dashboard/portfolios/${portfolio.id}/summary`);
      const [groups, candidates, events] = await Promise.all([
        window.BagsAuth.api(`/transfers/portfolios/${portfolio.id}/groups?limit=20`).catch(() => []),
        window.BagsAuth.api(`/transfers/portfolios/${portfolio.id}/candidates?status=needs_review&limit=100`).catch(() => []),
        window.BagsAuth.api(`/ledger/events?portfolio_id=${portfolio.id}&limit=100`).catch(() => []),
      ]);
      state.summary = summary;
      state.groups = groups;
      state.candidates = candidates;
      state.events = events;
      renderHome(summary, groups, candidates);
      renderLiveRoute();
    } catch (error) {
      const missingRun = error.status === 422 && String(error.message).includes('Cost Basis');
      renderEmpty(missingRun ? '请先在成本账本执行一次 Phase 6 成本重算。' : 'Dashboard 数据加载失败，请确认后端服务和数据库迁移状态后重试。');
      document.querySelector('.mode').classList.add('live-error');
    }
  }

  window.BagsRenderDashboardRoute = renderLiveRoute;
  primeLoadingState();
  renderLiveRoute();
  document.addEventListener('DOMContentLoaded', loadDashboard);
  addEventListener('hashchange', () => setTimeout(renderLiveRoute, 0));
})();
