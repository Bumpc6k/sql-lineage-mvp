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
 * SPI entry of the LINEAGE (血缘分析) task plugin.
 *
 * <p>No {@code @AutoService} is used on purpose: the service registration is written
 * by hand into {@code META-INF/services/org.apache.dolphinscheduler.plugin.task.api.TaskChannelFactory}
 * so that the plugin has no build-time dependency besides dolphinscheduler-task-api / -spi / -common.
 */
public class LineageTaskChannelFactory implements TaskChannelFactory {

    public static final String TASK_TYPE = "LINEAGE";

    @Override
    public TaskChannel create() {
        return new LineageTaskChannel();
    }

    @Override
    public String getName() {
        return TASK_TYPE;
    }

    @Override
    public List<PluginParams> getParams() {
        List<PluginParams> paramsList = new ArrayList<>();

        RadioParam mode = RadioParam.newBuilder("mode", "$t('Lineage Mode')")
                .addParamsOptions(new ParamsOptions("$t('SQL lineage (parse SQL)')", "sql", false))
                .addParamsOptions(new ParamsOptions("$t('Downstream impact (by table)')", "impact", false))
                .addParamsOptions(new ParamsOptions("$t('Upstream trace (by table)')", "upstream", false))
                .setValue("sql")
                .addValidate(Validate.newBuilder().setRequired(true).build())
                .build();
        paramsList.add(mode);

        InputParam sql = InputParam.newBuilder("sql", "$t('SQL Content')")
                .setPlaceholder("INSERT INTO dwd.t_order SELECT * FROM ods.t_order_src")
                .setType("textarea")
                .setRows(6)
                .build();
        paramsList.add(sql);

        InputParam dialect = InputParam.newBuilder("dialect", "$t('SQL Dialect')")
                .setPlaceholder("hive")
                .setValue("hive")
                .build();
        paramsList.add(dialect);

        InputParam table = InputParam.newBuilder("table", "$t('Table Name')")
                .setPlaceholder("dwd.t_order")
                .build();
        paramsList.add(table);

        InputParam depth = InputParam.newBuilder("depth", "$t('Depth')")
                .setPlaceholder("3")
                .setValue(3)
                .build();
        paramsList.add(depth);

        InputParam graph = InputParam.newBuilder("graph", "$t('Lineage Graph File')")
                .setPlaceholder("warehouse_graph.json")
                .setValue("warehouse_graph.json")
                .build();
        paramsList.add(graph);

        InputParam serviceUrl = InputParam.newBuilder("serviceUrl", "$t('Lineage Service URL')")
                .setPlaceholder("http://172.17.0.1:18080")
                .setValue("http://172.17.0.1:18080")
                .build();
        paramsList.add(serviceUrl);

        InputParam timeout = InputParam.newBuilder("timeout", "$t('HTTP Timeout (ms)')")
                .setPlaceholder("30000")
                .setValue(30000)
                .build();
        paramsList.add(timeout);

        return paramsList;
    }
}
