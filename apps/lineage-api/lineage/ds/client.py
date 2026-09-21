"""DolphinScheduler OpenAPI 客户端（P3 旁路集成）。

设计要点
--------
* **零第三方依赖**：只用标准库 ``urllib``，不引入 requests 等，也不改海豚一行源码；
  所有数据都通过海豚自己的 OpenAPI（``/dolphinscheduler`` 前缀）读取。
* **只读为主**：``sync`` 相关方法全是 GET；写入类方法（建项目 / 建工作流 / 建数据源）
  只服务于 :file:`demos/ds_setup_demo.py` 这类演示数据脚本，标注了 [写入]。
* **脚本抽取通用化**：SQL 任务取 ``taskParams.sql``，SHELL / PYTHON 任务取
  ``taskParams.rawScript``，并顺带把 ``preStatements`` / ``postStatements`` 一并取出；
  抽取逻辑对键名做递归扫描，因此新增任务类型只要还叫 ``sql`` / ``rawScript`` 也能吃下。

环境变量（都可被构造参数覆盖）::

    DS_BASE_URL  默认 http://localhost:12345/dolphinscheduler
    DS_USER      默认 admin
    DS_PASSWORD  默认 dolphinscheduler123
    DS_DIALECT   默认 hive
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL = "http://localhost:12345/dolphinscheduler"
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "dolphinscheduler123"
DEFAULT_DIALECT = "hive"
DEFAULT_TIMEOUT = 30.0
DEFAULT_PAGE_SIZE = 100
#: 写入类接口生成的 task code 起始值（海豚要求 19 位以内的正整数）
TASK_CODE_BASE = 90000000000000000

#: taskParams 里可能存放脚本文本的键，按优先级排列
SCRIPT_KEYS: Tuple[str, ...] = (
    "sql",
    "rawScript",
    "script",
    "shellScript",
    "sqlScript",
    "sqlText",
    "statement",
)
#: 这些键是「语句数组」（SQL 任务的预处理 / 后置语句）
SCRIPT_LIST_KEYS: Tuple[str, ...] = ("preStatements", "postStatements")
#: 匹配时要忽略大小写（海豚回来的是 rawScript，写死大小写会漏掉）
_SCRIPT_KEYS_LOWER = tuple(k.lower() for k in SCRIPT_KEYS)
_SCRIPT_LIST_KEYS_LOWER = tuple(k.lower() for k in SCRIPT_LIST_KEYS)
#: 递归扫描脚本键时允许的最大深度（taskParams 里嵌套很深的条件分支不进去）
MAX_SCAN_DEPTH = 6

#: 常见 SQL 客户端的外壳命令，需要从 SHELL 脚本里剥掉
SHELL_WRAPPER_WORDS = (
    "hive",
    "beeline",
    "spark-sql",
    "sparksql",
    "spark_sql",
    "doris",
    "mysql",
    "clickhouse-client",
    "psql",
    "trino",
    "presto",
    "impala-shell",
)

_QUOTED_E_RE = re.compile(r"(?:^|\s)-e\s+(?P<q>[\"'])(?P<body>.*)(?P=q)\s*$", re.S)
_QUOTED_WHOLE_RE = re.compile(r"^[\"'](?P<body>.*)[\"']$", re.S)
_SHELL_CMD_RE = re.compile(
    r"^\s*(?:%s)\b[^\n\"']*?(?:-e|-f)?\s*[\"']?(?P<body>.*?)[\"']?\s*$"
    % "|".join(re.escape(w) for w in SHELL_WRAPPER_WORDS),
    re.S | re.I,
)


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class DsError(Exception):
    """DolphinScheduler 集成相关错误基类。"""


class DsConnectionError(DsError):
    """连不上海豚（容器没起 / 地址写错 / 超时）。"""


class DsAuthError(DsError):
    """登录失败或 session 过期。"""


class DsApiError(DsError):
    """海豚返回了业务错误（``code != 0``）。"""

    def __init__(self, code: int, message: str, path: str = "") -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"海豚接口返回错误 code={code} msg={message}" + (f"（{path}）" if path else ""))


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _as_json_text(value: Any) -> str:
    """把 dict/list 序列化成 JSON 文本；字符串原样返回。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _maybe_json(value: Any) -> Any:
    """尽量把 JSON 文本解析成对象；不是 JSON 就原样返回。"""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def normalize_script(text: Any, task_type: str = "") -> str:
    """把抓到的脚本文本清洗成「能直接喂给 sqlglot」的形态。

    * 去掉 shebang / ``#`` 整行注释（SQL 注释用 ``--`` / ``/* */``，不受影响）
    * 剥掉 ``hive -e "..."`` / ``beeline -e '...'`` / ``mysql -e "..."`` 这类外壳命令
    * 整体被一对引号包住时去掉引号

    对纯 SQL 文本是幂等的（原文里没有这些形态就原样返回）。
    """
    if not isinstance(text, str):
        text = _as_json_text(text)
    t = text.strip()
    if not t:
        return ""
    if t.startswith("#!"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
    lines = [ln for ln in t.splitlines() if not ln.lstrip().startswith("#")]
    t = "\n".join(lines).strip()
    if not t:
        return ""

    m = _QUOTED_E_RE.search(t)
    if m:
        t = m.group("body").strip()
    else:
        m2 = _QUOTED_WHOLE_RE.match(t)
        if m2:
            t = m2.group("body").strip()
        else:
            m3 = _SHELL_CMD_RE.match(t)
            if m3:
                body = m3.group("body").strip()
                if body and re.search(r"\b(SELECT|INSERT|CREATE|UPDATE|DELETE|WITH|MERGE|ALTER)\b", body, re.I):
                    t = body

    # 剥掉可能残留的成对外引号（``hive -e "...."`` 被截断的情况）
    m4 = _QUOTED_WHOLE_RE.match(t)
    if m4 and re.search(r"\b(SELECT|INSERT|CREATE|UPDATE|DELETE|WITH)\b", m4.group("body"), re.I):
        t = m4.group("body").strip()
    return t


def task_params_of(task_definition: Dict[str, Any]) -> Dict[str, Any]:
    """取任务定义里的 ``taskParams``，兼容「JSON 文本」和「已解析对象」两种形态。"""
    params = task_definition.get("taskParams")
    if params is None:
        params = task_definition.get("taskParamMap")
    params = _maybe_json(params)
    if not isinstance(params, dict):
        return {}
    return params


def _collect_scripts(node: Any, path: str, out: List[Tuple[str, str]], depth: int = 0) -> None:
    """递归扫描 taskParams，收集脚本文本。"""
    if depth > MAX_SCAN_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            child_path = f"{path}.{key}" if path else str(key)
            if key_l in _SCRIPT_KEYS_LOWER and isinstance(value, str) and value.strip():
                out.append((child_path, value))
                continue
            if key_l in _SCRIPT_LIST_KEYS_LOWER and isinstance(value, (list, tuple)):
                for i, item in enumerate(value):
                    if isinstance(item, str) and item.strip():
                        out.append((f"{child_path}[{i}]", item))
                continue
            _collect_scripts(value, child_path, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            _collect_scripts(item, f"{path}[{i}]", out, depth + 1)


def extract_scripts(task_definition: Dict[str, Any]) -> List[Dict[str, str]]:
    """从任务定义中抽取可解析的脚本。

    返回 ``[{"key": "taskParams.sql", "text": "...", "raw_text": "..."}]``；
    没有脚本（如 DEPENDENT / SUB_PROCESS 任务）时返回空列表。

    * SQL 任务 -> ``taskParams.sql``（另有 ``preStatements`` / ``postStatements``）
    * SHELL / PYTHON 任务 -> ``taskParams.rawScript``（会自动剥掉 ``hive -e "..."`` 外壳）
    """
    params = task_params_of(task_definition)
    task_type = str(task_definition.get("taskType") or "SQL").upper()
    found: List[Tuple[str, str]] = []
    _collect_scripts(params, "", found)

    out: List[Dict[str, str]] = []
    seen: set = set()
    for key, raw in found:
        text = normalize_script(raw, task_type)
        if not text:
            continue
        dedup = (key.split("[")[0].lower(), text)
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append({"key": f"taskParams.{key}" if not key.startswith("taskParams") else key,
                    "text": text, "raw_text": raw.strip()})
    return out


def referenced_definition_codes(task_definition: Dict[str, Any]) -> List[Dict[str, str]]:
    """找出任务参数里引用的其它工作流定义 code。

    覆盖 ``SUB_PROCESS``（``processDefinitionCode``）与 ``DEPENDENT``
    （``dependence.dependTaskList[].dependItemList[].definitionCode``）两类，
    用于还原海豚原生的工作流级依赖。
    """
    params = task_params_of(task_definition)
    refs: List[Dict[str, str]] = []

    def add(code: Any, kind: str, key: str) -> None:
        if code in (None, "", 0, "0"):
            return
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            return
        refs.append({"definition_code": str(code_int), "kind": kind, "key": key})

    def walk(node: Any, path: str, depth: int = 0) -> None:
        if depth > MAX_SCAN_DEPTH:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if key == "processDefinitionCode":
                    add(value, "sub_process", child)
                elif key == "definitionCode":
                    add(value, "dependent", child)
                else:
                    walk(value, child, depth + 1)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", depth + 1)

    walk(params, "")
    return refs


def check_port(host: str = "localhost", port: int = 12345, timeout: float = 1.0) -> bool:
    """探测海豚端口是否可达（测试里用 skipif，CLI 里做友好报错）。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class DsClient:
    """DolphinScheduler OpenAPI 客户端（sessionId 会话，自动登录）。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        dialect: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        session_id: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("DS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.user = user or os.environ.get("DS_USER") or DEFAULT_USER
        self.password = password or os.environ.get("DS_PASSWORD") or DEFAULT_PASSWORD
        self.dialect = dialect or os.environ.get("DS_DIALECT") or DEFAULT_DIALECT
        self.timeout = float(timeout)
        self.session_id: Optional[str] = session_id
        #: 上一次请求的原始返回，便于排查
        self.last_response: Optional[Dict[str, Any]] = None

    # ---------------------------------------------------------------- #
    # 便捷构造 / 生命周期
    # ---------------------------------------------------------------- #
    @classmethod
    def from_env(cls, **overrides: Any) -> "DsClient":
        """用环境变量（DS_BASE_URL / DS_USER / DS_PASSWORD / DS_DIALECT）构造。"""
        return cls(**{k: v for k, v in overrides.items() if v is not None})

    def close(self) -> None:
        """登出（幂等；失败不抛异常，避免掩盖主流程错误）。"""
        if not self.session_id:
            return
        try:
            self.request("POST", "/logout", auth=True, raise_on_error=False)
        except DsError:
            pass
        finally:
            self.session_id = None

    def __enter__(self) -> "DsClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"<DsClient base_url={self.base_url!r} user={self.user!r} logged_in={bool(self.session_id)}>"

    # ---------------------------------------------------------------- #
    # 底层请求
    # ---------------------------------------------------------------- #
    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Any = None,
        form: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        raise_on_error: bool = True,
    ) -> Any:
        """发一个请求，返回信封里的 ``data``。

        * ``params`` -> URL query（海豚大量接口用 query 传参）
        * ``body``   -> JSON 请求体
        * ``form``   -> ``application/x-www-form-urlencoded`` 请求体（登录用）
        """
        url = self.base_url + path
        query = {k: v for k, v in (params or {}).items() if v is not None}
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)

        data: Optional[bytes] = None
        headers = {"Accept": "application/json"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.session_id:
            headers["sessionId"] = self.session_id

        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:                       # 4xx / 5xx 也带 JSON 信封
            raw = exc.read().decode("utf-8", "replace")
            payload = _maybe_json(raw)
            if isinstance(payload, dict) and "code" in payload:
                if raise_on_error:
                    raise DsApiError(int(payload.get("code") or -1), str(payload.get("msg") or raw),
                                     path) from None
                self.last_response = payload
                return payload.get("data")
            if raise_on_error:
                raise DsApiError(exc.code, f"HTTP {exc.code} {raw[:200]}", path) from None
            return None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise DsConnectionError(
                f"连不上 DolphinScheduler：{self.base_url}（{exc}）\n"
                f"提示：确认海豚容器在跑（docker ps | grep dolphinscheduler），"
                f"或改 --base-url / 环境变量 DS_BASE_URL"
            ) from None

        payload = _maybe_json(raw)
        self.last_response = payload if isinstance(payload, dict) else {"data": payload}
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None and int(code) != 0:
                if raise_on_error:
                    raise DsApiError(int(code), str(payload.get("msg") or ""), path)
                return payload.get("data")
            if "data" in payload:
                return payload["data"]
            return payload
        return payload

    # ---------------------------------------------------------------- #
    # 登录 / 登出
    # ---------------------------------------------------------------- #
    def login(self) -> str:
        """登录并缓存 ``sessionId``；失败抛 :class:`DsAuthError`。"""
        try:
            data = self.request(
                "POST", "/login", auth=False, raise_on_error=False,
                form={"userName": self.user, "userPassword": self.password},
            )
        except DsConnectionError:
            raise
        if not isinstance(data, dict) or not data.get("sessionId"):
            msg = ""
            if isinstance(self.last_response, dict):
                msg = str(self.last_response.get("msg") or "")
            raise DsAuthError(f"登录失败：用户 {self.user} @ {self.base_url}（{msg or '未拿到 sessionId'}）")
        self.session_id = str(data["sessionId"])
        return self.session_id

    def ensure_login(self) -> str:
        if not self.session_id:
            self.login()
        return self.session_id

    def logout(self) -> Any:  # pragma: no cover - 由 close() 覆盖
        return self.close()

    # ---------------------------------------------------------------- #
    # 读取：项目 / 工作流 / 任务
    # ---------------------------------------------------------------- #
    def _paged(self, path: str, params: Dict[str, Any], page_size: int, key: str = "totalList") -> List[Dict[str, Any]]:
        """翻页拉全量列表。"""
        self.ensure_login()
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            query = dict(params)
            query.update({"pageNo": page, "pageSize": page_size})
            data = self.request("GET", path, params=query)
            items = (data or {}).get(key) or []
            out.extend(items)
            total = int((data or {}).get("total") or 0)
            if len(items) < page_size or (total and len(out) >= total) or not items:
                break
            page += 1
        return out

    def list_projects(self, page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict[str, Any]]:
        """全部项目：``[{"code","name","defCount",...}]``。"""
        return self._paged("/projects", {}, page_size)

    def get_project(self, code: Any) -> Dict[str, Any]:
        self.ensure_login()
        return self.request("GET", f"/projects/{int(code)}") or {}

    def list_process_definitions(
        self, project_code: Any, page_size: int = DEFAULT_PAGE_SIZE, search_val: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """项目下的工作流定义列表（单次只取元信息，不含任务）。"""
        params = {"searchVal": search_val} if search_val else {}
        return self._paged(f"/projects/{int(project_code)}/process-definition", params, page_size)

    def get_process_definition(self, project_code: Any, code: Any) -> Dict[str, Any]:
        """工作流完整定义：``processDefinition`` + ``taskDefinitionList`` + ``processTaskRelationList``。

        这是核心读取接口——一次调用就能拿到工作流的全部任务与它们的上下游关系。
        """
        self.ensure_login()
        data = self.request("GET", f"/projects/{int(project_code)}/process-definition/{int(code)}")
        if not isinstance(data, dict):
            return {"processDefinition": {}, "taskDefinitionList": [], "processTaskRelationList": []}
        return {
            "processDefinition": data.get("processDefinition") or {},
            "taskDefinitionList": data.get("taskDefinitionList") or [],
            "processTaskRelationList": data.get("processTaskRelationList") or [],
        }

    def list_task_definitions(self, project_code: Any, process_definition_code: Optional[Any] = None,
                              page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict[str, Any]]:
        """任务定义列表。

        给了 ``process_definition_code`` 就只取该工作流的任务（走工作流详情接口，
        顺带带回 relation）；否则翻页拉项目下全部任务。
        """
        if process_definition_code is not None:
            return list(self.get_process_definition(project_code, process_definition_code)["taskDefinitionList"])
        self.ensure_login()
        return self._paged(f"/projects/{int(project_code)}/task-definition", {}, page_size)

    def list_process_task_relations(self, project_code: Any, process_definition_code: Any) -> List[Dict[str, Any]]:
        """工作流内部的任务依赖关系（``preTaskCode -> postTaskCode``）。"""
        return list(self.get_process_definition(project_code, process_definition_code)["processTaskRelationList"])

    # ---------------------------------------------------------------- #
    # 读取：把「项目 -> 工作流 -> 任务」一次拉全（sync 用）
    # ---------------------------------------------------------------- #
    def fetch_workflow(self, project_code: Any, code: Any) -> Dict[str, Any]:
        """拉一个工作流的完整定义（含任务与关系）。"""
        detail = self.get_process_definition(project_code, code)
        return {
            "processDefinition": detail["processDefinition"],
            "taskDefinitionList": detail["taskDefinitionList"],
            "processTaskRelationList": detail["processTaskRelationList"],
        }

    def fetch_all(
        self,
        project_names: Optional[Sequence[str]] = None,
        project_codes: Optional[Sequence[Any]] = None,
        dialect: Optional[str] = None,
        fetched_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """拉取（可筛选的）项目 -> 工作流 -> 任务 全量快照，供血缘构建使用。"""
        from datetime import datetime

        self.ensure_login()
        projects = self.list_projects()
        wanted_names = {n for n in (project_names or []) if n}
        wanted_codes = {str(c) for c in (project_codes or []) if c}
        if wanted_names or wanted_codes:
            projects = [
                p for p in projects
                if (p.get("name") in wanted_names) or (str(p.get("code")) in wanted_codes)
            ]
        bundles: List[Dict[str, Any]] = []
        for project in projects:
            pcode = project.get("code")
            workflows = [self.fetch_workflow(pcode, d.get("code"))
                         for d in self.list_process_definitions(pcode)]
            bundles.append({"project": project, "workflows": workflows})
        return {
            "fetched_at": fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "base_url": self.base_url,
            "user": self.user,
            "dialect": dialect or self.dialect,
            "projects": bundles,
        }

    # ---------------------------------------------------------------- #
    # [写入] 演示数据：建项目 / 建数据源 / 建工作流（只给演示脚本用）
    # ---------------------------------------------------------------- #
    def create_project(self, name: str, description: str = "") -> Dict[str, Any]:
        self.ensure_login()
        return self.request("POST", "/projects",
                            params={"projectName": name, "description": description}) or {}

    def delete_project(self, code: Any) -> Any:
        self.ensure_login()
        return self.request("DELETE", f"/projects/{int(code)}", raise_on_error=False)

    def list_datasources(self, page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict[str, Any]]:
        return self._paged("/datasources", {}, page_size)

    def create_datasource(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """建数据源（SQL 任务需要绑定一个数据源 id；演示用 HIVE 数据源不校验连通性）。"""
        self.ensure_login()
        return self.request("POST", "/datasources", body=payload) or {}

    def gen_task_codes(self, project_code: Any, num: int = 1) -> List[int]:
        """向海豚申请合法的 task code（比自己随机生成更稳）。"""
        self.ensure_login()
        data = self.request("GET", f"/projects/{int(project_code)}/task-definition/gen-task-codes",
                            params={"genNum": int(num)})
        return [int(c) for c in (data or [])]

    def create_process_definition(
        self,
        project_code: Any,
        name: str,
        task_definitions: Sequence[Dict[str, Any]],
        task_relations: Sequence[Dict[str, Any]],
        description: str = "",
        global_params: str = "[]",
        locations: str = "[]",
        timeout: int = 0,
        execution_type: str = "PARALLEL",
    ) -> Dict[str, Any]:
        """一次性创建工作流 + 其中的任务 + 任务间关系（海豚的进程定义创建接口）。

        .. note::
           海豚这个接口的入参是 ``@RequestParam``，官方前端把它们塞在 URL query 里；
           但数仓 SQL 含大量中文，URL 编码后单个工作流很容易超过容器 8KB 的请求头上限
           并返回 ``HTTP 414 URI Too Long``。这里改成 **form body**（同样的
           ``application/x-www-form-urlencoded``，Spring 一样能取到 ``@RequestParam``），
           实测可以正常建出含多任务 / 长 SQL 的工作流。
        """
        self.ensure_login()
        return self.request(
            "POST", f"/projects/{int(project_code)}/process-definition",
            form={
                "name": name,
                "description": description,
                "globalParams": global_params,
                "locations": locations,
                "timeout": timeout,
                "taskDefinitionJson": json.dumps(list(task_definitions), ensure_ascii=False),
                "taskRelationJson": json.dumps(list(task_relations), ensure_ascii=False),
                "executionType": execution_type,
            },
        ) or {}

    def save_single_task(
        self,
        project_code: Any,
        process_definition_code: Any,
        task_definition: Dict[str, Any],
        upstream_codes: Sequence[Any] = (),
    ) -> Dict[str, Any]:
        """往已有工作流里追加一个任务（可选声明上游任务 code）。

        同样走 form body；适合在建完工作流后补任务，或做增量演示。
        """
        self.ensure_login()
        return self.request(
            "POST", f"/projects/{int(project_code)}/task-definition/save-single",
            form={
                "processDefinitionCode": int(process_definition_code),
                "taskDefinitionJsonObj": json.dumps(task_definition, ensure_ascii=False),
                "upstreamCodes": ",".join(str(c) for c in upstream_codes if c),
            },
        ) or {}

    def delete_process_definition(self, project_code: Any, code: Any) -> Any:
        self.ensure_login()
        return self.request("DELETE", f"/projects/{int(project_code)}/process-definition/{int(code)}",
                            raise_on_error=False)


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_USER",
    "DEFAULT_PASSWORD",
    "DEFAULT_DIALECT",
    "DsClient",
    "DsError",
    "DsConnectionError",
    "DsAuthError",
    "DsApiError",
    "extract_scripts",
    "referenced_definition_codes",
    "normalize_script",
    "task_params_of",
    "check_port",
    "SCRIPT_KEYS",
    "SCRIPT_LIST_KEYS",
    "TASK_CODE_BASE",
]
