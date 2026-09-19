"""单元测试用的 DolphinScheduler OpenAPI 模拟服务（纯标准库 http.server）。

只实现 :mod:`lineage.ds_client` 会用到的那几个接口，返回结构与真实海豚一致
（``{"code":0,"msg":"success","data":...}`` 信封），用来在容器没跑 / 不想连真实
实例的情况下验证客户端与血缘构建逻辑。

真实集成测试在 :mod:`tests.test_ds_client` 末尾，靠 ``pytest.mark.skipif`` 判断
本地 12345 端口是否可达。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 模拟数据
# --------------------------------------------------------------------------- #
MOCK_PROJECT_CODE = 100001
MOCK_WF_ODS_CODE = 200001
MOCK_WF_DWS_CODE = 200002
MOCK_SESSION_ID = "mock-session-0001"
MOCK_GROUPS = 3

SQL_ODS = (
    "INSERT OVERWRITE TABLE ods.ods_卷烟产量流水 PARTITION (dt = '2026-01-01')\n"
    "SELECT a.work_order_no AS work_order_no, a.plant_code AS plant_code\n"
    "FROM src.erp_生产工单明细 a\nWHERE a.dt = '2026-01-01';"
)
SQL_SHELL = (
    '# SHELL 任务里的多语句 SQL\nhive -e "\n'
    "INSERT OVERWRITE TABLE ods.ods_成品库存快照 PARTITION (dt = '2026-01-01')\n"
    "SELECT c.warehouse_code AS warehouse_code FROM src.wms_库存快照 c WHERE c.dt = '2026-01-01';\n"
    "INSERT OVERWRITE TABLE ods.ods_库存基线 PARTITION (dt = '2026-01-01')\n"
    "SELECT c.plant_code AS plant_code FROM src.wms_库存快照 c WHERE c.dt = '2026-01-01';\n"
    '"'
)
SQL_DWS = (
    "INSERT OVERWRITE TABLE cdw.dws_产销存汇总 PARTITION (dt = '2026-01-01')\n"
    "SELECT p.plant_code AS plant_code, SUM(p.output_qty) AS output_qty\n"
    "FROM ods.ods_卷烟产量流水 p WHERE p.dt = '2026-01-01' GROUP BY p.plant_code;"
)


def sql_task(code: int, name: str, sql: str) -> Dict[str, Any]:
    """构造一个 SQL 任务定义（结构对齐真实海豚返回）。"""
    return {
        "id": code % 1000, "code": code, "name": name, "version": 1, "description": f"{name} 节点",
        "projectCode": MOCK_PROJECT_CODE, "taskType": "SQL",
        "taskParams": {"localParams": [], "resourceList": [], "type": "HIVE", "datasource": 1,
                       "sql": sql, "sqlType": "1", "preStatements": [], "postStatements": [],
                       "displayRows": 10},
        "flag": "YES", "isCache": "NO", "taskPriority": "MEDIUM", "workerGroup": "default",
        "environmentCode": -1, "failRetryTimes": 0, "failRetryInterval": 1, "timeoutFlag": "CLOSE",
        "timeout": 0, "delayTime": 0, "taskExecuteType": "BATCH",
    }


def shell_task(code: int, name: str, script: str) -> Dict[str, Any]:
    return {**sql_task(code, name, ""),
            "taskType": "SHELL",
            "taskParams": {"localParams": [], "resourceList": [], "rawScript": script}}


def dependent_task(code: int, name: str, target_definition_code: int) -> Dict[str, Any]:
    return {**sql_task(code, name, ""),
            "taskType": "DEPENDENT",
            "taskParams": {"localParams": [], "resourceList": [],
                           "dependence": {"relation": "AND", "dependTaskList": [
                               {"relation": "AND", "dependItemList": [
                                   {"projectCode": MOCK_PROJECT_CODE,
                                    "definitionCode": target_definition_code,
                                    "depTaskCode": 0, "cycle": "day", "dateValue": "today"}]}]}}}


def relation(pre: int, post: int) -> Dict[str, Any]:
    return {"id": 0, "name": "", "processDefinitionVersion": 1, "projectCode": MOCK_PROJECT_CODE,
            "preTaskCode": pre, "preTaskVersion": 0 if pre == 0 else 1,
            "postTaskCode": post, "postTaskVersion": 1, "conditionType": "NONE",
            "conditionParams": {}}


def default_state() -> Dict[str, Any]:
    """默认模拟数据：2 个工作流（ODS 采集 / DWS 汇总），后者对前者有原生依赖。"""
    ods_tasks = [
        sql_task(300001, "t_ods_产量流水", SQL_ODS),
        shell_task(300002, "t_ods_库存快照", SQL_SHELL),
    ]
    dws_tasks = [
        dependent_task(300003, "t_check_上游就绪", MOCK_WF_ODS_CODE),
        sql_task(300004, "t_dws_产销存汇总", SQL_DWS),
    ]
    return {
        "projects": [{"id": 1, "userId": 1, "userName": "admin", "code": MOCK_PROJECT_CODE,
                      "name": "演示项目", "description": "mock 项目", "defCount": 2, "perm": 0,
                      "instRunningCount": 0, "createTime": "2026-01-01 00:00:00",
                      "updateTime": "2026-01-01 00:00:00"}],
        "definitions": {
            MOCK_PROJECT_CODE: [
                {"id": 1, "code": MOCK_WF_ODS_CODE, "name": "wf_ods_采集", "version": 1,
                 "releaseState": "OFFLINE", "projectCode": MOCK_PROJECT_CODE,
                 "description": "ODS 采集", "globalParams": "[]", "locations": "[]", "timeout": 0,
                 "executionType": "PARALLEL"},
                {"id": 2, "code": MOCK_WF_DWS_CODE, "name": "wf_dws_汇总", "version": 1,
                 "releaseState": "OFFLINE", "projectCode": MOCK_PROJECT_CODE,
                 "description": "DWS 汇总", "globalParams": "[]", "locations": "[]", "timeout": 0,
                 "executionType": "PARALLEL"},
            ],
        },
        "details": {
            (MOCK_PROJECT_CODE, MOCK_WF_ODS_CODE): {
                "processDefinition": {"code": MOCK_WF_ODS_CODE, "name": "wf_ods_采集", "version": 1,
                                      "releaseState": "OFFLINE", "projectCode": MOCK_PROJECT_CODE,
                                      "description": "ODS 采集", "executionType": "PARALLEL"},
                "taskDefinitionList": ods_tasks,
                "processTaskRelationList": [relation(0, 300001), relation(0, 300002)],
            },
            (MOCK_PROJECT_CODE, MOCK_WF_DWS_CODE): {
                "processDefinition": {"code": MOCK_WF_DWS_CODE, "name": "wf_dws_汇总", "version": 1,
                                      "releaseState": "OFFLINE", "projectCode": MOCK_PROJECT_CODE,
                                      "description": "DWS 汇总", "executionType": "PARALLEL"},
                "taskDefinitionList": dws_tasks,
                "processTaskRelationList": [relation(0, 300003), relation(300003, 300004)],
            },
        },
        "datasources": [{"id": 1, "name": "hive_演示", "type": "HIVE", "note": "mock"}],
    }


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "MockDolphinScheduler/1.0"
    protocol_version = "HTTP/1.1"

    # 静音访问日志
    def log_message(self, *args: Any) -> None:  # pragma: no cover
        pass

    # -------------------------------------------------------------- #
    def _body(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype == "application/json":
            try:
                return {}, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {}, {}
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}, {}

    def _send(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, data: Any) -> None:
        self._send({"code": 0, "msg": "success", "data": data, "failed": False, "success": True})

    def _err(self, code: int, msg: str, status: int = 200) -> None:
        self._send({"code": code, "msg": msg, "data": None, "failed": True, "success": False}, status)

    # -------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    # -------------------------------------------------------------- #
    def _handle(self, method: str) -> None:
        state = self.server.state                                    # type: ignore[attr-defined]
        split = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(split.query, keep_blank_values=True).items()}
        form, body = self._body()
        state["requests"].append({
            "method": method, "path": split.path, "query": query,
            # 头部 key 统一小写：urllib 会把 "sessionId" 规范成 "Sessionid"，断言时不该踩大小写的坑
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "form": form, "body": body,
        })

        # 注入错误用：路径里带 /error/<code> 直接回业务错误
        if "/error/" in split.path:
            self._err(int(split.path.rsplit("/", 1)[-1]), "mock 注入的错误")
            return

        if split.path.endswith("/login"):
            if form.get("userPassword") != state["password"]:
                self._err(10001, "user name or password error")
                return
            self._ok({"securityConfigType": "PASSWORD", "sessionId": MOCK_SESSION_ID})
            return
        if split.path.endswith("/logout"):
            self._ok(None)
            return

        # 除登录外都要 sessionId
        if self.headers.get("sessionId") != MOCK_SESSION_ID:
            self._err(10010, "session timeout, please login again")
            return

        path = split.path
        if path.endswith("/projects") and method == "GET":
            self._ok(self._paged(state["projects"], query))
            return
        if path.endswith("/projects") and method == "POST":
            project = {"id": 9, "code": state["next_project_code"], "name": query.get("projectName"),
                       "description": query.get("description") or "", "defCount": 0, "perm": 0}
            state["projects"].append(project)
            state["next_project_code"] += 1
            self._ok(project)
            return
        if path.endswith("/datasources") and method == "GET":
            self._ok(self._paged(state["datasources"], query))
            return
        if path.endswith("/datasources") and method == "POST":
            created = {"id": len(state["datasources"]) + 1, "name": body.get("name"),
                       "type": body.get("type"), "note": body.get("note")}
            state["datasources"].append(created)
            self._ok(created)
            return

        m = re.fullmatch(r".*/projects/(\d+)", path)
        if m and method == "DELETE":
            code = int(m.group(1))
            if state["definitions"].get(code):
                self._err(10137, "please delete the process definitions in project first!")
                return
            state["projects"] = [p for p in state["projects"] if p["code"] != code]
            self._ok(None)
            return

        m = re.fullmatch(r".*/projects/(\d+)/task-definition/gen-task-codes", path)
        if m:
            num = int(query.get("genNum") or 1)
            state["task_code_seq"] += num
            self._ok(list(range(state["task_code_seq"] - num + 1, state["task_code_seq"] + 1)))
            return

        m = re.fullmatch(r".*/projects/(\d+)/task-definition/save-single", path)
        if m:
            wf_code = int(form["processDefinitionCode"])
            task = json.loads(form["taskDefinitionJsonObj"])
            detail = state["details"].get((int(m.group(1)), wf_code))
            if detail is None:
                self._err(10016, "process definition not found")
                return
            detail["taskDefinitionList"].append(task)
            upstream = [int(c) for c in (form.get("upstreamCodes") or "").split(",") if c]
            for up in upstream or [0]:
                detail["processTaskRelationList"].append(relation(up, task["code"]))
            self._ok(task)
            return

        m = re.fullmatch(r".*/projects/(\d+)/process-definition", path)
        if m and method == "GET":
            self._ok(self._paged(state["definitions"].get(int(m.group(1)), []), query))
            return
        if m and method == "POST":
            self._create_definition(int(m.group(1)), form)
            return

        m = re.fullmatch(r".*/projects/(\d+)/process-definition/(\d+)", path)
        if m and method == "GET":
            detail = state["details"].get((int(m.group(1)), int(m.group(2))))
            if detail is None:
                self._err(10016, "process definition not found")
                return
            self._ok(detail)
            return
        if m and method == "DELETE":
            key = (int(m.group(1)), int(m.group(2)))
            state["details"].pop(key, None)
            state["definitions"][int(m.group(1))] = [
                d for d in state["definitions"].get(int(m.group(1)), []) if d["code"] != key[1]]
            self._ok(None)
            return

        m = re.fullmatch(r".*/projects/(\d+)/task-definition", path)
        if m and method == "GET":
            tasks: List[Dict[str, Any]] = []
            for (pc, _code), detail in state["details"].items():
                if pc == int(m.group(1)):
                    tasks.extend(detail["taskDefinitionList"])
            self._ok(self._paged(tasks, query))
            return

        self._err(10111, f"mock 未实现的接口：{method} {path}", status=404)

    # -------------------------------------------------------------- #
    @staticmethod
    def _paged(items: List[Dict[str, Any]], query: Dict[str, str]) -> Dict[str, Any]:
        page = int(query.get("pageNo") or 1)
        size = int(query.get("pageSize") or 10)
        start = (page - 1) * size
        return {"totalList": items[start:start + size], "total": len(items),
                "totalPage": max(1, (len(items) + size - 1) // size),
                "pageSize": size, "currentPage": page, "pageNo": page - 1}

    def _create_definition(self, project_code: int, form: Dict[str, Any]) -> None:
        state = self.server.state                                     # type: ignore[attr-defined]
        try:
            tasks = json.loads(form.get("taskDefinitionJson") or "[]")
            relations = json.loads(form.get("taskRelationJson") or "[]")
        except json.JSONDecodeError:
            self._err(50017, "data not valid")
            return
        if not tasks:
            self._err(50017, "data [] not valid")
            return
        state["next_definition_code"] += 1
        code = state["next_definition_code"]
        definition = {"code": code, "name": form.get("name"), "version": 1,
                      "releaseState": "OFFLINE", "projectCode": project_code,
                      "description": form.get("description") or "",
                      "executionType": form.get("executionType") or "PARALLEL"}
        state["definitions"].setdefault(project_code, []).append(definition)
        state["details"][(project_code, code)] = {
            "processDefinition": definition,
            "taskDefinitionList": tasks,
            "processTaskRelationList": relations,
        }
        self._ok(definition)


class MockDsServer:
    """线程化的模拟海豚服务，``with MockDsServer() as srv: srv.url`` 即可用。"""

    def __init__(self, **overrides: Any) -> None:
        state = default_state()
        state.update(overrides)
        state.setdefault("password", "dolphinscheduler123")
        state.setdefault("next_project_code", MOCK_PROJECT_CODE + 1000)
        state.setdefault("next_definition_code", 900000)
        state.setdefault("task_code_seq", 400000)
        state["requests"] = []
        self.state = state
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    # -------------------------------------------------------------- #
    @property
    def url(self) -> str:
        assert self.httpd is not None, "服务还没启动"
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/dolphinscheduler"

    @property
    def requests(self) -> List[Dict[str, Any]]:
        return self.state["requests"]

    def find(self, path_suffix: str, method: Optional[str] = None) -> List[Dict[str, Any]]:
        """按路径后缀筛选已收到的请求（便于断言入参）。"""
        return [r for r in self.requests
                if r["path"].endswith(path_suffix) and (method is None or r["method"] == method)]

    def start(self) -> "MockDsServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.state = self.state                                 # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    def __enter__(self) -> "MockDsServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


__all__ = [
    "MockDsServer",
    "MOCK_PROJECT_CODE",
    "MOCK_WF_ODS_CODE",
    "MOCK_WF_DWS_CODE",
    "MOCK_SESSION_ID",
    "MOCK_GROUPS",
    "SQL_ODS",
    "SQL_SHELL",
    "SQL_DWS",
    "default_state",
    "sql_task",
    "shell_task",
    "dependent_task",
    "relation",
]
