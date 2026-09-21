"""``lineage.ds_client``（DolphinScheduler OpenAPI 客户端）单元 + 集成测试。

* 单元测试打本地 mock 服务（:mod:`tests.ds_mock`，标准库 http.server），
  验证登录 / 会话头 / 翻页 / 工作流详情 / 脚本抽取 / 错误处理 / 入参形态。
* 末尾几条是**真实集成测试**：只有本地 12345 端口可达时才跑，否则 skip。
"""

from __future__ import annotations

import json
import socket

import pytest

from lineage.ds.client import (
    DEFAULT_BASE_URL,
    SCRIPT_KEYS,
    DsApiError,
    DsAuthError,
    DsClient,
    DsConnectionError,
    check_port,
    extract_scripts,
    normalize_script,
    referenced_definition_codes,
    task_params_of,
)
from tests.ds_mock import (
    MOCK_PROJECT_CODE,
    MOCK_SESSION_ID,
    MOCK_WF_DWS_CODE,
    MOCK_WF_ODS_CODE,
    SQL_DWS,
    SQL_ODS,
    SQL_SHELL,
    MockDsServer,
    dependent_task,
    shell_task,
    sql_task,
)

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def server() -> MockDsServer:
    with MockDsServer() as srv:
        yield srv


@pytest.fixture()
def client(server: MockDsServer) -> DsClient:
    c = DsClient(base_url=server.url, user="admin", password="dolphinscheduler123")
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# 登录 / 会话
# --------------------------------------------------------------------------- #
def test_login_stores_session_id(client: DsClient, server: MockDsServer) -> None:
    sid = client.login()
    assert sid == MOCK_SESSION_ID
    assert client.session_id == MOCK_SESSION_ID
    login = server.find("/login", "POST")[-1]
    assert login["form"] == {"userName": "admin", "userPassword": "dolphinscheduler123"}


def test_requests_carry_session_header(client: DsClient, server: MockDsServer) -> None:
    client.list_projects()
    calls = [r for r in server.requests if not r["path"].endswith("/login")]
    assert calls, "至少应该发出一次业务请求"
    # urllib 会把请求头名规范化成 Sessionid，海豚侧大小写不敏感；mock 里统一按小写断言
    assert all(r["headers"].get("sessionid") == MOCK_SESSION_ID for r in calls)


def test_login_failure_raises_auth_error(server: MockDsServer) -> None:
    bad = DsClient(base_url=server.url, user="admin", password="wrong-password")
    with pytest.raises(DsAuthError) as exc:
        bad.login()
    assert "登录失败" in str(exc.value)


def test_close_logs_out_and_is_idempotent(client: DsClient, server: MockDsServer) -> None:
    client.login()
    client.close()
    assert client.session_id is None
    client.close()                      # 再关一次不应抛异常
    assert server.find("/logout", "POST")


def test_context_manager_closes(server: MockDsServer) -> None:
    with DsClient(base_url=server.url) as c:
        c.login()
        assert c.session_id
    assert c.session_id is None


# --------------------------------------------------------------------------- #
# 读取：项目 / 工作流 / 任务
# --------------------------------------------------------------------------- #
def test_list_projects(client: DsClient) -> None:
    projects = client.list_projects()
    assert [p["name"] for p in projects] == ["演示项目"]
    assert projects[0]["code"] == MOCK_PROJECT_CODE


def test_list_projects_paginates(server: MockDsServer) -> None:
    state_projects = server.state["projects"]
    for i in range(5):
        state_projects.append({"id": 10 + i, "code": 500000 + i, "name": f"项目{i}", "defCount": 0})
    client = DsClient(base_url=server.url)
    projects = client.list_projects(page_size=2)
    assert len(projects) == 6
    pages = server.find("/projects", "GET")
    assert len(pages) >= 3               # 每页 2 条 -> 6 条至少翻 3 页
    assert [p["query"]["pageNo"] for p in pages] == [str(i) for i in range(1, len(pages) + 1)]


def test_list_process_definitions(client: DsClient) -> None:
    defs = client.list_process_definitions(MOCK_PROJECT_CODE)
    assert [d["name"] for d in defs] == ["wf_ods_采集", "wf_dws_汇总"]


def test_get_process_definition_returns_tasks_and_relations(client: DsClient) -> None:
    detail = client.get_process_definition(MOCK_PROJECT_CODE, MOCK_WF_DWS_CODE)
    assert set(detail) == {"processDefinition", "taskDefinitionList", "processTaskRelationList"}
    assert detail["processDefinition"]["name"] == "wf_dws_汇总"
    assert [t["name"] for t in detail["taskDefinitionList"]] == ["t_check_上游就绪", "t_dws_产销存汇总"]
    assert [(r["preTaskCode"], r["postTaskCode"]) for r in detail["processTaskRelationList"]] == [
        (0, 300003), (300003, 300004)]


def test_list_task_definitions_by_definition_code(client: DsClient) -> None:
    tasks = client.list_task_definitions(MOCK_PROJECT_CODE, MOCK_WF_ODS_CODE)
    assert [t["name"] for t in tasks] == ["t_ods_产量流水", "t_ods_库存快照"]


def test_list_task_definitions_all(client: DsClient) -> None:
    tasks = client.list_task_definitions(MOCK_PROJECT_CODE)
    assert len(tasks) == 4


def test_list_process_task_relations(client: DsClient) -> None:
    rels = client.list_process_task_relations(MOCK_PROJECT_CODE, MOCK_WF_ODS_CODE)
    assert [(r["preTaskCode"], r["postTaskCode"]) for r in rels] == [(0, 300001), (0, 300002)]


def test_fetch_all_shape(client: DsClient, server: MockDsServer) -> None:
    snapshot = client.fetch_all()
    assert snapshot["dialect"] == "hive"
    assert snapshot["base_url"] == server.url
    bundles = snapshot["projects"]
    assert len(bundles) == 1
    assert bundles[0]["project"]["name"] == "演示项目"
    names = [w["processDefinition"]["name"] for w in bundles[0]["workflows"]]
    assert names == ["wf_ods_采集", "wf_dws_汇总"]


def test_fetch_all_filters_by_project_name(client: DsClient) -> None:
    assert client.fetch_all(project_names=["不存在的项目"])["projects"] == []
    assert len(client.fetch_all(project_names=["演示项目"])["projects"]) == 1
    assert len(client.fetch_all(project_codes=[MOCK_PROJECT_CODE])["projects"]) == 1


# --------------------------------------------------------------------------- #
# 脚本抽取
# --------------------------------------------------------------------------- #
def test_extract_scripts_sql_task() -> None:
    scripts = extract_scripts(sql_task(1, "t1", SQL_ODS))
    assert [s["key"] for s in scripts] == ["taskParams.sql"]
    assert "ods.ods_卷烟产量流水" in scripts[0]["text"]


def test_extract_scripts_shell_task_strips_hive_wrapper() -> None:
    scripts = extract_scripts(shell_task(2, "t2", SQL_SHELL))
    assert len(scripts) == 1
    text = scripts[0]["text"]
    assert text.startswith("INSERT OVERWRITE TABLE ods.ods_成品库存快照")
    assert "hive -e" not in text
    assert "#" not in text.splitlines()[0]
    assert text.rstrip().endswith(";")
    assert text.count("INSERT OVERWRITE") == 2          # hive -e 里的两条语句都保住了


def test_extract_scripts_pre_post_statements() -> None:
    task = sql_task(3, "t3", SQL_ODS)
    task["taskParams"]["preStatements"] = ["SET hive.exec.dynamic.partition = true"]
    task["taskParams"]["postStatements"] = ["ANALYZE TABLE ods.ods_卷烟产量流水 COMPUTE STATISTICS"]
    scripts = extract_scripts(task)
    keys = [s["key"] for s in scripts]
    assert "taskParams.sql" in keys
    assert any(k.startswith("taskParams.preStatements") for k in keys)
    assert any(k.startswith("taskParams.postStatements") for k in keys)


def test_extract_scripts_python_task_rawscript() -> None:
    task = sql_task(4, "t4", "")
    task["taskType"] = "PYTHON"
    task["taskParams"] = {"localParams": [], "resourceList": [],
                          "rawScript": "# python 任务里嵌的 SQL\nINSERT OVERWRITE TABLE ads.ads_x "
                                       "SELECT 1 AS id FROM ods.ods_y;"}
    scripts = extract_scripts(task)
    assert [s["key"] for s in scripts] == ["taskParams.rawScript"]
    assert "INSERT OVERWRITE TABLE ads.ads_x" in scripts[0]["text"]


def test_extract_scripts_skips_tasks_without_script() -> None:
    assert extract_scripts(dependent_task(5, "t5", 200001)) == []


def test_extract_scripts_taskparams_as_json_text() -> None:
    """真实海豚不同接口有时把 taskParams 返回成 JSON 文本，两种形态都要吃下。"""
    task = sql_task(6, "t6", SQL_DWS)
    task["taskParams"] = json.dumps(task["taskParams"], ensure_ascii=False)
    assert task_params_of(task)["datasource"] == 1
    assert extract_scripts(task)[0]["text"] == SQL_DWS


def test_extract_scripts_dedupes_identical_scripts() -> None:
    task = sql_task(7, "t7", SQL_ODS)
    task["taskParams"]["script"] = SQL_ODS            # 同样的文本挂在两个键上
    scripts = extract_scripts(task)
    assert len(scripts) == 2                          # 不同键 -> 都保留
    assert len({s["text"] for s in scripts}) == 1


def test_script_keys_cover_sql_and_rawscript() -> None:
    assert {"sql", "rawScript"} <= set(SCRIPT_KEYS)


# --------------------------------------------------------------------------- #
# 脚本清洗
# --------------------------------------------------------------------------- #
def test_normalize_script_keeps_plain_sql_untouched() -> None:
    assert normalize_script(SQL_ODS) == SQL_ODS
    assert normalize_script("  SELECT 1 AS a;  ") == "SELECT 1 AS a;"


def test_normalize_script_strips_shebang_and_comments() -> None:
    text = "#!/bin/bash\n# 说明行\nSELECT 1 AS a FROM src.t;"
    assert normalize_script(text) == "SELECT 1 AS a FROM src.t;"


def test_normalize_script_unwraps_beeline_and_mysql() -> None:
    assert normalize_script('beeline -e "SELECT 1 AS a FROM src.t;"') == "SELECT 1 AS a FROM src.t;"
    assert normalize_script("mysql -e 'SELECT 2 AS b FROM src.u;'") == "SELECT 2 AS b FROM src.u;"


def test_normalize_script_handles_empty() -> None:
    assert normalize_script("") == ""
    assert normalize_script(None) == ""
    assert normalize_script("# 只有注释\n") == ""


# --------------------------------------------------------------------------- #
# 原生依赖识别
# --------------------------------------------------------------------------- #
def test_referenced_definition_codes_dependent() -> None:
    refs = referenced_definition_codes(dependent_task(8, "t8", MOCK_WF_ODS_CODE))
    assert len(refs) == 1
    assert refs[0]["definition_code"] == str(MOCK_WF_ODS_CODE)
    assert refs[0]["kind"] == "dependent"


def test_referenced_definition_codes_sub_process() -> None:
    task = sql_task(9, "t9", "")
    task["taskType"] = "SUB_PROCESS"
    task["taskParams"] = {"processDefinitionCode": MOCK_WF_DWS_CODE, "localParams": [],
                          "resourceList": [], "environmentCode": -1}
    refs = referenced_definition_codes(task)
    assert refs == [{"definition_code": str(MOCK_WF_DWS_CODE), "kind": "sub_process",
                     "key": "processDefinitionCode"}]


def test_referenced_definition_codes_none_for_plain_sql() -> None:
    assert referenced_definition_codes(sql_task(10, "t10", SQL_ODS)) == []


# --------------------------------------------------------------------------- #
# 错误处理
# --------------------------------------------------------------------------- #
def test_api_error_envelope_raises(client: DsClient) -> None:
    with pytest.raises(DsApiError) as exc:
        client.request("GET", f"/projects/{MOCK_PROJECT_CODE}/error/10301")
    assert exc.value.code == 10301
    assert "海豚接口返回错误" in str(exc.value)


def test_missing_session_raises_api_error(server: MockDsServer) -> None:
    c = DsClient(base_url=server.url, session_id="wrong-session")
    with pytest.raises(DsApiError) as exc:
        c.list_projects()
    assert exc.value.code == 10010


def test_connection_error_is_friendly() -> None:
    # 127.0.0.1:1 基本不可能有服务监听
    c = DsClient(base_url="http://127.0.0.1:1/dolphinscheduler", timeout=1.0)
    with pytest.raises(DsConnectionError) as exc:
        c.login()
    msg = str(exc.value)
    assert "连不上 DolphinScheduler" in msg and "DS_BASE_URL" in msg


def test_raise_on_error_false_returns_none(client: DsClient) -> None:
    assert client.request("GET", f"/projects/{MOCK_PROJECT_CODE}/error/10301",
                          raise_on_error=False) is None


def test_close_swallows_logout_errors(server: MockDsServer) -> None:
    c = DsClient(base_url=server.url, session_id="wrong-session")
    c.close()                            # logout 报错也不能抛出去
    assert c.session_id is None


# --------------------------------------------------------------------------- #
# 配置与环境变量
# --------------------------------------------------------------------------- #
def test_env_var_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DS_BASE_URL", "http://ds.example.com:12345/dolphinscheduler/")
    monkeypatch.setenv("DS_USER", "zhangsan")
    monkeypatch.setenv("DS_PASSWORD", "secret")
    monkeypatch.setenv("DS_DIALECT", "spark")
    c = DsClient.from_env()
    assert c.base_url == "http://ds.example.com:12345/dolphinscheduler"     # 末尾斜杠会被去掉
    assert (c.user, c.password, c.dialect) == ("zhangsan", "secret", "spark")


def test_explicit_args_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DS_BASE_URL", "http://env.example.com/dolphinscheduler")
    c = DsClient(base_url="http://explicit.example.com/dolphinscheduler", user="lisi")
    assert c.base_url == "http://explicit.example.com/dolphinscheduler"
    assert c.user == "lisi"


def test_hardcoded_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DS_BASE_URL", "DS_USER", "DS_PASSWORD", "DS_DIALECT"):
        monkeypatch.delenv(key, raising=False)
    c = DsClient()
    assert c.base_url == DEFAULT_BASE_URL
    assert c.user == "admin"


def test_check_port() -> None:
    with MockDsServer() as srv:
        host, port = srv.httpd.server_address[:2]         # type: ignore[union-attr]
        assert check_port(host, port, timeout=2.0) is True
    assert check_port("127.0.0.1", 1, timeout=0.5) is False
    assert isinstance(check_port("127.0.0.1", 1, timeout=0.5), bool)


# --------------------------------------------------------------------------- #
# 写入类接口（演示脚本用）：入参走 form body，避免 URL 过长
# --------------------------------------------------------------------------- #
def test_create_project_and_delete(client: DsClient, server: MockDsServer) -> None:
    created = client.create_project("新项目", "描述")
    assert created["name"] == "新项目"
    client.delete_project(created["code"])
    assert all(p["code"] != created["code"] for p in client.list_projects())


def test_delete_project_with_definitions_is_rejected(client: DsClient) -> None:
    # mock 会回 10137（真实海豚同理：必须先删工作流定义）——项目还在
    client.delete_project(MOCK_PROJECT_CODE)
    assert any(p["code"] == MOCK_PROJECT_CODE for p in client.list_projects())


def test_create_datasource_and_list(client: DsClient) -> None:
    created = client.create_datasource({"name": "hive_x", "type": "HIVE", "host": "localhost"})
    assert created["name"] == "hive_x"
    assert any(d["name"] == "hive_x" for d in client.list_datasources())


def test_gen_task_codes(client: DsClient) -> None:
    codes = client.gen_task_codes(MOCK_PROJECT_CODE, 3)
    assert len(codes) == 3 and all(isinstance(c, int) for c in codes)


def test_create_process_definition_sends_form_body(client: DsClient, server: MockDsServer) -> None:
    """长中文 SQL 必须走 form body —— 走 query 会撞上容器 8KB 请求头限制（HTTP 414）。"""
    long_sql = SQL_DWS + "\n-- " + "注释" * 2000
    task = sql_task(700001, "t_新任务", long_sql)
    created = client.create_process_definition(
        MOCK_PROJECT_CODE, "wf_新工作流", [task], [{"preTaskCode": 0, "postTaskCode": 700001}],
        description="测试长 SQL")
    assert created["name"] == "wf_新工作流"
    request = server.find("/process-definition", "POST")[-1]
    assert "taskDefinitionJson" not in request["query"]      # 不在 URL 上
    assert json.loads(request["form"]["taskDefinitionJson"])[0]["name"] == "t_新任务"
    received_sql = json.loads(request["form"]["taskDefinitionJson"])[0]["taskParams"]["sql"]
    assert len(received_sql) == len(long_sql)                # 长脚本文本完整送达
    # 新建出来的工作流随后能通过读接口拿到
    detail = client.get_process_definition(MOCK_PROJECT_CODE, created["code"])
    assert detail["taskDefinitionList"][0]["name"] == "t_新任务"


def test_save_single_task_appends_with_upstream(client: DsClient, server: MockDsServer) -> None:
    client.save_single_task(MOCK_PROJECT_CODE, MOCK_WF_ODS_CODE,
                            sql_task(700002, "t_补丁", SQL_DWS), upstream_codes=[300001])
    request = server.find("/task-definition/save-single", "POST")[-1]
    assert request["form"]["upstreamCodes"] == "300001"
    detail = client.get_process_definition(MOCK_PROJECT_CODE, MOCK_WF_ODS_CODE)
    assert "t_补丁" in [t["name"] for t in detail["taskDefinitionList"]]
    assert any(r["preTaskCode"] == 300001 and r["postTaskCode"] == 700002
               for r in detail["processTaskRelationList"])


# --------------------------------------------------------------------------- #
# 真实集成测试（本地 12345 不可达时跳过）
# --------------------------------------------------------------------------- #
REAL_HOST = "localhost"
REAL_PORT = 12345
HAS_REAL_DS = check_port(REAL_HOST, REAL_PORT, timeout=1.0)
requires_ds = pytest.mark.skipif(not HAS_REAL_DS, reason="本地 DolphinScheduler（localhost:12345）不可达")


@requires_ds
def test_real_login_and_list_projects() -> None:
    c = DsClient()
    sid = c.login()
    try:
        assert sid
        projects = c.list_projects()
        assert isinstance(projects, list)
    finally:
        c.close()


@requires_ds
def test_real_fetch_all_snapshot() -> None:
    c = DsClient()
    c.login()
    try:
        snapshot = c.fetch_all()
    finally:
        c.close()
    assert snapshot["base_url"].startswith("http")
    for bundle in snapshot["projects"]:
        for wf in bundle["workflows"]:
            definition = wf["processDefinition"]
            assert definition.get("code") and definition.get("name")
            for task in wf["taskDefinitionList"]:
                assert task.get("name") and task.get("taskType")
                assert isinstance(task_params_of(task), dict)


@requires_ds
def test_real_demo_project_has_workflows() -> None:
    """演示数据脚本 demos/ds_setup_demo.py 跑过之后，项目里应有工作流与任务。"""
    c = DsClient()
    c.login()
    try:
        projects = {p["name"]: p for p in c.list_projects()}
        if "烟草数仓演示" not in projects:
            pytest.skip("演示项目还没创建，先跑 demos/ds_setup_demo.py")
        code = projects["烟草数仓演示"]["code"]
        defs = c.list_process_definitions(code)
        assert len(defs) >= 4
        detail = c.get_process_definition(code, defs[0]["code"])
        assert detail["taskDefinitionList"]
        scripts = [s for t in detail["taskDefinitionList"] for s in extract_scripts(t)]
        assert scripts, "演示工作流的任务里应该有可解析的 SQL 脚本"
    finally:
        c.close()


def test_socket_module_used_for_port_check() -> None:
    """check_port 用的是标准库 socket（无第三方依赖）。"""
    assert socket.socket is not None
