/* RepoOnboard 前端 · Vue 3 + AntV G6(依赖图 / 任务定位) + markmap(思维导图)
 *
 * 流程：首页输入仓库地址 → POST /api/analyze → 结构视图与仓库问答
 * 所有分析都在本地 Python 服务里完成，前端只负责渲染。 */
(function () {
  'use strict';

  const C = {
    accent: '#e8590c', accentSoft: '#f0b27a', gt: '#2f9e44',
    test: '#adb5bd', vendor: '#b197fc', aux: '#74c0fc', edge: '#cbd5e1'
  };
  const DEPENDENCY_TYPES = ['imports', 'calls', 'inherits'];

  function dependencyEdge(edge) {
    const types = {}, sourceTypes = edge.types || {};
    let weight = 0;
    DEPENDENCY_TYPES.forEach(type => {
      const count = Number(sourceTypes[type]) || 0;
      if (count > 0) { types[type] = count; weight += count; }
    });
    return weight > 0
      ? { source: edge.source, target: edge.target, weight, types }
      : null;
  }

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
        errMsg: '',
        loadingStep: STEPS[0],
        elapsed: 0,
        wantedTab: null,

        tab: 'mindmap',
        graphMode: 'compact',
        graphNodeCount: 0,
        graphEdgeCount: 0,
        q: '',
        sel: null,
        taskId: null,
        hoverPath: '',
        taskPool: [],
        wantedQuestion: '',
        wantedTask: '',
        qaId: '',
        qaVariantKey: '',
        qaOpenPath: '',
        taskText: '',
        taskBusy: false,
        taskError: '',
        taskResult: null,
        _gTask: null, _gGraph: null, _mm: null, _baseSize: null,
        _searchTimer: null, _resizeTimer: null, _tick: null
      };
    },

    computed: {
      data() { return this.current; },
      paths() { return (this.current && this.current.paths) || []; },
      repoTasks() { return (this.current && this.current.tasks) || []; },
      curTask() { return this.repoTasks.find(t => t.id === this.taskId) || null; },
      visibleTabs() {
        return [
          { key: 'mindmap', label: '思维导图' },
          { key: 'graph', label: '依赖图' },
          { key: 'paths', label: '主干路径' },
          { key: 'qa', label: '问答' }
        ];
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
      },

      /* ---------- 主干路径 ---------- */
      pathSummary() {
        const s = (this.current && this.current.path_summary) || null;
        return s || { paths: this.paths.length, files: 0, entries: 0, shared: [] };
      },
      sharedCount() { return (this.pathSummary.shared || []).length; },

      /* ---------- 仓库速答 ---------- */
      qaQuestions() {
        return (this.current && this.current.qa && this.current.qa.questions) || [];
      },
      qaGroups() {
        return (this.current && this.current.qa && this.current.qa.groups) || [];
      },
      curQa() {
        const list = this.qaQuestions;
        return list.find(q => q.id === this.qaId) || list[0] || null;
      },
      qaVariant() {
        const q = this.curQa;
        if (!q || !q.variants || !q.variants.length) return null;
        return q.variants.find(v => v.key === this.qaVariantKey) || q.variants[0];
      },
      qaAnswerHtml() {
        const v = this.qaVariant;
        return (v && v.answer) || (this.curQa ? this.curQa.answer : '');
      },
      qaBullets() {
        const v = this.qaVariant;
        return (v && v.bullets) || (this.curQa && this.curQa.bullets) || [];
      },
      qaFiles() {
        const v = this.qaVariant;
        return (v && v.files) || (this.curQa && this.curQa.files) || [];
      },
      qaOpenNode() {
        if (!this.qaOpenPath || !this.current) return null;
        return (this.current.nodes || []).find(n => n.id === this.qaOpenPath) || null;
      }
    },

    watch: {
      q() {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.applySearch(), 250);
      },
      qaVariantKey() { this.qaOpenPath = ''; }
    },

    mounted() {
      window.addEventListener('resize', () => this.onResize());
      this.boot();

      const sp = new URLSearchParams(location.search);
      this.wantedQuestion = sp.get('q') || '';
      this.wantedTask = sp.get('task') || '';
      const want = sp.get('repo');
      if (want) {
        this.useExample(want, sp.get('tab'));
        return;
      }
      const direct = sp.get('url') || sp.get('u');
      if (direct) {                     // 支持 ?url= 直接解析并分享链接
        this.homeUrl = direct;
        this.wantedTab = sp.get('tab');
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
            body: JSON.stringify({ url: url })
          });
          const body = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
          this.enterResult(body, this.wantedTab || 'mindmap');
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
        this.graphMode = 'compact';
        this.sel = null;
        this.hoverPath = '';
        this.taskText = '';
        this.taskBusy = false;
        this.taskError = '';
        this.taskResult = null;
        const qs = (data.qa && data.qa.questions) || [];
        this.qaId = qs.length ? qs[0].id : '';
        if (this.wantedQuestion && qs.some(q => q.id === this.wantedQuestion)) {
          this.qaId = this.wantedQuestion;      // 支持 ?q=deps 直达某个问题，方便演示
        }
        this.wantedQuestion = '';
        const firstQa = qs.find(q => q.id === this.qaId);
        this.qaVariantKey = (firstQa && firstQa.variants && firstQa.variants.length)
          ? firstQa.variants[0].key : '';
        this.qaOpenPath = '';
        const ts = data.tasks || [];
        this.taskId = ts.length ? ts[0].id : null;
        if (this.wantedTask) {
          const hit = ts.find(t => t.id === this.wantedTask);
          if (hit) { this.qaId = 'task'; this.showTask(hit); }
          this.wantedTask = '';
        }
        const okTabs = ['mindmap', 'graph', 'paths', 'qa'];
        let want = (tab && okTabs.indexOf(tab) >= 0) ? tab : this.tab;
        if (okTabs.indexOf(want) < 0) want = 'mindmap';
        this.tab = want;
        this.stage = 'result';
        this.renderActive();
      },

      backHome() {
        this.stage = 'home';
        this.current = null;
        this.taskResult = null;
        this.taskError = '';
        this.destroyGraph();
      },

      switchTab(k) {
        this.tab = k;
        this.sel = null;
        this.hoverPath = '';
        this.renderActive();
      },

      /* ---------- 速答：选问题 / 换视角 ---------- */
      qaByGroup(key) {
        return this.qaQuestions.filter(q => q.group === key);
      },

      pickQuestion(id) {
        if (this.qaId === id) return;
        this.qaId = id;
        this.qaOpenPath = '';
        const q = this.qaQuestions.find(x => x.id === id);
        this.qaVariantKey = (q && q.variants && q.variants.length) ? q.variants[0].key : '';
      },

      viaText(via) {
        if (!via) return '';
        const parts = [];
        if (via.calls) parts.push('调用 ' + via.calls);
        if (via.imports) parts.push('导入 ' + via.imports);
        if (via.inherits) parts.push('继承 ' + via.inherits);
        return parts.length ? parts.join(' · ') : '依赖';
      },

      roleClass(role) {
        return { '入口': 'entry', '枢纽': 'hub', '核心': 'core', '叶子': 'leaf' }[role] || 'plain';
      },

      fmt3(x) { return Number(x || 0).toFixed(3); },
      fmt4(x) { return Number(x || 0).toFixed(4); },

      toggleQaFile(path) {
        this.qaOpenPath = (this.qaOpenPath === path) ? '' : path;
      },

      focusFile(path) {
        const node = ((this.current && this.current.nodes) || []).find(n => n.id === path);
        if (!node) return;
        this.hoverPath = '';
        this.tab = 'graph';
        this.sel = node;
        this.renderActive();
      },

      /* ---------- 速答：按任务找文件 ---------- */
      showTask(t) {
        this.taskError = '';
        this.taskResult = {
          title: t.id + ' · ' + t.issue_title,
          files: (t.top5 || []).map(f => ({
            path: f.path,
            reason: (t.gt && t.gt.indexOf(f.path) >= 0)
              ? '命中真实改动 · 融合得分 ' + Number(f.score).toFixed(4)
              : '融合得分 ' + Number(f.score).toFixed(4)
          })),
          note: (t.gt && t.gt.length)
            ? '真实答案（已合并 PR 改动的文件）：' + t.gt.join('、')
              + '　命中 ' + (t.top5 || []).filter(f => t.gt.indexOf(f.path) >= 0).length
              + '/' + t.gt.length
            : ''
        };
      },

      async runTask() {
        const question = (this.taskText || '').trim();
        if (!question || this.taskBusy) return;
        if (!this.current || !this.current._analysis_id) {
          this.taskError = '自由检索需要实时解析的仓库：这个仓库来自预生成示例数据，'
            + '请在上面点一个内置 issue，或回到首页输入仓库地址重新解析。';
          return;
        }
        this.taskError = '';
        this.taskBusy = true;
        this.taskResult = null;
        try {
          const response = await fetch('api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ analysis_id: this.current._analysis_id, question })
          });
          const body = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(body.error || ('HTTP ' + response.status));
          this.taskResult = {
            title: question,
            files: (body.files || []).map(f => ({
              path: f.path, reason: f.reason || ('结构得分 ' + Number(f.score || 0).toFixed(3))
            })),
            note: body.engine ? '检索方式：' + body.engine : ''
          };
        } catch (e) {
          this.taskError = '这次没有完成检索：' + String((e && e.message) || e);
        } finally {
          this.taskBusy = false;
        }
      },

      renderActive() {
        this.$nextTick(() => {
          if (!this.current) return;
          if (this.tab === 'mindmap') this.renderMindmap();
          else if (this.tab === 'graph') this.renderDependencyGraph();
          else if (this.tab === 'task') this.renderTask();
          else this.destroyGraph();
        });
      },

      onTaskChange() {
        this.sel = null;
        this.$nextTick(() => this.renderTask());
      },

      onResize() {
        if (this.tab === 'mindmap' && this._mm) { try { this._mm.fit(); } catch (e) {} }
        if (this.tab !== 'task' && this.tab !== 'graph') return;
        clearTimeout(this._resizeTimer);
        this._resizeTimer = setTimeout(() => {
          if (this.tab === 'graph') this.renderDependencyGraph();
          else this.renderTask();
        }, 180);
      },

      destroyGraph() {
        [this._gTask, this._gGraph].forEach(graph => {
          if (graph && !graph.get('destroyed')) {
            try { graph.destroy(); } catch (e) {}
          }
        });
        this._gTask = null;
        this._gGraph = null;
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

      /* ================= 文件依赖图（G6） ================= */
      buildDependencyGraphData() {
        if (!this.current) return { nodes: [], edges: [] };
        const rawNodes = this.current.nodes || [];
        const rawEdges = this.current.edges || [];
        const rawById = new Map(rawNodes.map(n => [n.id, n]));

        if (this.graphMode === 'all' || !(this.current.view || []).length) {
          const ids = new Set(rawNodes.map(n => n.id));
          return {
            nodes: rawNodes.map(n => ({
              id: n.id, label: n.label || n.id, count: 1,
              score: Number(n.score) || 0, is_test: !!n.is_test,
              is_vendor: !!n.is_vendor, is_aux: !!n.is_aux,
              _raw: n, _group: null, viewIndex: null
            })),
            edges: rawEdges.filter(e => ids.has(e.source) && ids.has(e.target))
              .map(dependencyEdge).filter(Boolean)
          };
        }

        const groups = this.current.view || [];
        const groupByFile = new Map();
        const nodes = groups.map((group, i) => {
          const members = group.members || [];
          const raw = rawById.get(group.rep) || rawById.get(members[0]) || null;
          const count = Number(group.count) || members.length || 1;
          const category = group.cluster || '文件';
          const id = 'view-' + i;
          members.forEach(path => groupByFile.set(path, id));
          const shortCategory = category.replace(/[（）]/g, '');
          const label = count === 1 && raw ? raw.label : shortCategory + ' · ' + count;
          return {
            id, label, count, score: Number(group.score) || (raw && raw.score) || 0,
            is_test: category.indexOf('测试') >= 0 || !!(raw && raw.is_test),
            is_vendor: category.indexOf('第三方') >= 0 || !!(raw && raw.is_vendor),
            is_aux: category.indexOf('辅助') >= 0 || !!(raw && raw.is_aux),
            _raw: count === 1 ? raw : null,
            viewIndex: i,
            _group: { cluster: category, count, members, representative: raw && raw.id }
          };
        });

        const combined = new Map();
        rawEdges.forEach(e => {
          const relevant = dependencyEdge(e);
          if (!relevant) return;
          const source = groupByFile.get(e.source), target = groupByFile.get(e.target);
          if (!source || !target || source === target) return;
          const key = source + '\u0000' + target;
          let link = combined.get(key);
          if (!link) {
            link = { source, target, weight: 0, types: {} };
            combined.set(key, link);
          }
          link.weight += relevant.weight;
          Object.keys(relevant.types).forEach(type => {
            link.types[type] = (link.types[type] || 0) + relevant.types[type];
          });
        });
        return { nodes, edges: Array.from(combined.values()) };
      },

      setGraphMode(mode) {
        if (this.graphMode === mode) return;
        this.graphMode = mode;
        this.sel = null;
        this.renderActive();
      },

      graphPositions(nodes, edges, width, height) {
        const adjacent = new Map(nodes.map(n => [n.id, new Set()]));
        edges.forEach(e => {
          if (!adjacent.has(e.source) || !adjacent.has(e.target)) return;
          adjacent.get(e.source).add(e.target);
          adjacent.get(e.target).add(e.source);
        });
        const byId = new Map(nodes.map(n => [n.id, n]));
        const seen = new Set(), components = [];
        nodes.forEach(node => {
          if (seen.has(node.id)) return;
          const stack = [node.id], ids = [];
          seen.add(node.id);
          while (stack.length) {
            const id = stack.pop();
            ids.push(id);
            adjacent.get(id).forEach(next => {
              if (!seen.has(next)) { seen.add(next); stack.push(next); }
            });
          }
          const ordered = ids.map(id => byId.get(id)).sort((a, b) =>
            adjacent.get(b.id).size - adjacent.get(a.id).size || a.id.localeCompare(b.id));
          const points = {};
          points[ordered[0].id] = { x: 0, y: 0 };
          let radius = 0;
          const localLimit = Math.min(width, height) * 0.18;
          for (let i = 1; i < ordered.length; i++) {
            const r = Math.min(localLimit, 42 * Math.sqrt(i));
            const angle = (i - 1) * 2.399963229728653;
            points[ordered[i].id] = { x: r * Math.cos(angle), y: r * Math.sin(angle) };
            radius = Math.max(radius, r);
          }
          components.push({ nodes: ordered, points, radius });
        });

        components.sort((a, b) => b.nodes.length - a.nodes.length);
        const minDim = Math.min(width, height);
        const center = { x: width / 2, y: height / 2 };
        const positions = {};
        if (!components.length) return positions;
        const main = components[0];
        Object.keys(main.points).forEach(id => {
          positions[id] = { x: center.x + main.points[id].x, y: center.y + main.points[id].y };
        });

        const satellites = components.slice(1);
        satellites.forEach((component, index) => {
          const ring = Math.floor(index / 8);
          const slot = index % 8;
          const slots = Math.min(8, satellites.length - ring * 8);
          const orbit = Math.min(minDim * 0.22,
            Math.max(minDim * 0.07, main.radius + component.radius + 16) + ring * minDim * 0.04);
          const angle = -Math.PI / 2 + (2 * Math.PI * slot) / Math.max(1, slots);
          const componentCenter = {
            x: center.x + orbit * Math.cos(angle),
            y: center.y + orbit * Math.sin(angle)
          };
          Object.keys(component.points).forEach(id => {
            positions[id] = {
              x: componentCenter.x + component.points[id].x,
              y: componentCenter.y + component.points[id].y
            };
          });
        });
        return positions;
      },

      renderDependencyGraph() {
        const el = document.getElementById('g6graph');
        if (!el || !this.current) return;
        this.destroyGraph();
        el.innerHTML = '';
        if (!window.G6 || !window.G6.Graph) {
          el.innerHTML = '<div class="empty">G6 未加载：请检查 web/vendor/g6.min.js</div>';
          return;
        }

        const data = this.buildDependencyGraphData();
        this.graphNodeCount = data.nodes.length;
        this.graphEdgeCount = data.edges.length;
        if (!data.nodes.length) {
          el.innerHTML = '<div class="empty">这个仓库没有可显示的文件节点</div>';
          return;
        }

        const W = el.clientWidth || 900, H = el.clientHeight || 600;
        const dense = this.graphMode === 'all' && data.nodes.length > 120;
        const compact = this.graphMode === 'compact';
        let nodes = data.nodes.map(n => {
          const fill = n.is_test ? C.test : (n.is_vendor ? C.vendor : (n.is_aux ? C.aux : C.accent));
          const size = compact
            ? Math.min(34, 14 + Math.sqrt(Math.max(1, n.count)) * 2.2)
            : Math.max(6, Math.min(19, 7 + n.score * 9));
          return {
            id: n.id, label: dense ? '' : n.label, size,
            style: { fill, stroke: '#fff', lineWidth: 1.2, opacity: n.is_test ? 0.72 : 0.95 },
            _raw: n._raw, _group: n._group, _aggregateCount: n.count,
            _viewIndex: n.viewIndex,
            _is_test: n.is_test, _is_vendor: n.is_vendor, _is_aux: n.is_aux,
            _score: n.score
          };
        });
        const edges = data.edges.map(e => {
          const style = {
            stroke: C.edge,
            lineWidth: Math.min(2.5, 0.7 + Math.log2((e.weight || 1) + 1) * 0.35),
            opacity: 0.58
          };
          return { source: e.source, target: e.target, style };
        });
        if (!dense) {
          let positions;
          const stored = compact && this.current.view_layout;
          if (stored && stored.length === nodes.length) {
            let extent = 0;
            stored.forEach(p => { extent = Math.max(extent, Math.abs(p.x), Math.abs(p.y)); });
            const scale = Math.min(W, H) * 0.30 / Math.max(extent, 0.5);
            positions = {};
            nodes.forEach(n => {
              const p = stored[n._viewIndex] || { x: 0, y: 0 };
              positions[n.id] = { x: W / 2 + p.x * scale, y: H / 2 + p.y * scale };
            });
          } else {
            positions = this.graphPositions(nodes, edges, W, H);
          }
          nodes = nodes.map(n => Object.assign({}, n, positions[n.id] || { x: W / 2, y: H / 2 }));
        }
        const graph = new G6.Graph({
          container: el, width: W, height: H, animate: false, fitView: false,
          layout: dense
            ? { type: 'circular', radius: Math.min(W, H) * 0.34, divisions: nodes.length }
            : { type: 'preset' },
          defaultNode: {
            type: 'circle',
            labelCfg: { position: 'bottom', style: { fontSize: compact ? 10.5 : 8.5, fill: '#495057' } }
          },
          defaultEdge: { type: 'line', style: { stroke: C.edge, lineWidth: 1, opacity: 0.55 } },
          modes: { default: ['drag-canvas', 'zoom-canvas', 'drag-node'] }
        });
        graph.data({ nodes, edges });
        graph.render();
        graph.on('node:click', evt => {
          const m = evt.item.getModel();
          if (m._group && m._aggregateCount > 1) {
            const group = m._group;
            this.sel = {
              id: m.id, label: m.label, score: m._score || 0,
              fan_in: 0, pagerank: 0, symbols: [],
              is_test: !!m._is_test, is_vendor: !!m._is_vendor, is_aux: !!m._is_aux,
              _aggregateCount: m._aggregateCount,
              _representative: group.representative || (group.members || [])[0] || '—',
              _aggregateMembers: (group.members || []).slice(0, 10)
            };
          } else {
            this.sel = m._raw || null;
          }
        });
        this._gGraph = graph;
        setTimeout(() => {
          if (this._gGraph === graph && !graph.get('destroyed')) {
            this.applySearch();
          }
        }, 120);
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
        const g = this.tab === 'graph' ? this._gGraph : this._gTask;
        if (!g || g.get('destroyed')) return;
        const q = (this.q || '').toLowerCase().trim();
        try {
          g.getNodes().forEach(item => {
            const m = item.getModel();
            const members = m._group && m._group.members ? m._group.members.join(' ') : '';
            const searchable = (m.id + ' ' + (m.label || '') + ' ' + members).toLowerCase();
            const hit = q && searchable.indexOf(q) >= 0;
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
