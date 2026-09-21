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

import org.apache.dolphinscheduler.plugin.task.api.model.ResourceInfo;
import org.apache.dolphinscheduler.plugin.task.api.parameters.AbstractParameters;

import java.util.ArrayList;
import java.util.List;

/**
 * LINEAGE_DAG (工作流级血缘分析) task parameters.
 *
 * <p>与 {@link LineageParameters} 的区别：这里**没有 SQL 输入框** —— 脚本不是用户填的，
 * 而是任务运行时按 {@code processDefineCode} 从海豚 OpenAPI 把整个工作流的任务脚本
 * 一次性拉下来。所以参数只有「解析谁、怎么解析、调哪个服务」三件事：
 *
 * <ul>
 *   <li>{@code scope} —— {@code current} 只分析本工作流；{@code project} 把项目下所有工作流一起分析；</li>
 *   <li>{@code taskTypes} —— 解析哪些任务类型的脚本（默认 SQL,SHELL,PYTHON）；</li>
 *   <li>{@code includeSubProcess} —— 是否递归展开 SUB_PROCESS 子流程里的任务；</li>
 *   <li>{@code serviceUrl} / {@code timeout} / {@code dialect} —— 血缘服务地址与解析设置。</li>
 * </ul>
 */
public class LineageDagParameters extends AbstractParameters {

    /** current（本工作流） | project（项目下所有工作流） */
    private String scope = "current";

    /** 是否递归展开子流程（SUB_PROCESS）里的任务脚本 */
    private boolean includeSubProcess = false;

    /** 解析哪些任务类型的脚本，逗号分隔；支持 SQL,SHELL,PYTHON,PROCEDURE… */
    private String taskTypes = "SQL,SHELL,PYTHON";

    /** sql dialect passed to the lineage service, e.g. hive / spark */
    private String dialect = "hive";

    /** lineage service base url (container -> host) */
    private String serviceUrl = "http://172.17.0.1:18080";

    /** http timeout in milliseconds（工作流级要多拉几个脚本，默认给到 60s） */
    private int timeout = 60000;

    /** resource files (kept for interface compatibility, may stay empty) */
    private List<String> resourceList = new ArrayList<>();

    @Override
    public boolean checkParameters() {
        return serviceUrl != null && !serviceUrl.trim().isEmpty();
    }

    /**
     * @return the effective scope, 只认 current / project，其余一律按 current 处理。
     */
    public String normalizedScope() {
        if (scope == null) {
            return "current";
        }
        String s = scope.trim().toLowerCase();
        return "project".equals(s) ? "project" : "current";
    }

    /**
     * @return 任务类型列表（大写、去空、去重）；没配就给默认的 SQL/SHELL/PYTHON。
     */
    public List<String> taskTypeList() {
        List<String> out = new ArrayList<>();
        String raw = taskTypes == null ? "" : taskTypes.trim();
        if (raw.isEmpty()) {
            raw = "SQL,SHELL,PYTHON";
        }
        for (String part : raw.split(",")) {
            String item = part.trim().toUpperCase();
            if (!item.isEmpty() && !out.contains(item)) {
                out.add(item);
            }
        }
        return out;
    }

    public String getScope() {
        return scope;
    }

    public void setScope(String scope) {
        this.scope = scope;
    }

    public boolean isIncludeSubProcess() {
        return includeSubProcess;
    }

    public void setIncludeSubProcess(boolean includeSubProcess) {
        this.includeSubProcess = includeSubProcess;
    }

    public String getTaskTypes() {
        return taskTypes;
    }

    public void setTaskTypes(String taskTypes) {
        this.taskTypes = taskTypes;
    }

    public String getDialect() {
        return dialect;
    }

    public void setDialect(String dialect) {
        this.dialect = dialect;
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
    public List<ResourceInfo> getResourceFilesList() {
        return new ArrayList<>();
    }

    @Override
    public String toString() {
        return "LineageDagParameters{" + "scope='" + scope + '\'' + ", includeSubProcess=" + includeSubProcess
                + ", taskTypes='" + taskTypes + '\'' + ", dialect='" + dialect + '\'' + ", serviceUrl='" + serviceUrl
                + '\'' + ", timeout=" + timeout + '}';
    }
}
