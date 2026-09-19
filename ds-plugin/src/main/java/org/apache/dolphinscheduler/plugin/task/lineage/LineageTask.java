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
        String endpoint;
        String requestBody;
        String action;
        switch (mode) {
            case "impact":
                endpoint = "/impact";
                action = "下游影响分析 (downstream impact)";
                requestBody = buildTableBody();
                break;
            case "upstream":
                endpoint = "/upstream";
                action = "上游溯源 (upstream trace)";
                requestBody = buildTableBody();
                break;
            case "sql":
            default:
                endpoint = "/parse";
                action = "SQL 血缘解析 (parse SQL)";
                requestBody = buildSqlBody();
                break;
        }

        String url = serviceUrl + endpoint;
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
            response = LineageServiceClient.postJson(url, requestBody, parameters.getTimeout());
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

        printReport(root, mode);
        logger.info("耗时       : {} ms", cost);
        logger.info("{}", HEADER);

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

    private void printReport(JsonNode root, String mode) {
        logger.info("---------------- 血缘报告 (lineage report) ----------------");
        List<String> inputTables = readStringArray(root, "input_tables");
        List<String> outputTables = readStringArray(root, "output_tables");
        if (!inputTables.isEmpty()) {
            logger.info("输入表 input_tables ({}):", inputTables.size());
            for (String t : inputTables) {
                logger.info("    <- {}", t);
            }
        }
        if (!outputTables.isEmpty()) {
            logger.info("输出表 output_tables ({}):", outputTables.size());
            for (String t : outputTables) {
                logger.info("    -> {}", t);
            }
        }

        JsonNode tableLineage = root.get("table_lineage");
        if (tableLineage != null && !tableLineage.isNull()) {
            logger.info("表级血缘 table_lineage:");
            printNode(tableLineage, 1);
        }
        JsonNode columnLineage = root.get("column_lineage");
        if (columnLineage != null && !columnLineage.isNull()) {
            logger.info("字段级血缘 column_lineage:");
            printNode(columnLineage, 1);
        }
        JsonNode impact = root.get("impact");
        if (impact != null && !impact.isNull()) {
            logger.info("影响面 impact:");
            printNode(impact, 1);
        }
        JsonNode upstream = root.get("upstream");
        if (upstream != null && !upstream.isNull()) {
            logger.info("上游列表 upstream:");
            printNode(upstream, 1);
        }
        // /impact + /upstream response shape
        JsonNode direction = root.get("direction");
        if (direction != null && !direction.isNull()) {
            logger.info("分析方向 direction   : {}", direction.asText());
            logger.info("起始表   start_table : {}", text(root.get("start_table")));
            logger.info("图文件   graph_file  : {}", text(root.get("graph_file")));
            logger.info("是否命中 found       : {}", text(root.get("found")));
            logger.info("下游表数 downstream_count : {}", text(root.get("downstream_count")));
            logger.info("上游表数 upstream_count   : {}", text(root.get("upstream_count")));
            logger.info("边数     edge_count  : {}", text(root.get("edge_count")));
            printNamed(root, "直接下游/上游 direct", "direct");
            printNamed(root, "分层 levels", "levels");
            printNamed(root, "涉及表 tables", "tables");
            printNamed(root, "路径 paths", "paths");
        }
        JsonNode warnings = root.get("warnings");
        if (warnings != null && !warnings.isNull()) {
            logger.info("告警 warnings:");
            printNode(warnings, 1);
        }

        // print any other top level key we did not handle explicitly
        java.util.Iterator<String> names = root.fieldNames();
        while (names.hasNext()) {
            String name = names.next();
            if ("input_tables".equals(name) || "output_tables".equals(name) || "table_lineage".equals(name)
                    || "column_lineage".equals(name) || "impact".equals(name) || "upstream".equals(name)
                    || "warnings".equals(name) || "code".equals(name) || "message".equals(name)
                    || "success".equals(name) || "direction".equals(name) || "start_table".equals(name)
                    || "graph_file".equals(name) || "found".equals(name) || "downstream_count".equals(name)
                    || "upstream_count".equals(name) || "edge_count".equals(name) || "direct".equals(name)
                    || "levels".equals(name) || "tables".equals(name) || "paths".equals(name)
                    || "dialect".equals(name) || "statement_count".equals(name)
                    || "column_lineage_count".equals(name)) {
                continue;
            }
            JsonNode node = root.get(name);
            logger.info("{}:", name);
            printNode(node, 1);
        }
        logger.info("---------------- 血缘报告结束 (end of report) --------------");
        if ("sql".equals(mode) && inputTables.isEmpty() && outputTables.isEmpty()) {
            logger.warn("未解析出任何输入/输出表，请检查 SQL 与 dialect 是否正确");
        }
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

        Map<String, String> output = new LinkedHashMap<>();
        output.put("lineage_mode", mode);
        output.put("lineage_service_url", url);
        output.put("lineage_input_tables", String.join(",", inputTables));
        output.put("lineage_output_tables", String.join(",", outputTables));
        output.put("lineage_input_table_count", String.valueOf(inputTables.size()));
        output.put("lineage_output_table_count", String.valueOf(outputTables.size()));
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
