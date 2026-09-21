/*!
 * 血缘工作台 —— apps/web（独立前端模块，零构建）
 *
 * 边界：本模块**只通过 HTTP** 调 apps/lineage-api，不 import 任何后端代码。
 * 契约：contracts/openapi.yaml（字段名以真实返回为准，见下方 SHAPES 注释）。
 *
 * 用到的接口与真实返回形状：
 *   GET  /health      -> {success, service, kb_metrics, reports_dir, default_graph, ...}
 *   POST /analyze     -> {success, dialect, statement_count, input_tables[], output_tables[],
 *                        table_lineage[{source,target}], column_lineage_count,
 *                        column_lineage[{target_table,target_column,source_table,source_column,expression,resolved}],
 *                        knowledge{kb_available, metric_count, metrics[], terms[], rules[]},
 *                        report_id, report_url}
 *   POST /upstream    -> {success, direction, start_table, found, direct[], levels[{level,tables[]}],
 *                         tables[], paths[[...]], upstream_count, edge_count}
 *   POST /impact      -> 同上（downstream_count）
 *   GET  /reports     -> {success, count, reports[{report_id, size_bytes, generated_at, url}]}
 *   GET  /report/<id> -> 自包含 HTML 报告
 */
const { createApp } = Vue;

const SAMPLE_SQL = `insert overwrite table ads.ads_产销存月报
select
    a.brand_name                              as 品牌,
    a.plant_name                              as 工厂,
    sum(a.output_qty)                         as 产量,
    sum(a.sale_qty)                           as 销量,
    sum(a.stock_qty)                          as 库存,
    round(sum(a.sale_qty) / sum(a.output_qty), 4) as 产销率
from cdw.dws_产销存汇总 a
join dim.dim_brand b on a.brand_code = b.brand_code
join dim.dim_plant p on a.plant_code = p.plant_code
where a.dt = '\${bizdate}'
group by a.brand_name, a.plant_name`;

const DEFAULT_BASE = (() => {
  /* 被后端 /app/ 托管时（同源），直接用当前源；独立起 5173 时指向后端 18080 */
  const o = location.origin;
  if (o && /:18080$/.test(o)) return o;
  if (/^https?:$/.test(location.protocol) && /:(5173|8080|8000|3000|5500)$/.test(location.port)) return 'http://localhost:18080';
  return 'http://localhost:18080';
})();

createApp({
  data() {
    return {
      tab: 'query',
      baseUrl: localStorage.getItem('lineage.baseUrl') || DEFAULT_BASE,
      health: { ok: false, checked: false },
      dialects: ['hive', 'spark', 'doris', 'postgres'],
      dialect: 'hive',
      sql: SAMPLE_SQL,
      table: 'cdw.dws_产销存汇总',
      filter: '',
      result: null,
      graph: null,
      reports: [],
      currentReport: '',
      err: '',
      busy: { analyze: false, up: false, down: false, reports: false },
    };
  },

  computed: {
    filteredColumns() {
      const rows = (this.result && this.result.column_lineage) || [];
      const q = this.filter.toLowerCase();
      if (!q) return rows;
      return rows.filter((c) =>
        `${c.target_table}.${c.target_column} ${c.source_table}.${c.source_column}`.toLowerCase().includes(q));
    },
  },

  mounted() {
    this.checkHealth();
    /* 演示深链接（也方便无头渲染做验证）：
       ?demo=1            打开就跑一次示例 SQL 解析
       ?tab=reports       直接打开报告浏览屏并加载列表
       ?table=xxx&dir=upstream|impact  直接查某张表的上下游 */
    const q = new URLSearchParams(location.search);
    if (q.get('tab') === 'reports') { this.tab = 'reports'; this.loadReports(); }
    if (q.get('table')) {
      this.table = q.get('table');
      this.queryTable(q.get('dir') === 'impact' ? 'impact' : 'upstream');
    } else if (q.get('demo') === '1' || q.get('autorun') === 'analyze') {
      this.analyze();
    }
  },

  methods: {
    url(path) { return this.baseUrl.replace(/\/+$/, '') + path; },

    async call(path, body) {
      const opt = body
        ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        : { method: 'GET' };
      const r = await fetch(this.url(path), opt);
      const text = await r.text();
      let data;
      try { data = JSON.parse(text); } catch { throw new Error(`服务返回的不是 JSON（HTTP ${r.status}）：${text.slice(0, 160)}`); }
      if (!r.ok || data.success === false) throw new Error(data.error || `HTTP ${r.status}`);
      return data;
    },

    async checkHealth() {
      try {
        const d = await this.call('/health');
        this.health = { ...d, ok: true, checked: true };
        this.err = '';
      } catch (e) {
        this.health = { ok: false, checked: true };
        this.err = `连不上血缘服务（${this.baseUrl}）：${e.message}。若地址不对请改上方「服务地址」。`;
      }
    },

    onBaseChange() {
      localStorage.setItem('lineage.baseUrl', this.baseUrl);
      this.checkHealth();
    },

    loadSample() { this.sql = SAMPLE_SQL; this.dialect = 'hive'; },

    async analyze() {
      this.busy.analyze = true; this.err = ''; this.graph = null;
      try {
        this.result = await this.call('/analyze', { sql: this.sql, dialect: this.dialect, with_report: true });
      } catch (e) { this.err = `解析失败：${e.message}`; }
      this.busy.analyze = false;
    },

    async queryTable(direction) {
      const key = direction === 'upstream' ? 'up' : 'down';
      this.busy[key] = true; this.err = ''; this.result = null;
      try {
        this.graph = await this.call('/' + direction, { table: this.table, graph: 'warehouse_graph.json' });
      } catch (e) { this.err = `${direction === 'upstream' ? '上游' : '下游'}查询失败：${e.message}`; }
      this.busy[key] = false;
    },

    async loadReports() {
      this.busy.reports = true;
      try {
        const d = await this.call('/reports');
        this.reports = d.reports || [];
        if (this.reports.length && !this.currentReport) this.currentReport = this.reports[0].report_id;
      } catch (e) { this.err = `报告列表加载失败：${e.message}`; }
      this.busy.reports = false;
    },

    openReport(id) {
      this.currentReport = id;
      this.tab = 'reports';
      if (!this.reports.length) this.loadReports();
    },

    reportUrl(id) { return this.url('/report/' + id); },
  },
}).mount('#app');
