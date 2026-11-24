/**
 * Battle Scoreboard (Bảng Điểm KDA Toàn Trận) - Hiển thị khi nhấn phím 'G'
 * Thiết kế giao diện esport đối kháng theo 2 phe (Camp 1 / Phe Xanh vs Camp 2 / Phe Đỏ)
 */

export function buildScoreboardModel(state, catalog = {}) {
  const characters = new Map((state?.characters || []).map(c => [c.accountId, c]));
  const actors = state?.actors || {};
  const statsMap = state?.stats || {};
  const selfObj = actors[String(state?.selfObjectId)];
  const selfAccountId = selfObj?.accountId;

  const score1 = Number(state?.scores?.camp1 ?? 0);
  const score2 = Number(state?.scores?.camp2 ?? 0);

  const seenAccounts = new Set();
  const rows = [];

  for (const act of Object.values(actors)) {
    if (!act || act.removed || (act.camp !== 1 && act.camp !== 2) || !Number.isInteger(act.accountId)) continue;
    if (seenAccounts.has(act.accountId)) continue;
    seenAccounts.add(act.accountId);
    rows.push(extractRow(act, characters.get(act.accountId), statsMap[String(act.accountId)], selfAccountId, catalog));
  }

  for (const char of (state?.characters || [])) {
    if (!char || (char.camp !== 1 && char.camp !== 2) || !Number.isInteger(char.accountId)) continue;
    if (seenAccounts.has(char.accountId)) continue;
    seenAccounts.add(char.accountId);
    rows.push(extractRow(char, char, statsMap[String(char.accountId)], selfAccountId, catalog));
  }

  const camp1 = rows.filter(r => r.camp === 1);
  const camp2 = rows.filter(r => r.camp === 2);

  const sorter = (a, b) => b.kills - a.kills || b.damage - a.damage;
  camp1.sort(sorter);
  camp2.sort(sorter);

  return { score1, score2, camp1, camp2 };
}

function extractRow(primary, char, statObj, selfAccountId, catalog) {
  const accountId = primary.accountId;
  const camp = primary.camp;
  const name = primary.name || char?.name || (accountId === 0 ? 'Bot' : `Người chơi ${accountId}`);
  const tankId = primary.tankId ?? char?.tankId ?? 10000001;
  const tankLevel = primary.tankLevel ?? char?.tankLevel ?? statObj?.battleLevel ?? 1;
  const tankName = catalog?.tanks?.[String(tankId)]?.name || catalog?.tanks?.[String(tankId)]?.nativeName || 'Tank';

  const stats = statObj?.stats || [];
  // stats[0]: deaths, stats[1]: kills (tank), stats[2]: kills (total), stats[3]: assists, stats[6]: damage
  const deaths = stats[0] ?? 0;
  const kills = stats[1] ?? stats[2] ?? 0;
  const assists = stats[3] ?? 0;
  const damage = stats[6] ?? 0;

  const isSelf = accountId === selfAccountId;

  return {
    accountId,
    camp,
    name,
    tankId,
    tankName,
    tankLevel,
    kills,
    deaths,
    assists,
    damage,
    isSelf,
    ping: isSelf ? (primary.delay ?? 25) : (primary.delay ?? 30)
  };
}

export class BattleScoreboard {
  constructor(options = {}) {
    this.host = options.host || document.body;
    this.visible = false;
    this.root = document.createElement('div');
    this.root.className = 'm8-scoreboard-overlay';
    this.root.hidden = true;

    this.modal = document.createElement('div');
    this.modal.className = 'm8-scoreboard-modal';
    this.root.append(this.modal);

    // Click outside to close
    this.root.addEventListener('click', (e) => {
      if (e.target === this.root) {
        this.toggle(false);
      }
    });

    this.host.append(this.root);
  }

  toggle(force, state, catalog) {
    const wasVisible = this.visible;
    this.visible = typeof force === 'boolean' ? force : !this.visible;
    this.root.hidden = !this.visible;
    if (this.visible && state) {
      this.render(state, catalog);
    }
    return wasVisible;
  }

  update(state, catalog) {
    if (!this.visible || !state) return;
    this.render(state, catalog);
  }

  render(state, catalog) {
    const model = buildScoreboardModel(state, catalog);
    this.modal.replaceChildren();

    // 1. Header
    const header = document.createElement('div');
    header.className = 'm8-scoreboard-header';

    const title = document.createElement('div');
    title.className = 'm8-scoreboard-title';
    title.textContent = 'BẢNG ĐIỂM CHIẾN TRƯỜNG';

    const scoreBar = document.createElement('div');
    scoreBar.className = 'm8-scoreboard-scorebar';
    scoreBar.innerHTML = `
      <span class="m8-sb-camp1-score">PHE XANH: <strong>${model.score1}</strong></span>
      <span class="m8-sb-vs">VS</span>
      <span class="m8-sb-camp2-score">PHE ĐỎ: <strong>${model.score2}</strong></span>
    `;

    const closeBtn = document.createElement('button');
    closeBtn.className = 'm8-scoreboard-close';
    closeBtn.textContent = '✕';
    closeBtn.title = 'Đóng bảng điểm (G / Esc)';
    closeBtn.onclick = () => this.toggle(false);

    header.append(title, scoreBar, closeBtn);
    this.modal.append(header);

    // 2. Camp 1 Section (Blue / Green)
    const camp1Section = this.renderCampSection(model.camp1, 'camp1', 'PHE XANH (CAMP 1)');
    this.modal.append(camp1Section);

    // 3. Camp 2 Section (Red)
    const camp2Section = this.renderCampSection(model.camp2, 'camp2', 'PHE ĐỎ (CAMP 2)');
    this.modal.append(camp2Section);

    // 4. Footer
    const footer = document.createElement('div');
    footer.className = 'm8-scoreboard-footer';
    footer.innerHTML = '<span>Nhấn <kbd>G</kbd> hoặc <kbd>Esc</kbd> để Bật / Tắt bảng điểm</span>';
    this.modal.append(footer);
  }

  renderCampSection(rows, campClass, titleText) {
    const section = document.createElement('div');
    section.className = `m8-scoreboard-camp ${campClass}`;

    const table = document.createElement('table');
    table.className = 'm8-scoreboard-table';

    // Head
    const thead = document.createElement('thead');
    thead.innerHTML = `
      <tr>
        <th class="col-player">NGƯỜI CHƠI</th>
        <th class="col-tank">TANK</th>
        <th class="col-level">CẤP</th>
        <th class="col-kda">K / D / A</th>
        <th class="col-damage">SÁT THƯƠNG</th>
        <th class="col-ping">PING</th>
      </tr>
    `;
    table.append(thead);

    // Body
    const tbody = document.createElement('tbody');
    if (!rows.length) {
      const emptyTr = document.createElement('tr');
      emptyTr.innerHTML = '<td colspan="6" class="m8-sb-empty">Chưa có người chơi</td>';
      tbody.append(emptyTr);
    } else {
      for (const r of rows) {
        const tr = document.createElement('tr');
        if (r.isSelf) tr.classList.add('is-self');
        tr.innerHTML = `
          <td class="col-player"><span class="m8-sb-name">${r.name}${r.isSelf ? ' <small>(Bạn)</small>' : ''}</span></td>
          <td class="col-tank"><span class="m8-sb-tank">${r.tankName}</span></td>
          <td class="col-level"><span class="m8-sb-level">Lv.${r.tankLevel}</span></td>
          <td class="col-kda"><strong class="m8-sb-kda">${r.kills} / ${r.deaths} / ${r.assists}</strong></td>
          <td class="col-damage"><span class="m8-sb-dmg">${r.damage.toLocaleString()}</span></td>
          <td class="col-ping"><span class="m8-sb-ping">${r.ping}ms</span></td>
        `;
        tbody.append(tr);
      }
    }
    table.append(tbody);
    section.append(table);
    return section;
  }
}
