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

import org.apache.dolphinscheduler.plugin.task.api.TaskChannel;
import org.apache.dolphinscheduler.plugin.task.api.TaskChannelFactory;
import org.apache.dolphinscheduler.spi.params.base.ParamsOptions;
import org.apache.dolphinscheduler.spi.params.base.PluginParams;
import org.apache.dolphinscheduler.spi.params.base.Validate;
import org.apache.dolphinscheduler.spi.params.input.InputParam;
import org.apache.dolphinscheduler.spi.params.radio.RadioParam;

import java.util.ArrayList;
import java.util.List;

/**
 * SPI entry of the LINEAGE_DAG (工作流级血缘分析) task plugin.
 *
 * <p>{@link #getName()} 返回 {@code LINEAGE_DAG} —— 海豚据此在任务类型表里找它，
 * 所以前端 bundle 里的任务类型映射表也必须补上同名键（见 {@code ds-plugin/ui-static/assets/}）。
 *
 * <p>挂位建议：挂在**工作流尾部**（所有 SQL 任务都跑完之后），这样解析到的是「本次真实执行过」
 * 的那套脚本；当然它只读定义、不读执行结果，挂哪儿都能跑。
 */
public class LineageDagTaskChannelFactory implements TaskChannelFactory {

    public static final String TASK_TYPE = "LINEAGE_DAG";

    @Override
    public TaskChannel create() {
        return new LineageDagTaskChannel();
    }

    @Override
    public String getName() {
        return TASK_TYPE;
    }

    @Override
    public List<PluginParams> getParams() {
        List<PluginParams> paramsList = new ArrayList<>();

        RadioParam scope = RadioParam.newBuilder("scope", "$t('Analysis Scope')")
                .addParamsOptions(new ParamsOptions("$t('Current workflow (all tasks)')", "current", false))
                .addParamsOptions(new ParamsOptions("$t('Whole project (all workflows)')", "project", false))
                .setValue("current")
                .addValidate(Validate.newBuilder().setRequired(true).build())
                .build();
        paramsList.add(scope);

        InputParam taskTypes = InputParam.newBuilder("taskTypes", "$t('Task Types')")
                .setPlaceholder("SQL,SHELL,PYTHON")
                .setValue("SQL,SHELL,PYTHON")
                .build();
        paramsList.add(taskTypes);

        InputParam dialect = InputParam.newBuilder("dialect", "$t('SQL Dialect')")
                .setPlaceholder("hive")
                .setValue("hive")
                .build();
        paramsList.add(dialect);

        InputParam includeSubProcess = InputParam.newBuilder("includeSubProcess", "$t('Include Sub Process')")
                .setPlaceholder("false")
                .setValue("false")
                .build();
        paramsList.add(includeSubProcess);

        InputParam serviceUrl = InputParam.newBuilder("serviceUrl", "$t('Lineage Service URL')")
                .setPlaceholder("http://172.17.0.1:18080")
                .setValue("http://172.17.0.1:18080")
                .build();
        paramsList.add(serviceUrl);

        InputParam timeout = InputParam.newBuilder("timeout", "$t('HTTP Timeout (ms)')")
                .setPlaceholder("60000")
                .setValue(60000)
                .build();
        paramsList.add(timeout);

        return paramsList;
    }
}
