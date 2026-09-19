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

import org.apache.dolphinscheduler.plugin.task.api.parameters.AbstractParameters;

import java.util.ArrayList;
import java.util.List;

/**
 * LINEAGE (血缘分析) task parameters.
 *
 * <p>These fields map 1:1 to the JSON payload kept in {@code taskDefinition.taskParams}
 * and to the UI form built by {@link LineageTaskChannelFactory#getParams()}.
 */
public class LineageParameters extends AbstractParameters {

    /** sql | table | impact | upstream */
    private String mode = "sql";

    /** SQL statement, used when mode = sql */
    private String sql;

    /** sql dialect passed to the lineage service, e.g. hive / spark / mysql */
    private String dialect = "hive";

    /** table name, used when mode = table / impact / upstream */
    private String table;

    /** downstream / upstream depth for impact + upstream analysis */
    private int depth = 3;

    /** lineage graph file used by the lineage service */
    private String graph = "warehouse_graph.json";

    /** lineage service base url (container -> host) */
    private String serviceUrl = "http://172.17.0.1:18080";

    /** http timeout in milliseconds */
    private int timeout = 30000;

    /** resource files (kept for interface compatibility, may stay empty) */
    private List<String> resourceList = new ArrayList<>();

    @Override
    public boolean checkParameters() {
        if (serviceUrl == null || serviceUrl.trim().isEmpty()) {
            return false;
        }
        String m = normalizedMode();
        if ("sql".equals(m)) {
            return sql != null && !sql.trim().isEmpty();
        }
        if ("table".equals(m) || "impact".equals(m) || "upstream".equals(m)) {
            return table != null && !table.trim().isEmpty();
        }
        return false;
    }

    /**
     * @return the effective mode, "table" being an alias of "impact".
     */
    public String normalizedMode() {
        if (mode == null || mode.trim().isEmpty()) {
            return "sql";
        }
        String m = mode.trim().toLowerCase();
        return "table".equals(m) ? "impact" : m;
    }

    public String getMode() {
        return mode;
    }

    public void setMode(String mode) {
        this.mode = mode;
    }

    public String getSql() {
        return sql;
    }

    public void setSql(String sql) {
        this.sql = sql;
    }

    public String getDialect() {
        return dialect;
    }

    public void setDialect(String dialect) {
        this.dialect = dialect;
    }

    public String getTable() {
        return table;
    }

    public void setTable(String table) {
        this.table = table;
    }

    public int getDepth() {
        return depth;
    }

    public void setDepth(int depth) {
        this.depth = depth;
    }

    public String getGraph() {
        return graph;
    }

    public void setGraph(String graph) {
        this.graph = graph;
    }

    public String getServiceUrl() {
        return serviceUrl;
    }

    public void setServiceUrl(String serviceUrl) {
        this.serviceUrl = serviceUrl;
    }

    public int getTimeout() {
        return timeout;
    }

    public void setTimeout(int timeout) {
        this.timeout = timeout;
    }

    public List<String> getResourceList() {
        return resourceList;
    }

    public void setResourceList(List<String> resourceList) {
        this.resourceList = resourceList;
    }

    @Override
    public List<org.apache.dolphinscheduler.plugin.task.api.model.ResourceInfo> getResourceFilesList() {
        return new ArrayList<>();
    }

    @Override
    public String toString() {
        return "LineageParameters{" + "mode='" + mode + '\'' + ", dialect='" + dialect + '\'' + ", table='" + table
                + '\'' + ", depth=" + depth + ", graph='" + graph + '\'' + ", serviceUrl='" + serviceUrl + '\''
                + ", timeout=" + timeout + ", sql=" + (sql == null ? "null" : ("<" + sql.length() + " chars>")) + '}';
    }
}
