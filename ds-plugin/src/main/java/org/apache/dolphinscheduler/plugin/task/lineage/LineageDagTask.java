/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.dolphinscheduler.plugin.task.lineage;

import org.apache.dolphinscheduler.common.utils.JSONUtils;
import org.apache.dolphinscheduler.plugin.task.api.AbstractTask;
import org.apache.dolphinscheduler.plugin.task.api.TaskCallBack;
import org.apache.dolphinscheduler.plugin.task.api.TaskConstants;
import org.apache.dolphinscheduler.plugin.task.api.TaskException;
import org.apache.dolphinscheduler.plugin.task.api.TaskExecutionContext;
import org.apache.dolphinscheduler.plugin.task.api.enums.DataType;
import org.apache.dolphinscheduler.plugin.task.api.enums.Direct;
import org.apache.dolphinscheduler.plugin.task.api.model.Property;
import org.apache.dolphinscheduler.plugin.task.api.parameters.AbstractParameters;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * LINEAGE_DAG (工作流级血缘分析) task.
 *
 * <p>与 {@link LineageTask} 的根本区别：**用户不填 SQL**。
 * 本任务从 {@link TaskExecutionContext} 拿到 {@code projectCode} / {@code processDefineCode}，
 * 调血缘服务的 {@code POST /analyze-workflow}，由服务端登录海豚 OpenAPI 把**整个工作流的
 * 所有任务脚本**一次拉下来批量解析 —— 所以它能给出单脚本任务给不出的三样东西：
 *
 * <ol>
 *   <li><b>跨任务链路</b>：``src.erp_x → ods.ods_x → cdw.dwd_x → cdw.dws_x`` 一次看全；</li>
 *   <li><b>链路质量体检</b>：产出表无人消费（断链）/ 输入表无上游（孤岛）/ 环路 / 未登记口径；</li>
 *   <li><b>工作流级口径汇总</b>：整个工作流产出的指标口径一次对齐。</li>
 * </ol>
 *
 * <p>价值在于**历史工作流零改造**：原有 N 个任务一行不用改，只在尾部挂 1 个本类型节点即可。
 *
 * <p>日志排版沿用 {@link LineageTask} 的约定：中文分段报告、``  ┌── ① …`` / ``  │  `` 前缀、
 * 表格化字段血缘（最多 15 行）、口径最多展开 3 条，其余折叠，结尾一行汇总 + ``📊 报告 URL``。
 */
public class LineageDagTask extends AbstractTask {

    private static final Logger logger = LoggerFactory.getLogger(LineageDagTask.class);

    private static final String HEADER = "============================================================";

    // ---- 日志排版参数（② 全链路 / ③ 字段表格 / ④ 口径折叠）----
    /** ③ 段表格列宽（按显示宽度算，CJK 占 2 列） */
    private static final int W_TASK = 18;
    /** ① 段类型列宽：要放得下 LINEAGE_DAG（11 字符） */
    private static final int W_TYPE = 12;
    private static final int W_TARGET = 18;
    private static final int W_SOURCE = 30;
    private static final int W_EXPR = 34;
    /** ② 段链路一行最长显示宽度，超出折行 */
    private static final int W_CHAIN = 96;
    /** ② 段最多打印多少条任务级依赖 */
    private static final int MAX_TASK_EDGES = 8;
    /** ③ 段最多打印多少行跨任务字段血缘 */
    private static final int MAX_FIELD_ROWS = 15;
    /** ④ 段最多展开多少条业务口径 */
    private static final int MAX_METRIC_CARDS = 3;

    private final TaskExecutionContext taskExecutionContext;
    private final LineageDagParameters parameters;

    public LineageDagTask(TaskExecutionContext taskExecutionContext) {
        super(taskExecutionContext);
        this.taskExecutionContext = taskExecutionContext;
        this.parameters = JSONUtils.parseObject(taskExecutionContext.getTaskParams(), LineageDagParameters.class);
        logger.info("LINEAGE_DAG task initialized, params: {}",
                parameters == null ? "null" : parameters.toString());
    }

    @Override
    public void init() {
        logger.info("LINEAGE_DAG task start, taskInstanceId={}, processDefineCode={}, projectCode={}",
                taskExecutionContext.getTaskInstanceId(),
                taskExecutionContext.getProcessDefineCode(),
                taskExecutionContext.getProjectCode());
    }

    @Override
    public void handle(TaskCallBack taskCallBack) throws TaskException {
        if (parameters == null) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("LINEAGE_DAG task params can not be parsed");
        }
        if (!parameters.checkParameters()) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("LINEAGE_DAG task parameters check failed: " + parameters);
        }

        String serviceUrl = trimTrailingSlash(parameters.getServiceUrl());
        String url = serviceUrl + "/analyze-workflow";
        String requestBody = buildBody();

        logger.info("{}", HEADER);
        logger.info("  LINEAGE_DAG 工作流级血缘分析任务");
        logger.info("{}", HEADER);
        logger.info("分析范围   : {} - {}", parameters.normalizedScope(),
                "project".equals(parameters.normalizedScope())
                        ? "项目下全部工作流（跨工作流链路）" : "当前工作流（全部任务脚本）");
        logger.info("任务类型   : {}", join(parameters.taskTypeList()));
        logger.info("血缘服务   : {}", url);
        logger.info("请求体     : {}", requestBody);
        logger.info("{}", HEADER);

        long start = System.currentTimeMillis();
        String response;
        try {
            response = LineageServiceClient.postJson(url, requestBody, parameters.getTimeout());
        } catch (Exception e) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            logger.error("调用工作流血缘服务失败: {}", url, e);
            throw new TaskException("call lineage service failed: " + url, e);
        }
        long cost = System.currentTimeMillis() - start;

        JsonNode root;
        try {
            root = JSONUtils.parseObject(response);
        } catch (Exception e) {
            logger.error("工作流血缘服务返回内容不是合法 JSON: {}", clip(response, 500), e);
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("lineage service response is not a valid json", e);
        }
        if (root == null) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("lineage service returned empty response");
        }
        if (!root.path("success").asBoolean(false)) {
            String error = opt(root.get("error"));
            logger.error("{}", HEADER);
            logger.error("  工作流血缘分析失败: {}", error.isEmpty() ? response : error);
            logger.error("{}", HEADER);
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("workflow lineage analysis failed: " + error);
        }

        printReport(root, url, cost);

        setExitStatusCode(TaskConstants.EXIT_CODE_SUCCESS);
        collectOutputParameters(root, url, cost);
    }

    @Override
    public void cancel() throws TaskException {
        // blocking HTTP call, nothing to cancel
    }

    @Override
    public AbstractParameters getParameters() {
        return parameters;
    }

    // ---------------------------------------------------------------- request body

    /**
     * 请求体：工作流身份 + 解析范围。
     *
     * <p>{@code project_code} / {@code process_define_code} 由海豚在任务执行上下文里直接给出，
     * 所以「历史工作流零改造」才成立 —— 用户不需要在工作流里配置任何标识。
     */
    private String buildBody() {
        Map<String, Object> body = new LinkedHashMap<>();
        Long defineCode = taskExecutionContext.getProcessDefineCode();
        body.put("project_code", taskExecutionContext.getProjectCode());
        if (defineCode != null) {
            body.put("process_define_code", defineCode);
        }
        body.put("scope", parameters.normalizedScope());
        body.put("task_types", parameters.taskTypeList());
        body.put("include_sub_process", parameters.isIncludeSubProcess());
        body.put("dialect", parameters.getDialect());
        body.put("with_knowledge", Boolean.TRUE);
        body.put("with_report", Boolean.TRUE);
        return JSONUtils.toJsonString(body);
    }

    // ---------------------------------------------------------------- report

    private void printReport(JsonNode root, String url, long cost) {
        JsonNode workflow = root.get("workflow");
        JsonNode tasks = root.get("tasks");
        JsonNode merged = root.get("merged");
        JsonNode quality = root.get("quality");

        String workflowName = text(workflow == null ? null : workflow.get("name"));
        int taskCount = workflow == null ? 0 : workflow.path("task_count").asInt(0);
        int parsedCount = workflow == null ? 0 : workflow.path("parsed_task_count").asInt(0);
        int nodeCount = merged == null ? 0 : sizeOf(merged.get("nodes"));
        int edgeCount = merged == null ? 0 : sizeOf(merged.get("edges"));
        int columnCount = merged == null ? 0 : merged.path("column_lineage_count").asInt(0);
        int metricCount = root.path("metric_count").asInt(0);

        logger.info("");
        logger.info("╔══════════════════════════════════════════════════════════════════╗");
        logger.info("║           工 作 流 血 缘 分 析 报 告   WORKFLOW LINEAGE REPORT   ║");
        logger.info("╚══════════════════════════════════════════════════════════════════╝");
        logger.info("  工作流   : {}   任务 {} 个（解析 {}）   语句 {}   耗时 {} ms",
                workflowName, taskCount, parsedCount,
                workflow == null ? 0 : workflow.path("statement_count").asInt(0), cost);
        logger.info("  血缘服务 : {}   （口径命中 {} 条 / 节点 {} / 边 {}）", url, metricCount, nodeCount, edgeCount);

        printTasks(tasks);
        printChain(root, workflow, merged);
        printColumnLineage(merged, columnCount);
        printKnowledge(root);
        printQuality(quality);

        // ---------------------------------------------------------- 汇总
        logger.info("");
        logger.info("  ══════════════════════════════════════════════════════════════════");
        logger.info("  ✅ 工作流血缘分析完成 | 任务 {} 个 | 节点 {} | 边 {} | 字段映射 {} 个 | 口径命中 {} 条 | 耗时 {} ms",
                taskCount, nodeCount, edgeCount, columnCount, metricCount, cost);
        String reportUrl = opt(root.get("url"));
        if (reportUrl.isEmpty()) {
            JsonNode report = root.get("report");
            reportUrl = report != null && report.isObject() ? opt(report.get("url")) : "";
        }
        if (!reportUrl.isEmpty()) {
            logger.info("  📊 完整报告（浏览器打开）: {}", reportUrl);
            String internal = opt(root.get("internal_url"));
            if (internal.isEmpty()) {
                JsonNode report = root.get("report");
                internal = report != null && report.isObject() ? opt(report.get("internal_url")) : "";
            }
            if (!internal.isEmpty()) {
                logger.info("     （容器内访问用: {}）", internal);
            }
        }
        logger.info("  ══════════════════════════════════════════════════════════════════");
        logger.info("");

        if (nodeCount == 0) {
            logger.warn("未解析出任何表级血缘：请确认工作流里确实有 SQL/SHELL 任务，且 taskTypes 配置正确");
        }
    }

    /** ① 任务清单：一眼看出这个工作流有几个任务、谁产出了什么表、命中了几条口径。 */
    private void printTasks(JsonNode tasks) {
        logger.info("");
        logger.info("  ┌── ① 任务清单（{} 个）─────────────────────────────────────────────", sizeOf(tasks));
        if (tasks == null || !tasks.isArray() || tasks.size() == 0) {
            logger.info("  │  (工作流里没有任务)");
            return;
        }
        logger.info("  │  {} │ {} │ {} │ {} │ {}",
                pad("任务名", W_TASK), pad("类型", W_TYPE), pad("脚本长度", 8), pad("语句", 4), pad("口径", 4));
        logger.info("  │  {}┼{}┼{}┼{}┼{}", repeat("─", W_TASK + 2), repeat("─", W_TYPE + 2),
                repeat("─", 10), repeat("─", 6), repeat("─", 6));
        for (JsonNode task : tasks) {
            List<String> outs = readStringArray(task, "output_tables");
            logger.info("  │  {} │ {} │ {} │ {} │ {}",
                    pad(clip(oneLine(text(task.get("name"))), W_TASK), W_TASK),
                    pad(text(task.get("type")), W_TYPE),
                    pad(text(task.get("script_len")), 8),
                    pad(text(task.get("statement_count")), 4),
                    pad(text(task.get("metric_count")), 4));
            if (!outs.isEmpty()) {
                logger.info("  │      └─ 产出 : {}", flattenList(outs, 4));
            }
            for (JsonNode error : iter(task.get("errors"))) {
                logger.info("  │      ⚠ {}", clip(oneLine(error.asText()), 88));
            }
        }
    }

    /** ② 全链路图谱：跨任务的表级链路 + 任务级依赖，工作流级最核心的一屏。 */
    private void printChain(JsonNode root, JsonNode workflow, JsonNode merged) {
        List<String> chain = readStringArray(root, "chain");
        logger.info("");
        logger.info("  ┌── ② 全链路图谱（跨任务）────────────────────────────────────────");
        if (chain.isEmpty()) {
            logger.info("  │  (未解析出跨任务链路：各任务可能都是单表加工)");
        } else {
            logger.info("  │  链路（{} 级）:", chain.size());
            for (String piece : wrap(joinArrow(chain), W_CHAIN)) {
                logger.info("  │      {}", piece);
            }
            // 分层展示：按表名前缀把链路拆成 src / ods / cdw / ads 若干层
            List<String> layers = new ArrayList<>();
            for (String table : chain) {
                String layer = layerOf(table);
                if (!layer.isEmpty() && !layers.contains(layer)) {
                    layers.add(layer);
                }
            }
            logger.info("  │  分层 : {}（{} 层；本工作流内 {} 级）",
                    joinWith(layers, " → "), layers.size(),
                    sizeOf(workflow == null ? null : workflow.get("chain_in_workflow")));
            JsonNode chainExternal = workflow == null ? null : workflow.get("chain_external");
            List<String> external = chainExternal != null && chainExternal.isObject()
                    ? readStringArray(chainExternal.get("upstream")) : new ArrayList<String>();
            if (!external.isEmpty()) {
                logger.info("  │  跨工作流上游 : {}  （全局血缘拼接）", join(external));
            }
        }

        JsonNode taskLineage = merged == null ? null : merged.get("task_lineage");
        if (taskLineage != null && taskLineage.isArray() && taskLineage.size() > 0) {
            logger.info("  │  任务级依赖（{} 条）:", taskLineage.size());
            int shown = 0;
            for (JsonNode edge : taskLineage) {
                if (shown++ >= MAX_TASK_EDGES) {
                    logger.info("  │      … 其余 {} 条见完整报告", taskLineage.size() - MAX_TASK_EDGES);
                    break;
                }
                logger.info("  │      {} ──► {}   （经 {}）",
                        text(edge.get("source_task")), text(edge.get("target_task")),
                        flattenList(readStringArray(edge, "via_tables"), 3));
            }
        }
    }

    /** ③ 跨任务字段血缘（紧凑表格，最多 15 行）。 */
    private void printColumnLineage(JsonNode merged, int columnCount) {
        JsonNode columns = merged == null ? null : merged.get("column_lineage");
        logger.info("");
        logger.info("  ┌── ③ 跨任务字段血缘（{} 个字段映射）──────────────────────────────", columnCount);
        if (columnCount == 0 || columns == null || !columns.isArray()) {
            logger.info("  │  (无字段级血缘)");
            return;
        }
        logger.info("  │  {} │ {} │ {} │ {}",
                pad("来源任务", W_TASK), pad("目标字段", W_TARGET), pad("来源字段", W_SOURCE), pad("加工表达式", W_EXPR));
        logger.info("  │  {}┼{}┼{}┼{}", repeat("─", W_TASK + 2), repeat("─", W_TARGET + 2),
                repeat("─", W_SOURCE + 2), repeat("─", W_EXPR + 2));
        int shown = 0;
        for (JsonNode column : columns) {
            if (shown >= MAX_FIELD_ROWS) {
                break;
            }
            logger.info("  │  {} │ {} │ {} │ {}",
                    pad(clip(oneLine(text(column.get("task"))), W_TASK), W_TASK),
                    pad(clip(oneLine(text(column.get("target_column"))), W_TARGET), W_TARGET),
                    pad(clip(oneLine(sourceField(column)), W_SOURCE), W_SOURCE),
                    clip(oneLine(text(column.get("expression"))), W_EXPR));
            shown++;
        }
        if (columnCount > shown) {
            logger.info("  │  … 其余 {} 行见完整报告", columnCount - shown);
        }
    }

    /** ④ 业务口径汇总：工作流级命中最多，日志里只展开最关键的 3 条。 */
    private void printKnowledge(JsonNode root) {
        JsonNode knowledge = root.get("knowledge");
        logger.info("");
        logger.info("  ┌── ④ 业务口径汇总（知识库匹配）──────────────────────────────────");
        boolean available = knowledge != null && !knowledge.isNull()
                && knowledge.path("kb_available").asBoolean(false);
        if (knowledge == null || knowledge.isNull()) {
            logger.info("  │  未匹配到业务口径（服务端未返回 knowledge 段）");
            return;
        }
        if (!available) {
            logger.info("  │  未匹配到业务口径（可先执行 kb build 建库）");
            String reason = opt(knowledge.get("reason"));
            if (!reason.isEmpty()) {
                logger.info("  │  原因: {}", reason);
            }
            return;
        }
        JsonNode metrics = knowledge.get("metrics");
        if (metrics == null || !metrics.isArray() || metrics.size() == 0) {
            logger.info("  │  知识库已就绪，但本工作流产出字段未匹配到已登记指标口径");
            return;
        }
        int shown = 0;
        for (JsonNode metric : metrics) {
            if (shown >= MAX_METRIC_CARDS) {
                break;
            }
            shown++;
            String cn = opt(metric.get("chinese_name"));
            String column = opt(metric.get("target_column"));
            String formula = formulaBody(cn.isEmpty() ? column : cn, opt(metric.get("formula")));
            logger.info("  │  ★ {}. {}{} = {}", shown, cn.isEmpty() ? column : cn,
                    column.isEmpty() ? "" : "（" + column + "）", formula.isEmpty() ? "(无公式)" : formula);
            logger.info("  │       目标表 {} · 类型 {} · 置信度 {} · 匹配 {}",
                    opt(metric.get("target_table")), opt(metric.get("metric_type")),
                    opt(metric.get("confidence")), opt(metric.get("matched_by")));
        }
        int rest = metrics.size() - shown;
        if (rest > 0) {
            logger.info("  │  … 另有 {} 条口径详见完整报告（口径由 kb build 从加工脚本自动提炼）", rest);
        }
    }

    /** ⑤ 链路质量体检：断链 / 孤岛 / 环路 / 未登记口径 —— 工作流级最实用的一屏。 */
    private void printQuality(JsonNode quality) {
        logger.info("");
        logger.info("  ┌── ⑤ 链路质量体检 ───────────────────────────────────────────────");
        if (quality == null || quality.isNull()) {
            logger.info("  │  (未做体检)");
            return;
        }
        List<JsonNode> cycles = list(quality.get("cycles"));
        List<JsonNode> dangling = list(quality.get("dangling_outputs"));
        List<JsonNode> orphans = list(quality.get("orphan_inputs"));
        List<JsonNode> missing = list(quality.get("missing_knowledge"));

        logger.info("  │  {} 环路：{}", cycles.isEmpty() ? "✅" : "⚠",
                cycles.isEmpty() ? "任务间未形成环，表级依赖亦无环" : cycles.size() + " 个环路");
        for (JsonNode cycle : cycles) {
            logger.info("  │       ↺ {}", flatten(cycle));
        }

        logger.info("  │  {} {} 张产出表在本工作流内无人消费{}", dangling.isEmpty() ? "✅" : "⚠",
                dangling.size(), dangling.isEmpty() ? "（无断链）" : "（断链）");
        for (JsonNode item : dangling) {
            logger.info("  │       {}  ← {}", text(item.get("table")), clip(oneLine(opt(item.get("hint"))), 74));
        }

        logger.info("  │  {} {} 张输入表在本工作流内无上游{}", orphans.isEmpty() ? "✅" : "⚠",
                orphans.size(), orphans.isEmpty() ? "（无孤岛输入）" : "（孤岛输入）");
        for (JsonNode item : orphans) {
            logger.info("  │       {}  ← {}", text(item.get("table")), clip(oneLine(opt(item.get("hint"))), 74));
        }

        logger.info("  │  {} {} 张产出表未在知识库登记口径{}", missing.isEmpty() ? "✅" : "⚠",
                missing.size(), missing.isEmpty() ? "（口径已全登记）" : "");
        for (JsonNode item : missing) {
            logger.info("  │       {}", text(item.get("table")));
        }
    }

    // ---------------------------------------------------------------- out params

    private void collectOutputParameters(JsonNode root, String url, long cost) {
        JsonNode workflow = root.get("workflow");
        JsonNode merged = root.get("merged");
        List<String> chain = readStringArray(root, "chain");

        Map<String, String> output = new LinkedHashMap<>();
        output.put("lineage_dag_workflow", workflow == null ? "" : text(workflow.get("name")));
        output.put("lineage_dag_workflow_code", workflow == null ? "" : text(workflow.get("code")));
        output.put("lineage_dag_task_count", workflow == null ? "0" : text(workflow.get("task_count")));
        output.put("lineage_dag_parsed_task_count", workflow == null ? "0" : text(workflow.get("parsed_task_count")));
        output.put("lineage_dag_node_count", merged == null ? "0" : String.valueOf(merged.path("nodes").size()));
        output.put("lineage_dag_edge_count", merged == null ? "0" : String.valueOf(merged.path("edges").size()));
        output.put("lineage_dag_metric_count", String.valueOf(root.path("metric_count").asInt(0)));
        output.put("lineage_dag_column_count",
                merged == null ? "0" : String.valueOf(merged.path("column_lineage_count").asInt(0)));
        output.put("lineage_dag_chain", joinArrow(chain));
        output.put("lineage_dag_service_url", url);
        output.put("lineage_dag_cost_ms", String.valueOf(cost));
        String reportUrl = opt(root.get("url"));
        if (reportUrl.isEmpty()) {
            JsonNode report = root.get("report");
            reportUrl = report != null && report.isObject() ? opt(report.get("url")) : "";
        }
        output.put("lineage_dag_report_id", opt(root.get("report_id")));
        output.put("lineage_dag_report_url", reportUrl);
        String raw = root.toString();
        output.put("lineage_dag_report_raw", raw.length() > 4000 ? raw.substring(0, 4000) : raw);

        setTaskOutputParams(output);

        // in DolphinScheduler 3.2.x the worker sends back parameters.getVarPool()
        List<Property> varPool = parameters.getVarPool();
        if (varPool == null) {
            varPool = new ArrayList<>();
            parameters.varPool = varPool;
        }
        for (Map.Entry<String, String> entry : output.entrySet()) {
            varPool.add(new Property(entry.getKey(), Direct.OUT, DataType.VARCHAR, entry.getValue()));
        }
        logger.info("输出参数已写入 varPool: {}", output.keySet());
    }

    // ---------------------------------------------------------------- 表格排版

    /** 显示宽度：CJK 等全角字符按 2 列算（日志按等宽字体对齐） */
    private static int displayWidth(String value) {
        if (value == null) {
            return 0;
        }
        int width = 0;
        for (int i = 0; i < value.length(); i++) {
            width += value.charAt(i) > 0x2000 ? 2 : 1;
        }
        return width;
    }

    /** 按显示宽度右侧补空格（中文也能对齐） */
    private static String pad(String value, int width) {
        String v = value == null ? "" : value;
        StringBuilder sb = new StringBuilder(v);
        for (int i = displayWidth(v); i < width; i++) {
            sb.append(' ');
        }
        return sb.toString();
    }

    /** 按显示宽度截断，超出部分用「…」收尾 */
    private static String clip(String value, int width) {
        String v = value == null ? "" : value;
        if (displayWidth(v) <= width) {
            return v;
        }
        StringBuilder sb = new StringBuilder();
        int used = 0;
        for (int i = 0; i < v.length(); i++) {
            char c = v.charAt(i);
            int step = c > 0x2000 ? 2 : 1;
            if (used + step > width - 1) {
                break;
            }
            sb.append(c);
            used += step;
        }
        return sb.append('…').toString();
    }

    /** 按显示宽度折行：优先在空格处断开，单个超长 token 才硬切 */
    private static List<String> wrap(String value, int width) {
        List<String> out = new ArrayList<>();
        String v = (value == null ? "" : value).trim();
        if (v.isEmpty()) {
            out.add("");
            return out;
        }
        StringBuilder line = new StringBuilder();
        int used = 0;
        for (String token : v.split("\\s+")) {
            if (token.isEmpty()) {
                continue;
            }
            int tokenWidth = displayWidth(token);
            if (tokenWidth > width) {
                if (used > 0) {
                    out.add(line.toString());
                    line.setLength(0);
                    used = 0;
                }
                String rest = token;
                while (displayWidth(rest) > width) {
                    String headPiece = head(rest, width);
                    out.add(headPiece);
                    rest = rest.substring(headPiece.length());
                }
                line.append(rest);
                used = displayWidth(rest);
                continue;
            }
            if (used > 0 && used + 1 + tokenWidth > width) {
                out.add(line.toString());
                line.setLength(0);
                used = 0;
            }
            if (used > 0) {
                line.append(' ');
                used++;
            }
            line.append(token);
            used += tokenWidth;
        }
        if (line.length() > 0) {
            out.add(line.toString());
        }
        return out.isEmpty() ? java.util.Collections.singletonList("") : out;
    }

    /** 取能放进 width 显示列的最长前缀 */
    private static String head(String value, int width) {
        StringBuilder sb = new StringBuilder();
        int used = 0;
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            int step = c > 0x2000 ? 2 : 1;
            if (used + step > width) {
                break;
            }
            sb.append(c);
            used += step;
        }
        return sb.toString();
    }

    /** 表格单元格：换行/制表压成空格，避免把日志表格撑坏 */
    private static String oneLine(String value) {
        if (value == null) {
            return "";
        }
        return value.replace("\r", " ").replace("\n", " ").replace("\t", " ").trim();
    }

    /** 来源字段：表.字段（表名在②段出现过，这里保留全名便于直接定位） */
    private static String sourceField(JsonNode column) {
        String table = text(column.get("source_table"));
        String col = text(column.get("source_column"));
        if ("-".equals(table) || "null".equals(table)) {
            return col;
        }
        return table + "." + col;
    }

    /** 表名前缀当分层名（``cdw.dwd_产量明细`` → ``cdw``） */
    private static String layerOf(String table) {
        String name = table == null ? "" : table;
        int dot = name.indexOf('.');
        return dot > 0 ? name.substring(0, dot) : "";
    }

    /** 链路转成一行箭头文本 */
    private static String joinArrow(List<String> tables) {
        return joinWith(tables, " ──► ");
    }

    /** 口径公式：知识库里是「名称 = 表达式」，行首已经写了名称，这里剥掉重复的前缀 */
    private static String formulaBody(String name, String formula) {
        String body = formula == null ? "" : formula.trim();
        if (name != null && !name.isEmpty() && body.startsWith(name)) {
            body = body.substring(name.length()).trim();
            if (body.startsWith("=")) {
                body = body.substring(1).trim();
            }
        }
        return body;
    }

    /** 取值：缺失一律给空串（区别于 {@link #text(JsonNode)} 的 "-"） */
    private static String opt(JsonNode node) {
        return node == null || node.isNull() ? "" : node.asText();
    }

    private static String text(JsonNode node) {
        return node == null || node.isNull() ? "-" : node.asText();
    }

    private static int sizeOf(JsonNode node) {
        return node != null && node.isArray() ? node.size() : 0;
    }

    /** 只读列表视图：非数组一律返回空 */
    private static List<JsonNode> list(JsonNode node) {
        List<JsonNode> out = new ArrayList<>();
        if (node != null && node.isArray()) {
            for (JsonNode child : node) {
                out.add(child);
            }
        }
        return out;
    }

    private static Iterable<JsonNode> iter(JsonNode node) {
        return list(node);
    }

    private static List<String> readStringArray(JsonNode root, String field) {
        return root == null ? new ArrayList<String>() : readStringArray(root.get(field));
    }

    private static List<String> readStringArray(JsonNode node) {
        List<String> result = new ArrayList<>();
        if (node == null || node.isNull()) {
            return result;
        }
        if (node.isArray()) {
            for (JsonNode child : node) {
                if (child.isValueNode()) {
                    result.add(child.asText());
                }
            }
        } else if (node.isValueNode()) {
            result.add(node.asText());
        }
        return result;
    }

    /** 列表压成一行：最多显示 limit 项，超出用「…(共 N)」收尾 */
    private static String flattenList(List<String> items, int limit) {
        if (items.isEmpty()) {
            return "-";
        }
        List<String> shown = new ArrayList<>();
        for (int i = 0; i < items.size() && i < limit; i++) {
            shown.add(items.get(i));
        }
        String text = join(shown);
        return items.size() > shown.size() ? text + " …(共 " + items.size() + ")" : text;
    }

    /** 把数组/字符串压成一行（用于环路展示） */
    private static String flatten(JsonNode node) {
        if (node == null || node.isNull()) {
            return "-";
        }
        if (node.isArray()) {
            StringBuilder sb = new StringBuilder();
            int i = 0;
            for (JsonNode child : node) {
                if (i++ > 0) {
                    sb.append(" → ");
                }
                sb.append(child.isValueNode() ? child.asText() : flatten(child));
            }
            return sb.toString();
        }
        return node.asText();
    }

    private static String join(List<String> items) {
        return joinWith(items, ", ");
    }

    private static String joinWith(List<String> items, String separator) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(separator);
            }
            sb.append(items.get(i));
        }
        return sb.toString();
    }

    private static String trimTrailingSlash(String url) {
        if (url == null) {
            return "";
        }
        String trimmed = url.trim();
        while (trimmed.endsWith("/")) {
            trimmed = trimmed.substring(0, trimmed.length() - 1);
        }
        return trimmed;
    }

    private static String repeat(String s, int times) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < times; i++) {
            sb.append(s);
        }
        return sb.toString();
    }
}
