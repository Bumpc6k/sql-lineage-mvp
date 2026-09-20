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
import org.apache.dolphinscheduler.plugin.task.api.TaskChannel;
import org.apache.dolphinscheduler.plugin.task.api.TaskExecutionContext;
import org.apache.dolphinscheduler.plugin.task.api.parameters.AbstractParameters;
import org.apache.dolphinscheduler.plugin.task.api.parameters.ParametersNode;
import org.apache.dolphinscheduler.plugin.task.api.parameters.resource.ResourceParametersHelper;

/**
 * TaskChannel of the LINEAGE_DAG (工作流级血缘分析) task.
 *
 * <p>与 {@link LineageTaskChannel} 完全对称，只是参数类换成了 {@link LineageDagParameters}
 * （没有 SQL 字段，脚本由任务运行时从海豚 OpenAPI 拉取）。
 */
public class LineageDagTaskChannel implements TaskChannel {

    @Override
    public void cancelApplication(boolean status) {
        // the workflow lineage task only performs a short blocking HTTP call, nothing to cancel
    }

    @Override
    public LineageDagTask createTask(TaskExecutionContext taskRequest) {
        return new LineageDagTask(taskRequest);
    }

    @Override
    public AbstractParameters parseParameters(ParametersNode parametersNode) {
        return JSONUtils.parseObject(parametersNode.getTaskParams(), LineageDagParameters.class);
    }

    @Override
    public ResourceParametersHelper getResources(String parameters) {
        return null;
    }
}
