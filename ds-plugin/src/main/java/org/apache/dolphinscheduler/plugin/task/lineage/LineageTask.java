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
 * LINEAGE (血缘分析) task.
 *
 * <p>The task performs a blocking HTTP call against the lineage service
 * (default {@code http://172.17.0.1:18080}) and writes the returned lineage report
 * into the DolphinScheduler task instance log. Selected values are also exposed as
 * out-parameters ({@code varPool}) so that downstream tasks can reference them.
 */
public class LineageTask extends AbstractTask {

    private static final Logger logger = LoggerFactory.getLogger(LineageTask.class);

    private static final String HEADER = "============================================================";

    private final TaskExecutionContext taskExecutionContext;
    private final LineageParameters parameters;

    public LineageTask(TaskExecutionContext taskExecutionContext) {
        super(taskExecutionContext);
        this.taskExecutionContext = taskExecutionContext;
        this.parameters = JSONUtils.parseObject(taskExecutionContext.getTaskParams(), LineageParameters.class);
        logger.info("LINEAGE task initialized, params: {}",
                parameters == null ? "null" : parameters.toString());
    }

    @Override
    public void init() {
        logger.info("LINEAGE task start, taskInstanceId={}", taskExecutionContext.getTaskInstanceId());
    }

    @Override
    public void handle(TaskCallBack taskCallBack) throws TaskException {
        if (parameters == null) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("LINEAGE task params can not be parsed");
        }
        if (!parameters.checkParameters()) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("LINEAGE task parameters check failed: " + parameters);
        }

        String serviceUrl = trimTrailingSlash(parameters.getServiceUrl());
        String mode = parameters.normalizedMode();
        String primaryEndpoint;
        String fallbackEndpoint = null;
        String requestBody;
        String action;
        switch (mode) {
            case "impact":
                primaryEndpoint = "/impact";
                action = "下游影响分析 (downstream impact)";
                requestBody = buildTableBody();
                break;
            case "upstream":
                primaryEndpoint = "/upstream";
                action = "上游溯源 (upstream trace)";
                requestBody = buildTableBody();
                break;
            case "sql":
            default:
                primaryEndpoint = "/analyze";
                fallbackEndpoint = "/parse";
                action = "SQL 血缘解析 + 业务口径匹配 (analyze SQL + knowledge base)";
                requestBody = buildSqlBody();
                break;
        }

        String url = serviceUrl + primaryEndpoint;
        long start = System.currentTimeMillis();
        String response;
        try {
            logger.info("{}", HEADER);
            logger.info("  LINEAGE 血缘分析任务");
            logger.info("{}", HEADER);
            logger.info("分析模式   : {} - {}", mode, action);
            logger.info("血缘服务   : {}", url);
            logger.info("请求体     : {}", requestBody);
            logger.info("{}", HEADER);
            try {
                response = LineageServiceClient.postJson(url, requestBody, parameters.getTimeout());
            } catch (Exception first) {
                if (fallbackEndpoint == null) {
                    throw first;
                }
                // 服务端可能还是老版本（没有 /analyze）：回退到 /parse，血缘报告照常输出
                String fallbackUrl = serviceUrl + fallbackEndpoint;
                logger.warn("一体化端点不可用（{}），回退到 {}：本次只输出血缘，不含业务口径",
                        first.getMessage(), fallbackUrl);
                response = LineageServiceClient.postJson(fallbackUrl, requestBody, parameters.getTimeout());
                url = fallbackUrl;
            }
        } catch (Exception e) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            logger.error("调用血缘服务失败: {}", url, e);
            throw new TaskException("call lineage service failed: " + url, e);
        }
        long cost = System.currentTimeMillis() - start;

        JsonNode root;
        try {
            root = JSONUtils.parseObject(response);
        } catch (Exception e) {
            logger.error("血缘服务返回内容不是合法 JSON: {}", response, e);
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("lineage service response is not a valid json", e);
        }

        if (root == null) {
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("lineage service returned empty response");
        }

        // the service may report a business level error through code/message
        JsonNode codeNode = root.get("code");
        if (codeNode != null && codeNode.isNumber() && codeNode.asInt() != 0) {
            logger.error("血缘服务返回业务错误: {}", response);
            setExitStatusCode(TaskConstants.EXIT_CODE_FAILURE);
            throw new TaskException("lineage service business error: " + root.get("message"));
        }

        printReport(root, mode, url, cost);

        setExitStatusCode(TaskConstants.EXIT_CODE_SUCCESS);
        collectOutputParameters(root, url, mode, cost);
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

    private String buildSqlBody() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("sql", parameters.getSql());
        body.put("dialect", parameters.getDialect());
        // /analyze 需要它；老版本服务端忽略未知字段，所以同一个请求体可以两边复用
        body.put("with_knowledge", Boolean.TRUE);
        return JSONUtils.toJsonString(body);
    }

    private String buildTableBody() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("table", parameters.getTable());
        body.put("graph", parameters.getGraph());
        body.put("depth", parameters.getDepth());
        return JSONUtils.toJsonString(body);
    }

    // ---------------------------------------------------------------- report

    private void printReport(JsonNode root, String mode, String url, long cost) {
        List<String> inputTables = readStringArray(root, "input_tables");
        List<String> outputTables = readStringArray(root, "output_tables");
        JsonNode tableLineage = root.get("table_lineage");
        JsonNode columnLineage = root.get("column_lineage");
        JsonNode statements = root.get("statements");
        int columnCount = columnLineage != null && columnLineage.isArray() ? columnLineage.size() : 0;

        logger.info("");
        logger.info("╔══════════════════════════════════════════════════════════════════╗");
        logger.info("║              数 据 血 缘 分 析 报 告   LINEAGE REPORT            ║");
        logger.info("╚══════════════════════════════════════════════════════════════════╝");

        StringBuilder head = new StringBuilder();
        head.append("  分析模式 : ").append("sql".equals(mode) ? "SQL 解析" : mode);
        if ("sql".equals(mode)) {
            head.append("   |   方言 : ").append(text(root.get("dialect")));
            head.append("   |   语句数 : ").append(text(root.get("statement_count")));
        }
        head.append("   |   耗时 : ").append(cost).append(" ms");
        logger.info("{}", head.toString());
        logger.info("  血缘服务 : {}", url);

        // ---------------------------------------------------------- ① 表级
        logger.info("");
        logger.info("  ┌── ① 表级血缘 ──────────────────────────────────────────────────");
        boolean flowPrinted = false;
        if (tableLineage != null && tableLineage.isArray() && tableLineage.size() > 0) {
            logger.info("  │  数据流向：");
            for (JsonNode e : tableLineage) {
                logger.info("  │      {}  ──►  {}", text(e.get("source")), text(e.get("target")));
            }
            flowPrinted = true;
        } else if (!inputTables.isEmpty() && !outputTables.isEmpty()) {
            logger.info("  │  数据流向：");
            for (String i : inputTables) {
                for (String o : outputTables) {
                    logger.info("  │      {}  ──►  {}", i, o);
                }
            }
            flowPrinted = true;
        }
        if (!flowPrinted) {
            logger.info("  │  (未解析出表级血缘)");
        }
        if (!inputTables.isEmpty()) {
            logger.info("  │  源表（输入 {} 张）: {}", inputTables.size(), join(inputTables));
        }
        if (!outputTables.isEmpty()) {
            logger.info("  │  目标表（输出 {} 张）: {}", outputTables.size(), join(outputTables));
        }

        // ---------------------------------------------------------- ② 字段级
        logger.info("");
        logger.info("  ┌── ② 字段级血缘（{} 个字段映射）────────────────────────────────", columnCount);
        if (columnCount > 0) {
            Map<String, List<JsonNode>> byTarget = new LinkedHashMap<>();
            for (JsonNode c : columnLineage) {
                String tt = text(c.get("target_table"));
                List<JsonNode> list = byTarget.get(tt);
                if (list == null) {
                    list = new ArrayList<>();
                    byTarget.put(tt, list);
                }
                list.add(c);
            }
            int shown = 0;
            final int maxShow = 40;
            for (Map.Entry<String, List<JsonNode>> en : byTarget.entrySet()) {
                logger.info("  │  目标表 {} :", en.getKey());
                for (JsonNode c : en.getValue()) {
                    if (shown >= maxShow) {
                        logger.info("  │      ... 其余字段已省略（共 {} 个）", columnCount);
                        break;
                    }
                    logger.info("  │      {}  ←  {}.{}      表达式: {}",
                            text(c.get("target_column")), text(c.get("source_table")),
                            text(c.get("source_column")), text(c.get("expression")));
                    shown++;
                }
            }
        } else {
            logger.info("  │  (无字段级血缘)");
        }

        // ---------------------------------------------------------- ③ 加工条件
        logger.info("");
        logger.info("  ┌── ③ 加工条件（过滤 / 分区）────────────────────────────────────");
        boolean anyCond = false;
        if (statements != null && statements.isArray()) {
            for (JsonNode st : statements) {
                JsonNode filters = st.get("filters");
                if (filters != null && filters.isArray() && filters.size() > 0) {
                    logger.info("  │  过滤条件（语句 {}）:", text(st.get("statement_index")));
                    for (JsonNode f : filters) {
                        logger.info("  │      • {}", f.asText());
                        anyCond = true;
                    }
                }
                JsonNode pf = st.get("partition_filters");
                if (pf != null && pf.isObject() && pf.size() > 0) {
                    logger.info("  │  分区过滤:");
                    java.util.Iterator<Map.Entry<String, JsonNode>> it = pf.fields();
                    while (it.hasNext()) {
                        Map.Entry<String, JsonNode> e = it.next();
                        logger.info("  │      • {} = {}", e.getKey(), text(e.getValue()));
                        anyCond = true;
                    }
                }
            }
        }
        if (!anyCond) {
            logger.info("  │  (无条件)");
        }

        // ---------------------------------------------------------- ④ SQL / 表名
        logger.info("");
        String sqlText = null;
        if (statements != null && statements.isArray()) {
            for (JsonNode st : statements) {
                if (st.hasNonNull("sql")) {
                    sqlText = st.get("sql").asText();
                    break;
                }
            }
        }
        if (sqlText == null || sqlText.trim().isEmpty()) {
            sqlText = parameters.getSql();
        }
        if (sqlText != null && !sqlText.trim().isEmpty()) {
            logger.info("  ┌── ④ 加工 SQL 原文 ─────────────────────────────────────────────");
            for (String ln : sqlText.split("\\r?\\n")) {
                logger.info("  │      {}", ln);
            }
        } else if (parameters.getTable() != null && !parameters.getTable().trim().isEmpty()) {
            logger.info("  ┌── ④ 分析目标表 ────────────────────────────────────────────────");
            logger.info("  │      {}", parameters.getTable());
        }

        // ---------------------------------------------------------- ⑤ 业务口径
        JsonNode knowledge = root.get("knowledge");
        boolean kbAvailable = knowledge != null && !knowledge.isNull()
                && knowledge.path("kb_available").asBoolean(false);
        JsonNode kbMetrics = kbAvailable ? knowledge.get("metrics") : null;
        int metricCount = kbMetrics != null && kbMetrics.isArray() ? kbMetrics.size() : 0;
        // 只有 sql 模式（调 /analyze）才有口径可谈；impact/upstream 保持原来的四段报告
        boolean showKnowledge = knowledge != null || "sql".equals(mode);
        if (showKnowledge) {
            printKnowledgeSection(knowledge, kbAvailable, metricCount);
        }

        // ---------------------------------------------------------- impact/upstream
        JsonNode direction = root.get("direction");
        if (direction != null && !direction.isNull()) {
            logger.info("");
            logger.info("  ┌── {} 分析 ────────────────────────────────────────────────",
                    "downstream".equals(direction.asText()) ? "下游影响" : "上游溯源");
            logger.info("  │  起始表 : {}      图文件 : {}      命中 : {}",
                    text(root.get("start_table")), text(root.get("graph_file")), text(root.get("found")));
            logger.info("  │  结果表数 : {}      边数 : {}",
                    "downstream".equals(direction.asText()) ? text(root.get("downstream_count"))
                            : text(root.get("upstream_count")),
                    text(root.get("edge_count")));
            JsonNode levels = root.get("levels");
            if (levels != null && levels.isArray() && levels.size() > 0) {
                logger.info("  │  分层结果：");
                for (JsonNode lv : levels) {
                    logger.info("  │      第 {} 层 : {}", text(lv.get("level")), flatten(lv.get("tables")));
                }
            }
            JsonNode paths = root.get("paths");
            if (paths != null && paths.isArray() && paths.size() > 0) {
                logger.info("  │  血缘路径（{} 条，最多显示 5 条）:", paths.size());
                int pc = 0;
                for (JsonNode p : paths) {
                    if (pc++ >= 5) {
                        break;
                    }
                    logger.info("  │      {}", flatten(p));
                }
            }
        }

        JsonNode warnings = root.get("warnings");
        if (warnings != null && !warnings.isNull() && warnings.size() > 0) {
            logger.info("");
            logger.info("  ┌── ⚠ 告警 ──────────────────────────────────────────────────────");
            printNode(warnings, 1);
        }

        // ---------------------------------------------------------- 汇总
        logger.info("");
        logger.info("  ══════════════════════════════════════════════════════════════════");
        if (showKnowledge) {
            logger.info("  ✅ 血缘分析完成 | 源表 {} 张 → 目标表 {} 张 | 字段映射 {} 个 | 业务口径命中 {} 条 | 耗时 {} ms",
                    inputTables.size(), outputTables.size(), columnCount, metricCount, cost);
        } else {
            logger.info("  ✅ 血缘分析完成 | 源表 {} 张 → 目标表 {} 张 | 字段映射 {} 个 | 耗时 {} ms",
                    inputTables.size(), outputTables.size(), columnCount, cost);
        }
        logger.info("  ══════════════════════════════════════════════════════════════════");
        logger.info("");

        if ("sql".equals(mode) && inputTables.isEmpty() && outputTables.isEmpty()) {
            logger.warn("未解析出任何输入/输出表，请检查 SQL 与 dialect 是否正确");
        }
    }

    /**
     * ⑤ 业务口径：把服务端 {@code /analyze} 匹配到的知识库口径渲染成中文报告段。
     *
     * <p>知识库不可用 / 无命中 / 服务端是旧版本，都只是多一行提示 —— ①②③④ 段不受影响。
     */
    private void printKnowledgeSection(JsonNode knowledge, boolean kbAvailable, int metricCount) {
        logger.info("");
        logger.info("  ┌── ⑤ 业务口径（知识库匹配）──────────────────────────────────────");
        if (knowledge == null || knowledge.isNull()) {
            logger.info("  │  未匹配到业务口径（可先执行 kb build 建库）");
            logger.info("  │  ⓘ 服务端未返回 knowledge 段：可能是旧版本服务（本插件用 POST /analyze）");
            return;
        }
        if (!kbAvailable) {
            logger.info("  │  未匹配到业务口径（可先执行 kb build 建库）");
            String reason = opt(knowledge.get("reason"));
            if (!reason.isEmpty()) {
                logger.info("  │  原因: {}", reason);
            }
            return;
        }

        JsonNode metrics = knowledge.get("metrics");
        if (metricCount > 0 && metrics != null && metrics.isArray()) {
            logger.info("  │  本任务产出指标的业务口径：");
            int shown = 0;
            final int maxShow = 8;
            for (JsonNode m : metrics) {
                if (shown >= maxShow) {
                    logger.info("  │      ... 其余 {} 条口径已省略", metricCount - maxShow);
                    break;
                }
                shown++;
                String cn = opt(m.get("chinese_name"));
                String column = opt(m.get("target_column"));
                // 知识库里的 formula 是「名称 = 表达式」，名称已在行首，这里只取表达式
                String formula = formulaBody(cn.isEmpty() ? column : cn, opt(m.get("formula")));
                if (formula.isEmpty()) {
                    formula = formulaBody(cn.isEmpty() ? column : cn, opt(m.get("formula_full")));
                }
                logger.info("  │      • {}{} = {}",
                        cn.isEmpty() ? column : cn,
                        column.isEmpty() ? "" : "（" + column + "）",
                        formula.isEmpty() ? "(无公式)" : formula);
                logger.info("  │        类型: {}    置信度: {}    目标表: {}",
                        opt(m.get("metric_type")), opt(m.get("confidence")), opt(m.get("target_table")));
                logger.info("  │        来源: {} 第 {} 条语句",
                        opt(m.get("source_script")), opt(m.get("source_statement")));
                logger.info("  │        依赖: {}", dependsText(m.get("depends_on"), opt(m.get("depends_text"))));
                logger.info("  │        上游链路: {}", flatten(m.get("lineage_path")));
            }
        } else {
            logger.info("  │  知识库已就绪，但本任务产出字段未匹配到已登记指标口径");
        }

        JsonNode terms = knowledge.get("terms");
        if (terms != null && terms.isArray() && terms.size() > 0) {
            logger.info("  │  涉及字段的中文业务名：");
            List<String> cells = new ArrayList<>();
            for (JsonNode t : terms) {
                if (cells.size() >= 16) {
                    cells.add("...");
                    break;
                }
                cells.add("• " + opt(t.get("field")) + " → " + opt(t.get("chinese_name")));
            }
            for (int i = 0; i < cells.size(); i += 2) {
                int end = Math.min(i + 2, cells.size());
                logger.info("  │      {}", joinWith(cells.subList(i, end), "     "));
            }
        }

        JsonNode rules = knowledge.get("rules");
        if (rules != null && rules.isArray() && rules.size() > 0) {
            logger.info("  │  业务规则：");
            int n = 0;
            for (JsonNode r : rules) {
                if (n++ >= 5) {
                    break;
                }
                logger.info("  │      • [{}] {}", opt(r.get("rule_type")), opt(r.get("description")));
            }
        }
        logger.info("  │  ⓘ 口径由 kb build 从加工脚本自动提炼（语法级，未做语义校验）");
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

    /** 依赖字段：优先按结构化 depends_on 拼，服务端没给就退回 depends_text */
    private static String dependsText(JsonNode deps, String fallback) {
        if (deps == null || !deps.isArray() || deps.size() == 0) {
            return fallback == null || fallback.isEmpty() ? "-" : fallback;
        }
        StringBuilder sb = new StringBuilder();
        for (JsonNode d : deps) {
            if (sb.length() > 0) {
                sb.append(", ");
            }
            sb.append(opt(d.get("table"))).append('.').append(opt(d.get("column")));
            String cn = opt(d.get("chinese_name"));
            if (!cn.isEmpty()) {
                sb.append('(').append(cn).append(')');
            }
        }
        return sb.length() == 0 ? "-" : sb.toString();
    }

    /** 取值：缺失一律给空串（区别于 {@link #text(JsonNode)} 的 "-"） */
    private static String opt(JsonNode node) {
        return node == null || node.isNull() ? "" : node.asText();
    }

    /** 用逗号把列表拼成一行 */
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

    /** 把数组/字符串压成一行（用于分层与路径展示） */
    private static String flatten(JsonNode node) {
        if (node == null || node.isNull()) {
            return "-";
        }
        if (node.isArray()) {
            StringBuilder sb = new StringBuilder();
            int i = 0;
            for (JsonNode c : node) {
                if (i++ > 0) {
                    sb.append(" → ");
                }
                sb.append(c.isValueNode() ? c.asText() : flatten(c));
            }
            return sb.toString();
        }
        if (node.isObject()) {
            StringBuilder sb = new StringBuilder();
            java.util.Iterator<Map.Entry<String, JsonNode>> it = node.fields();
            while (it.hasNext()) {
                Map.Entry<String, JsonNode> e = it.next();
                if (sb.length() > 0) {
                    sb.append(" | ");
                }
                sb.append(e.getKey()).append("=")
                        .append(e.getValue().isValueNode() ? e.getValue().asText() : flatten(e.getValue()));
            }
            return sb.toString();
        }
        return node.asText();
    }

    private void printNamed(JsonNode root, String label, String field) {
        JsonNode node = root.get(field);
        if (node == null || node.isNull()) {
            return;
        }
        if (node.isArray() && node.size() == 0) {
            logger.info("{}: []", label);
            return;
        }
        logger.info("{}:", label);
        printNode(node, 1);
    }

    private static String text(JsonNode node) {
        return node == null || node.isNull() ? "-" : node.asText();
    }

    private void printNode(JsonNode node, int indent) {
        String pad = repeat("  ", indent);
        if (node == null || node.isNull()) {
            logger.info("{}null", pad);
            return;
        }
        if (node.isObject()) {
            java.util.Iterator<Map.Entry<String, JsonNode>> it = node.fields();
            while (it.hasNext()) {
                Map.Entry<String, JsonNode> entry = it.next();
                JsonNode value = entry.getValue();
                if (value.isValueNode()) {
                    logger.info("{}{} : {}", pad, entry.getKey(), value.asText());
                } else {
                    logger.info("{}{} :", pad, entry.getKey());
                    printNode(value, indent + 1);
                }
            }
        } else if (node.isArray()) {
            int i = 0;
            for (JsonNode child : node) {
                if (child.isValueNode()) {
                    logger.info("{}[{}] {}", pad, i, child.asText());
                } else {
                    logger.info("{}[{}] :", pad, i);
                    printNode(child, indent + 1);
                }
                i++;
            }
            if (i == 0) {
                logger.info("{}[]", pad);
            }
        } else {
            logger.info("{}{}", pad, node.asText());
        }
    }

    // ---------------------------------------------------------------- out params

    private void collectOutputParameters(JsonNode root, String url, String mode, long cost) {
        List<String> inputTables = readStringArray(root, "input_tables");
        List<String> outputTables = readStringArray(root, "output_tables");

        // 业务口径命中（来自服务端 /analyze 的 knowledge 段）
        JsonNode knowledge = root.get("knowledge");
        boolean kbAvailable = knowledge != null && !knowledge.isNull()
                && knowledge.path("kb_available").asBoolean(false);
        int metricCount = 0;
        List<String> metricNames = new ArrayList<>();
        if (kbAvailable) {
            JsonNode metrics = knowledge.get("metrics");
            if (metrics != null && metrics.isArray()) {
                metricCount = metrics.size();
                for (JsonNode m : metrics) {
                    String name = opt(m.get("chinese_name"));
                    if (name.isEmpty()) {
                        name = opt(m.get("target_column"));
                    }
                    if (!name.isEmpty() && !metricNames.contains(name)) {
                        metricNames.add(name);
                    }
                }
            }
        }

        Map<String, String> output = new LinkedHashMap<>();
        output.put("lineage_mode", mode);
        output.put("lineage_service_url", url);
        output.put("lineage_input_tables", String.join(",", inputTables));
        output.put("lineage_output_tables", String.join(",", outputTables));
        output.put("lineage_input_table_count", String.valueOf(inputTables.size()));
        output.put("lineage_output_table_count", String.valueOf(outputTables.size()));
        output.put("lineage_kb_available", String.valueOf(kbAvailable));
        output.put("lineage_metric_count", String.valueOf(metricCount));
        output.put("lineage_metric_names", String.join(",", metricNames));
        output.put("lineage_cost_ms", String.valueOf(cost));
        String raw = root.toString();
        output.put("lineage_report_raw", raw.length() > 4000 ? raw.substring(0, 4000) : raw);

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

    // ---------------------------------------------------------------- utils

    private static List<String> readStringArray(JsonNode root, String field) {
        List<String> result = new ArrayList<>();
        JsonNode node = root.get(field);
        if (node == null || node.isNull()) {
            return result;
        }
        if (node.isArray()) {
            for (JsonNode child : node) {
                if (child.isValueNode()) {
                    result.add(child.asText());
                } else {
                    result.add(child.toString());
                }
            }
        } else if (node.isValueNode()) {
            result.add(node.asText());
        } else {
            result.add(node.toString());
        }
        return result;
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
