/* RepoOnboard 前端 · Vue 3 + AntV G6(任务定位) + markmap(思维导图)
 *
 * 流程：首页输入仓库地址 → POST /api/analyze → 结果页三视图
 * 所有分析都在本地 Python 服务里完成，前端只负责渲染。 */
(function () {
  'use strict';

  const C = {
    accent: '#e8590c', accentSoft: '#f0b27a', gt: '#2f9e44',
    test: '#adb5bd', vendor: '#b197fc', aux: '#74c0fc', edge: '#cbd5e1'
  };

  function isHot(n, task) {
    if (!task) return false;
    return task.top5.some(f => f.path === n.id)
      || (task.gt || []).indexOf(n.id) >= 0
      || (task.sub || []).indexOf(n.id) >= 0;
  }

  function nodeColor(n, task) {
    if (task) {
      const inTop = task.top5.some(f => f.path === n.id);
      const inGt = (task.gt || []).indexOf(n.id) >= 0;
      if (inTop && inGt) return C.gt;
      if (inTop) return C.accent;
      if (inGt) return C.gt;
      if ((task.sub || []).indexOf(n.id) >= 0) return C.accentSoft;
      return '#d7dde4';
    }
    if (n.is_test) return C.test;
    if (n.is_vendor) return C.vendor;
    if (n.is_aux) return C.aux;
    return C.accent;
  }

  const STEPS = ['正在下载仓库…', 'AST 建图（解析 import / 调用 / 继承）…',
                 '计算结构重要性与主干路径…', '整理返回数据…'];

  const app = Vue.createApp({
    data() {
      return {
        stage: 'home',              // home | loading | result
        index: { repos: [], metrics: {}, explains: {} },
        current: null,
        homeUrl: 'https://github.com/pallets/itsdangerous',
        homeTask: '',
        errMsg: '',
        loadingStep: STEPS[0],
        elapsed: 0,
        wantedTab: null,

        tab: 'paths',
        q: '',
        sel: null,
        taskId: null,
        hoverPath: '',
        taskPool: [],
        _gTask: null, _mm: null, _baseSize: null,
        _searchTimer: null, _resizeTimer: null, _tick: null
      };
    },

    computed: {
      data() { return this.current; },
      paths() { return (this.current && this.current.paths) || []; },
      repoTasks() { return (this.current && this.current.tasks) || []; },
      curTask() { return this.repoTasks.find(t => t.id === this.taskId) || null; },
      visibleTabs() {
        const ts = [{ key: 'mindmap', label: '思维导图' }, { key: 'paths', label: '主干路径' }];
        if (this.repoTasks.length) ts.push({ key: 'task', label: '任务定位' });
        return ts;
      },
      repoCore() {
        if (!this.current) return [];
        const core = this.current.nodes.filter(n => !n.is_test && !n.is_vendor && !n.is_aux);
        return core.slice(0, 18);
      },
      listFiles() {
        if (this.tab === 'task') return this.taskPool;
        if (this.tab === 'paths') return this.repoCore;
        return [];
      },
      hitCount() {
        if (!this.curTask || !this.curTask.gt) return 0;
        const gt = this.curTask.gt;
        return this.curTask.top5.filter(f => gt.indexOf(f.path) >= 0).length;
      },
      selExplain() {
        if (!this.sel || !this.current) return '';
        return (this.index.explains || {})[this.current.name + '::' + this.sel.id] || '';
      }
    },

    watch: {
      q() {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.applySearch(), 250);
      }
    },

    mounted() {
      window.addEventListener('resize', () => this.onResize());
      this.boot();

      const sp = new URLSearchParams(location.search);
      const want = sp.get('repo');
      if (want) {
        this.useExample(want, sp.get('tab'));
        return;
      }
      const direct = sp.get('url') || sp.get('u');
      if (direct) {                     // 支持 ?url= 直接解析并分享链接
        this.homeUrl = direct;
        this.wantedTab = sp.get('tab');
        const t = sp.get('task');
        if (t) this.homeTask = t;
        this.startAnalyze();
      }
    },

    methods: {
      /* ================= 数据 ================= */
      async boot() {
        try {
          const r = await fetch('data/index.json', { cache: 'no-cache' });
          if (r.ok) this.index = await r.json();
        } catch (e) {
          this.errMsg = '读不到示例数据（data/index.json）。服务是否已启动？';
        }
      },

      async fetchJSON(url) {
        const r = await fetch(url, { cache: 'no-cache' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      },

      async useExample(slug, tab) {
        this.errMsg = '';
        try {
          const data = await this.fetchJSON('data/' + slug + '.json');
          this.enterResult(data, tab);
        } catch (e) {
          this.errMsg = '示例数据缺失：' + slug + '（请先运行 python src/build_web.py）';
        }
      },

      async startAnalyze() {
        if (this.stage === 'loading') return;
        const url = (this.homeUrl || '').trim();
        if (!url) { this.errMsg = '请先填入仓库地址'; return; }

        this.errMsg = '';
        this.stage = 'loading';
        this.elapsed = 0;
        let i = 0;
        this.loadingStep = STEPS[0];
        this._tick = setInterval(() => {
          this.elapsed += 1;
          i = Math.min(i + 1, STEPS.length - 1);
          this.loadingStep = STEPS[i];
        }, 1500);

        try {
          const r = await fetch('api/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url, task: (this.homeTask || '').trim() || null })
          });
          const body = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
          this.enterResult(body, this.wantedTab || 'paths');
        } catch (e) {
          this.stage = 'home';
          const msg = String((e && e.message) || e);
          this.errMsg = /Failed to fetch|NetworkError|Load failed/i.test(msg)
            ? '连不上本地服务。请先在项目目录运行：python src/server.py'
            : msg;
        } finally {
          clearInterval(this._tick);
          this._tick = null;
        }
      },

      enterResult(data, tab) {
        this.current = data;
        this.taskPool = [];
        this.sel = null;
        this.hoverPath = '';
        const ts = data.tasks || [];
        this.taskId = ts.length ? ts[0].id : null;
        const okTabs = ['mindmap', 'paths', 'task'];
        let want = (tab && okTabs.indexOf(tab) >= 0) ? tab : this.tab;
        if (okTabs.indexOf(want) < 0) want = 'paths';
        if (want === 'task' && !ts.length) want = 'paths';
        this.tab = want;
        this.stage = 'result';
        this.renderActive();
      },

      backHome() {
        this.stage = 'home';
        this.current = null;
        this.destroyGraph();
      },

      switchTab(k) {
        this.tab = k;
        this.sel = null;
        this.hoverPath = '';
        this.renderActive();
      },

      renderActive() {
        this.$nextTick(() => {
          if (!this.current) return;
          if (this.tab === 'mindmap') this.renderMindmap();
          else if (this.tab === 'task') this.renderTask();
        });
      },

      onTaskChange() {
        this.sel = null;
        this.$nextTick(() => this.renderTask());
      },

      onResize() {
        if (this.tab === 'mindmap' && this._mm) { try { this._mm.fit(); } catch (e) {} }
        if (this.tab !== 'task') return;
        clearTimeout(this._resizeTimer);
        this._resizeTimer = setTimeout(() => this.renderTask(), 180);
      },

      destroyGraph() {
        if (this._gTask && !this._gTask.get('destroyed')) {
          try { this._gTask.destroy(); } catch (e) {}
        }
        this._gTask = null;
        this._baseSize = null;
      },

      /* ================= 思维导图 ================= */
      renderMindmap() {
        this.destroyGraph();
        const svg = document.getElementById('mm');
        if (!svg || !this.current) return;
        const box = svg.parentElement.getBoundingClientRect();
        svg.setAttribute('width', Math.max(320, box.width - 2));
        svg.setAttribute('height', Math.max(320, box.height - 44));
        svg.innerHTML = '';

        const mm = window.markmap || {};
        if (!mm.Transformer || !mm.Markmap) {
          svg.parentElement.insertAdjacentHTML('beforeend',
            '<div style="color:#c00;padding:12px">markmap 未加载：检查 web/vendor/markmap-*.js</div>');
          return;
        }
        try {
          const t = new mm.Transformer();
          const { root } = t.transform(this.current.mindmap || '# empty');
          this._mm = mm.Markmap.create(svg, { duration: 0, autoFit: true }, root);
          if (this._mm && this._mm.fit) this._mm.fit();
        } catch (e) {
          svg.parentElement.insertAdjacentHTML('beforeend',
            '<div style="color:#c00;padding:12px">markmap 渲染失败：' + e.message + '</div>');
        }
      },

      /* ================= 任务定位（G6 · 同心环） ================= */
      buildTaskPool(task) {
        if (!task || !this.current) return { pool: [], edges: [] };
        const keep = new Set();
        task.top5.forEach(f => keep.add(f.path));
        (task.gt || []).forEach(p => keep.add(p));
        (task.sub || []).forEach(p => keep.add(p));
        const pool = this.current.nodes.filter(n => keep.has(n.id));
        const ids = new Set(pool.map(n => n.id));
        const edges = this.current.edges.filter(e => ids.has(e.source) && ids.has(e.target));
        return { pool, edges };
      },

      ringPositions(pool) {
        const buckets = [[], [], []];
        pool.forEach((n, i) => buckets[i < 5 ? 0 : (i < 12 ? 1 : 2)].push(n));
        const SPACING = 30, base = [86, 200, 320];
        const radii = [0, 1, 2].map(ri => {
          const c = buckets[ri].length;
          return c ? Math.max(base[ri], (c * SPACING) / (2 * Math.PI)) : 0;
        });
        const pos = {};
        [0, 1, 2].forEach(ri => {
          const arr = buckets[ri], r = radii[ri];
          if (!arr.length) return;
          const offset = [-Math.PI / 2, 0, Math.PI / 14][ri];
          arr.forEach((n, k) => {
            const a = offset + (2 * Math.PI * k) / arr.length;
            pos[n.id] = { x: r * Math.cos(a), y: r * Math.sin(a) };
          });
        });
        return { pos, radii };
      },

      renderTask() {
        const el = document.getElementById('g6task');
        if (!el || !this.curTask) return;
        this.destroyGraph();
        el.innerHTML = '';

        const { pool, edges: poolEdges } = this.buildTaskPool(this.curTask);
        this.taskPool = pool;
        const task = this.curTask;

        const W = el.clientWidth || 800, H = el.clientHeight || 600;
        const { pos, radii } = this.ringPositions(pool);
        const usedR = radii.filter(r => r > 0);
        const maxR = Math.max.apply(null, (usedR.length ? usedR : [1]).concat([1]));
        let scale = (Math.min(W, H) - 110) / (2 * maxR);
        scale = Math.min(Math.max(scale, 0.8), 2.4);
        const cx = W / 2, cy = H / 2;

        const nodes = pool.map(n => {
          const hot = isHot(n, task);
          const p = pos[n.id] || { x: 0, y: 0 };
          return {
            id: n.id, x: cx + p.x * scale, y: cy + p.y * scale,
            label: n.label,
            size: (hot ? 26 : 12) * scale,
            style: { fill: nodeColor(n, task), stroke: '#fff', lineWidth: hot ? 2.5 : 1.2 },
            _raw: n
          };
        });

        const edges = poolEdges.map(e => ({
          source: e.source, target: e.target,
          style: { stroke: C.edge, lineWidth: Math.min(1 + e.weight * 0.35, 3), opacity: 0.6 }
        }));

        const graph = new G6.Graph({
          container: el, width: W, height: H,
          animate: false, fitView: false, layout: { type: 'preset' },
          defaultNode: {
            type: 'circle',
            labelCfg: { position: 'bottom', style: { fontSize: 10.5, fill: '#495057' } }
          },
          defaultEdge: { type: 'line' },
          modes: { default: ['drag-canvas', 'zoom-canvas', 'drag-node'] }
        });

        graph.data({ nodes, edges });
        graph.render();

        this._baseSize = {};
        nodes.forEach(n => { this._baseSize[n.id] = n.size; });
        graph.on('node:click', evt => {
          const m = evt.item.getModel();
          if (m._raw) this.sel = m._raw;
        });
        this._gTask = graph;
        setTimeout(() => this.applySearch(), 200);
      },

      /* ================= 列表 ↔ 视图 联动 ================= */
      hoverFile(nid) {
        this.hoverPath = nid;
        const g = this._gTask;
        if (this.tab !== 'task' || !g || g.get('destroyed') || !this._baseSize) return;
        try {
          g.getNodes().forEach(item => {
            const m = item.getModel();
            const on = m.id === nid;
            const base = this._baseSize[m.id] || 12;
            g.updateItem(item, {
              size: on ? base * 1.6 : base,
              style: Object.assign({}, m.style, {
                opacity: on ? 1 : 0.2,
                stroke: on ? C.accent : '#fff',
                lineWidth: on ? 3 : 1.2
              })
            });
          });
        } catch (e) {}
      },

      leaveFile() {
        this.hoverPath = '';
        const g = this._gTask;
        if (this.tab !== 'task' || !g || g.get('destroyed') || !this._baseSize) return;
        try {
          g.getNodes().forEach(item => {
            const m = item.getModel();
            const base = this._baseSize[m.id] || 12;
            g.updateItem(item, {
              size: base,
              style: Object.assign({}, m.style, { opacity: 1, stroke: '#fff', lineWidth: 1.2 })
            });
          });
        } catch (e) {}
      },

      selectPath(path) {
        this.sel = ((this.current && this.current.nodes) || []).find(n => n.id === path) || null;
      },

      pickFromList(nid) {
        this.sel = ((this.current && this.current.nodes) || []).find(n => n.id === nid) || null;
      },

      applySearch() {
        const g = this._gTask;
        if (!g || g.get('destroyed')) return;
        const q = (this.q || '').toLowerCase().trim();
        try {
          g.getNodes().forEach(item => {
            const m = item.getModel();
            const hit = q && m.id.toLowerCase().indexOf(q) >= 0;
            g.updateItem(item, {
              style: Object.assign({}, m.style, hit
                ? { stroke: C.accent, lineWidth: 3 }
                : { stroke: '#fff', lineWidth: 1.2 })
            });
          });
        } catch (e) {}
      },

      /* ================= 工具 ================= */
      typeLabel(n) {
        if (n.is_test) return '测试文件';
        if (n.is_vendor) return '内嵌第三方';
        if (n.is_aux) return '辅助脚本 / 文档';
        return '核心源码';
      },
      isGt(p) {
        if (!this.curTask || !this.curTask.gt) return false;
        return this.curTask.gt.indexOf(p) >= 0;
      }
    }
  });

  app.config.errorHandler = function (err, vm, info) {
    const box = document.createElement('pre');
    box.style.cssText = 'color:#c00;background:#fff;padding:12px;margin:0;'
      + 'font:12px Consolas,monospace;white-space:pre-wrap;position:relative;z-index:99999';
    box.textContent = 'Vue 运行错误 [' + info + ']：' + (err && err.message)
      + '\n' + (err && err.stack ? err.stack : '');
    document.body.insertBefore(box, document.body.firstChild);
  };

  try {
    app.mount('#app');
  } catch (e) {
    try { document.getElementById('app').removeAttribute('v-cloak'); } catch (_) {}
    const box = document.createElement('pre');
    box.style.cssText = 'color:#c00;background:#fff;padding:14px;font:12px Consolas,monospace;'
      + 'white-space:pre-wrap;position:relative;z-index:9999';
    box.textContent = '启动失败：' + (e && e.message) + '\n\n' + (e && e.stack ? e.stack : '');
    document.body.insertBefore(box, document.body.firstChild);
  }
})();
