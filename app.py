import marshal
import threading
import re
from pathlib import Path

_backup = Path(__file__).with_name("app.cpython-314.pyc.bak")
_code = marshal.loads(_backup.read_bytes()[16:])
_run_name = globals().get("__name__")
globals()["__name__"] = "tracelog_core"
exec(_code, globals())
globals()["__name__"] = _run_name


_orig_save_field = save_field
_orig_search_needles = search_needles


def save_field(form):
    kind = (form.get("kind") or "").strip()
    if kind != "log_id":
        return _orig_save_field(form)
    name = (form.get("name") or "").strip()
    if not name:
        raise ValueError("请填写名称")
    pattern = (form.get("pattern") or "").strip()
    field_id = (form.get("id") or "").strip()
    with connect() as conn:
        with conn.cursor() as cur:
            if field_id.isdigit():
                cur.execute(
                    "UPDATE search_fields SET name=%s, kind=%s, pattern=%s WHERE id=%s",
                    (name[:40], kind, pattern, int(field_id)),
                )
                if cur.rowcount == 0:
                    raise ValueError("查询条件不存在")
            else:
                cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM search_fields")
                order = cur.fetchone()["n"]
                cur.execute(
                    "INSERT INTO search_fields (name, kind, pattern, enabled, sort_order, created_at) VALUES (%s,%s,%s,1,%s,%s)",
                    (name[:40], kind, pattern, order, datetime.now()),
                )


def search_needles(field, keyword):
    if (field.get("kind") or "") != "log_id":
        return _orig_search_needles(field, keyword)
    value = (keyword or "").strip()
    if not value:
        raise ValueError("请填写查询内容")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{4,80}", value):
        raise ValueError("日志 ID 格式不正确")
    return [
        value,
        "logid=%s" % value,
        "logId=%s" % value,
        "logid: %s" % value,
        "[logid: %s" % value,
        "[logId: %s" % value,
        '"logid":"%s"' % value,
        '"logId":"%s"' % value,
    ]



_limits = threading.local()
SEED_LIMIT = 300
REQUEST_LIMIT = 40
CHAIN_LIMIT = 800
TRACE_FETCH_LIMIT = 5000
UNLINKED_KEY = "未关联"
LOOSE_PREFIX = "unlinked:"


def _flag_box():
    box = getattr(_limits, "flags", None)
    if box is None:
        box = {"seedLimited": False, "requestLimited": False, "chainLimited": False, "focused": False, "seedCap": SEED_LIMIT}
        _limits.flags = box
    return box


def redact_message(text):
    text = AUTH_RE.sub("Authorization:[已隐藏]", text or "")
    text = JWT_RE.sub("[已隐藏凭证]", text)
    text = MOBILE_RE.sub("[已隐藏手机号]", text)
    return text.strip()[:4000]


def trace_limit_note(flags):
    parts = []
    cap = int(flags.get("seedCap") or SEED_LIMIT)
    if flags.get("focused") and flags.get("seedLimited"):
        parts.append("这次请求的日志超过 %s 条，只取了 %s 条" % (cap, cap))
    elif flags.get("requestLimited"):
        parts.append("只展开了最近的 40 次请求")
    elif flags.get("seedLimited"):
        parts.append("只取了最近的 %s 条日志" % cap)
    if flags.get("chainLimited") and not flags.get("focused"):
        parts.append("关联日志只取了 800 条")
    if not parts:
        return ""
    return "。".join(parts) + "。"


REQUEST_ID_RE = re.compile(
    r'(?i)(?:\[logid:\s*|"traceid"\s*:\s*"|"trace_id"\s*:\s*"|traceid\s*[:=]\s*|logid\s*[:=]\s*)([A-Za-z0-9_.:-]{4,80})'
)
STATUS_EXTRA_RE = re.compile(r'(?i)(?:状态码[:：]\s*|"status"\s*:\s*|status[=:]\s*)(\d{3})')


def usable_request_id(value):
    value = (value or "").strip().strip('"')
    if value.lower() in ("", "unknown", "unknown-logid", "-", "null", "none"):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{4,80}", value):
        return ""
    return value


def canonical_request_key(msg):
    found = []
    for match in REQUEST_ID_RE.finditer(msg or ""):
        value = usable_request_id(match.group(1))
        if value and value not in found:
            found.append(value)
    return found[0] if found else ""


def path_only(uri):
    return str(uri or "").split("?")[0].split("#")[0]


def short_summary(event):
    uri = path_only(event.get("uri"))
    if not uri:
        return event.get("summary") or ""
    method = event.get("http_method") or ""
    text = (method + " " + uri).strip()
    if event.get("status_code"):
        text += "  " + str(event["status_code"])
    if event.get("duration"):
        text += "  " + str(event["duration"])
    return text[:500]


_orig_init_db = init_db
_orig_parse_event = parse_event
_orig_do_post = Handler.do_POST


def init_db():
    _orig_init_db()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM events LIKE 'message'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE events ADD COLUMN message TEXT NULL")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_stats (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  name VARCHAR(80) NOT NULL,
                  project_id BIGINT NOT NULL,
                  time_window VARCHAR(16) NOT NULL,
                  time_from VARCHAR(40) NOT NULL DEFAULT '',
                  time_to VARCHAR(40) NOT NULL DEFAULT '',
                  container_name VARCHAR(128) NOT NULL DEFAULT '',
                  realtime TINYINT NOT NULL DEFAULT 0,
                  updated_at DATETIME NOT NULL
                )
                """
            )
            for column in ("can_use_datasource", "can_use_report"):
                cur.execute("SHOW COLUMNS FROM accounts LIKE %s", (column,))
                if not cur.fetchone():
                    cur.execute("ALTER TABLE accounts ADD COLUMN " + column + " TINYINT NOT NULL DEFAULT 0")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS data_sources (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  name VARCHAR(80) NOT NULL,
                  host VARCHAR(191) NOT NULL,
                  port INT NOT NULL DEFAULT 3306,
                  username VARCHAR(80) NOT NULL,
                  password VARCHAR(255) NOT NULL,
                  database_name VARCHAR(80) NOT NULL,
                  created_at DATETIME NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_reports (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  name VARCHAR(80) NOT NULL,
                  source_id BIGINT NOT NULL,
                  table_name VARCHAR(80) NOT NULL DEFAULT '',
                  chart_kind VARCHAR(16) NOT NULL DEFAULT 'bar',
                  axis_x VARCHAR(80) NOT NULL DEFAULT '',
                  axis_y VARCHAR(80) NOT NULL DEFAULT '',
                  sql_text MEDIUMTEXT NOT NULL,
                  created_by VARCHAR(80) NOT NULL,
                  updated_at DATETIME NOT NULL,
                  KEY idx_saved_reports_source (source_id)
                )
                """
            )


            cur.execute("SELECT id FROM search_fields WHERE kind=%s", ("log_id",))
            if not cur.fetchone():
                cur.execute("SELECT id FROM search_fields WHERE name=%s", ("日志 ID",))
                named = cur.fetchone()
                if named:
                    cur.execute("UPDATE search_fields SET kind=%s WHERE id=%s", ("log_id", named["id"]))
                else:
                    cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM search_fields")
                    order = cur.fetchone()["n"]
                    cur.execute(
                        "INSERT INTO search_fields (name, kind, pattern, enabled, sort_order, created_at) VALUES (%s,%s,%s,1,%s,%s)",
                        ("日志 ID", "log_id", "", order, datetime.now()),
                    )
            ensure_anonymous_account(cur)
            ensure_audit_logs(cur)


def ensure_audit_logs(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          username VARCHAR(64) NOT NULL,
          action VARCHAR(80) NOT NULL,
          method VARCHAR(8) NOT NULL,
          path VARCHAR(191) NOT NULL,
          detail VARCHAR(1000) NOT NULL DEFAULT '',
          success TINYINT NOT NULL DEFAULT 1,
          ip VARCHAR(64) NOT NULL DEFAULT '',
          created_at DATETIME NOT NULL,
          KEY idx_audit_time (created_at),
          KEY idx_audit_user (username)
        )
        """
    )


def ensure_anonymous_account(cur):
    cur.execute("SELECT id FROM accounts WHERE username=%s", ("anonymous",))
    if cur.fetchone():
        return
    cur.execute(
        """
        INSERT INTO accounts (username, password_hash, created_at, is_admin, can_create_project, can_use_datasource, can_use_report)
        VALUES (%s,%s,%s,0,0,0,0)
        """,
        ("anonymous", hash_password(secrets.token_hex(24)), datetime.now()),
    )


def handle_anonymous_login(self):
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_account_flags(cur)
            ensure_anonymous_account(cur)
    token = start_session("anonymous")
    self._json(200, {"ok": True, "username": "anonymous"}, [("Set-Cookie", "tracelog=%s; HttpOnly; Path=/; SameSite=Lax; Max-Age=604800" % token)])


def parse_event(row, user_id):
    event = _orig_parse_event(row, user_id)
    msg = row.get("_msg") or ""
    event["message"] = redact_message(msg)
    forced = ""
    for value in getattr(_limits, "trace_ids", None) or []:
        if value and value.lower() in msg.lower():
            forced = value
            break
    key = forced or canonical_request_key(msg)
    if key:
        event["request_key"] = key[:128]
    if not event.get("status_code"):
        status = STATUS_EXTRA_RE.search(msg)
        if status:
            event["status_code"] = status.group(1)
    event["uri"] = path_only(event.get("uri"))
    event["summary"] = short_summary(event)
    return event


def handle_new_api_stats(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        project_id = (form.get("projectId") or "").strip()
        if not project_id.isdigit():
            raise ValueError("请先选择项目")
        if not can_use_project(user, project_id):
            raise ValueError("没有这个项目的权限")
        window = (form.get("window") or "").strip() or "15m"
        container = (form.get("container") or "").strip()
        namespace = (form.get("namespace") or "").strip()
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.base_url
                    FROM projects p
                    JOIN project_sources ps ON ps.project_id=p.id
                    JOIN log_sources s ON s.id=ps.source_id
                    WHERE p.id=%s
                    LIMIT 1
                    """,
                    (int(project_id),),
                )
                if not cur.fetchone():
                    raise ValueError("项目没有日志地址")
        import summary as summary_mod
        self._json(200, summary_mod.read_new_stats(project_id, namespace, container, window, form.get("timeFrom") or "", form.get("timeTo") or ""))
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)})


def list_hosts(sources):
    found = {}
    errors = []
    query = "_time:24h | stats by (host, container_name) count() as hits | sort by (host asc) | limit 500"
    for source in sources:
        try:
            rows = fetch_rows(source, query, 500, 20)
        except Exception as exc:
            errors.append(str(exc))
            continue
        source_name = str(source.get("name") or "")
        for row in rows:
            host = str(row.get("host") or "").strip()
            if not host:
                continue
            slot = found.setdefault(host, {"logs": set(), "sources": set()})
            log_name = str(row.get("container_name") or "").strip()
            if log_name:
                slot["logs"].add(log_name)
            if source_name:
                slot["sources"].add(source_name)
    if not found and errors:
        raise ValueError(errors[0])
    return [{
        "name": name,
        "logs": sorted(slot["logs"]),
        "source": "、".join(sorted(slot["sources"])),
    } for name, slot in sorted(found.items())]


def handle_hosts(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        source_ids = parse_source_ids(form.get("sourceIds") or "")
        if not source_ids:
            self._json(200, {"hosts": []})
            return
        marks = ",".join(["%s"] * len(source_ids))
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, base_url, account_id, project_id FROM log_sources WHERE id IN (" + marks + ")",
                    tuple(int(item) for item in source_ids),
                )
                sources = cur.fetchall()
        self._json(200, {"hosts": list_hosts(sources)})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)})



def ensure_saved_stats(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_stats (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          name VARCHAR(80) NOT NULL,
          project_id BIGINT NOT NULL,
          time_window VARCHAR(16) NOT NULL,
          time_from VARCHAR(40) NOT NULL DEFAULT '',
          time_to VARCHAR(40) NOT NULL DEFAULT '',
          container_name VARCHAR(128) NOT NULL DEFAULT '',
          realtime TINYINT NOT NULL DEFAULT 0,
          updated_at DATETIME NOT NULL
        )
        """
    )


def list_saved_stats(username):
    admin = is_admin(username)
    sql = """
        SELECT s.id, s.name, s.project_id, p.name AS project_name, s.time_window, s.time_from, s.time_to, s.container_name, s.realtime
        FROM saved_stats s JOIN projects p ON p.id=s.project_id
    """
    args = ()
    if not admin:
        sql += " WHERE s.project_id IN (SELECT project_id FROM user_projects WHERE username=%s)"
        args = (username,)
    sql += " ORDER BY s.id DESC"
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_saved_stats(cur)
            cur.execute(sql, args)
            rows = cur.fetchall()
    return [{
        "id": row["id"],
        "name": row["name"],
        "project_id": row["project_id"],
        "project_name": row.get("project_name") or "",
        "time_window": row["time_window"],
        "time_from": row.get("time_from") or "",
        "time_to": row.get("time_to") or "",
        "container_name": row.get("container_name") or "",
        "realtime": int(row.get("realtime") or 0),
    } for row in rows]


def save_saved_stat(username, form):
    name = (form.get("name") or "").strip()
    project_id = (form.get("projectId") or "").strip()
    window = (form.get("window") or "15m").strip()
    time_from = (form.get("timeFrom") or "").strip()
    time_to = (form.get("timeTo") or "").strip()
    container = (form.get("container") or "").strip()[:128]
    realtime = 1 if (form.get("realtime") or "") in ("1", "on", "true") else 0
    stat_id = (form.get("id") or "").strip()
    if not name:
        raise ValueError("请填写名称")
    if len(name) > 80:
        raise ValueError("名称不能超过 80 个字")
    if not project_id.isdigit():
        raise ValueError("请先选择项目")
    if not can_use_project(username, project_id):
        raise ValueError("没有这个项目的权限")
    if window not in ("15m", "1h", "6h", "custom"):
        raise ValueError("时间范围不正确")
    if window == "custom":
        time_filter("custom", time_from, time_to, 0)
    else:
        time_from = ""
        time_to = ""
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_saved_stats(cur)
            if stat_id:
                if not stat_id.isdigit():
                    raise ValueError("统计不存在")
                cur.execute("SELECT project_id FROM saved_stats WHERE id=%s", (int(stat_id),))
                current = cur.fetchone()
                if not current or not can_use_project(username, current["project_id"]):
                    raise ValueError("统计不存在")
                cur.execute(
                    """
                    UPDATE saved_stats
                    SET name=%s, project_id=%s, time_window=%s, time_from=%s, time_to=%s, container_name=%s, realtime=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (name, int(project_id), window, time_from, time_to, container, realtime, datetime.now(), int(stat_id)),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO saved_stats (name, project_id, time_window, time_from, time_to, container_name, realtime, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (name, int(project_id), window, time_from, time_to, container, realtime, datetime.now()),
                )


def delete_saved_stat(username, stat_id):
    stat_id = (stat_id or "").strip()
    if not stat_id.isdigit():
        raise ValueError("统计不存在")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_saved_stats(cur)
            cur.execute("SELECT project_id FROM saved_stats WHERE id=%s", (int(stat_id),))
            current = cur.fetchone()
            if not current or not can_use_project(username, current["project_id"]):
                raise ValueError("统计不存在")
            cur.execute("DELETE FROM saved_stats WHERE id=%s", (int(stat_id),))


def handle_saved_stats(self, action):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        if action == "list":
            self._json(200, {"stats": list_saved_stats(user)})
        elif action == "delete":
            delete_saved_stat(user, form.get("id") or "")
            self._json(200, {"ok": True})
        else:
            save_saved_stat(user, form)
            self._json(200, {"ok": True})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)})




def ensure_account_flags(cur):
    for column in ("can_use_datasource", "can_use_report"):
        cur.execute("SHOW COLUMNS FROM accounts LIKE %s", (column,))
        if not cur.fetchone():
            cur.execute("ALTER TABLE accounts ADD COLUMN " + column + " TINYINT NOT NULL DEFAULT 0")


def account_flags(username):
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_account_flags(cur)
            cur.execute(
                "SELECT is_admin, can_create_project, can_use_datasource, can_use_report FROM accounts WHERE username=%s",
                (username,),
            )
            return cur.fetchone() or {}


def module_allowed(username, column):
    row = account_flags(username)
    return bool(row.get("is_admin") or row.get(column))


def flag_on(form, name):
    return 1 if (form.get(name) or "") in ("1", "on", "true") else 0


def save_user_access(cur, username, form):
    ids = parse_source_ids(form.get("projectIds") or "")
    cur.execute("DELETE FROM user_projects WHERE username=%s", (username,))
    for project_id in ids:
        cur.execute("INSERT INTO user_projects (username, project_id) VALUES (%s,%s)", (username, int(project_id)))
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_data_sources (
          username VARCHAR(64) NOT NULL,
          source_id BIGINT NOT NULL,
          PRIMARY KEY (username, source_id)
        )
        """
    )
    granted = parse_source_ids(form.get("dataSourceIds") or "")
    cur.execute("DELETE FROM user_data_sources WHERE username=%s", (username,))
    for source_id in granted:
        cur.execute("INSERT INTO user_data_sources (username, source_id) VALUES (%s,%s)", (username, int(source_id)))
    cur.execute(
        """
        UPDATE accounts
        SET can_create_project=%s, can_use_datasource=%s, can_use_report=%s
        WHERE username=%s
        """,
        (flag_on(form, "canCreateProject"), flag_on(form, "canDataSource"), flag_on(form, "canReport"), username),
    )


def handle_me(self):
    name = current_username(self)
    if not name:
        self._json(401, {"error": "请先登录"})
        return
    row = account_flags(name)
    admin = bool(row.get("is_admin"))
    self._json(200, {
        "username": name,
        "isAdmin": admin,
        "canCreateProject": admin or bool(row.get("can_create_project")),
        "canDataSource": admin or bool(row.get("can_use_datasource")),
        "canReport": admin or bool(row.get("can_use_report")),
    })


def handle_users_get(self):
    name = self._user()
    if not name:
        return
    try:
        require_admin(name)
        with connect() as conn:
            with conn.cursor() as cur:
                ensure_account_flags(cur)
                cur.execute(
                    """
                    SELECT a.id, a.username, a.created_at, a.is_admin, a.can_create_project, a.can_use_datasource, a.can_use_report,
                           (SELECT GROUP_CONCAT(project_id) FROM user_projects WHERE username=a.username) AS project_ids,
                           (SELECT GROUP_CONCAT(source_id) FROM user_data_sources WHERE username=a.username) AS data_source_ids
                    FROM accounts a ORDER BY a.id
                    """
                )
                users = []
                for row in cur.fetchall():
                    item = dict(row)
                    if item.get("created_at"):
                        item["created_at"] = item["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                    users.append(item)
        self._json(200, {"users": users})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})


def handle_user_permissions(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        require_admin(user)
        username = (form.get("username") or "").strip()
        if not valid_username(username):
            raise ValueError("用户名不正确")
        with connect() as conn:
            with conn.cursor() as cur:
                ensure_account_flags(cur)
                cur.execute("SELECT is_admin FROM accounts WHERE username=%s", (username,))
                if not cur.fetchone():
                    raise ValueError("用户不存在")
                save_user_access(cur, username, form)
                password = form.get("password") or ""
                if password:
                    if len(password) < 6:
                        raise ValueError("密码至少 6 位")
                    cur.execute("UPDATE accounts SET password_hash=%s WHERE username=%s", (hash_password(password), username))
        self._json(200, {"ok": True})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})


def handle_user_create(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        require_admin(user)
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        if not valid_username(username):
            raise ValueError("用户名需以字母开头，只能包含字母、数字和下划线")
        if len(password) < 6:
            raise ValueError("密码至少 6 位")
        with connect() as conn:
            with conn.cursor() as cur:
                ensure_account_flags(cur)
                cur.execute(
                    """
                    INSERT INTO accounts (username, password_hash, created_at, is_admin, can_create_project, can_use_datasource, can_use_report)
                    VALUES (%s,%s,%s,0,%s,%s,%s)
                    """,
                    (username, hash_password(password), datetime.now(), flag_on(form, "canCreateProject"), flag_on(form, "canDataSource"), flag_on(form, "canReport")),
                )
                save_user_access(cur, username, form)
        self._json(200, {"ok": True})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        if "Duplicate" in str(exc) or "1062" in str(exc):
            self._json(400, {"error": "用户名已存在"})
        else:
            self._json(400, {"error": str(exc)[:180]})


def ensure_data_sources(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS data_sources (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          name VARCHAR(80) NOT NULL,
          host VARCHAR(191) NOT NULL,
          port INT NOT NULL DEFAULT 3306,
          username VARCHAR(80) NOT NULL,
          password VARCHAR(255) NOT NULL,
          database_name VARCHAR(80) NOT NULL,
          created_at DATETIME NOT NULL
        )
        """
    )


def data_source_row(cur, source_id):
    cur.execute(
        "SELECT id, name, host, port, username, password, database_name FROM data_sources WHERE id=%s",
        (int(source_id),),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("数据源不存在")
    return row


def connect_data_source(row, read_timeout=20):
    import pymysql
    try:
        return pymysql.connect(
            host=row["host"],
            port=int(row["port"] or 3306),
            user=row["username"],
            password=row["password"],
            database=row["database_name"],
            connect_timeout=5,
            read_timeout=read_timeout,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        message = str(exc)
        secret = row.get("password") or ""
        if secret:
            message = message.replace(secret, "******")
        raise ValueError("连接失败：" + message[:180])


def public_source(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "port": int(row["port"] or 3306),
        "username": row["username"],
        "database_name": row["database_name"],
    }


def list_data_sources(username=None):
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            sql = "SELECT id, name, host, port, username, database_name FROM data_sources"
            args = ()
            if username:
                sql += " WHERE id IN (SELECT source_id FROM user_data_sources WHERE username=%s)"
                args = (username,)
            sql += " ORDER BY id DESC"
            cur.execute(sql, args)
            return [public_source(row) for row in cur.fetchall()]


def can_query_source(username, source_id):
    row = account_flags(username)
    if row.get("is_admin"):
        return True
    if not row.get("can_use_report"):
        return False
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM user_data_sources WHERE username=%s AND source_id=%s",
                (username, int(source_id)),
            )
            return bool(cur.fetchone())


def save_data_source(form):
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    database_name = (form.get("database") or "").strip()
    source_id = (form.get("id") or "").strip()
    try:
        port = int((form.get("port") or "3306").strip())
    except ValueError:
        raise ValueError("端口不正确")
    if not name or not host or not username or not database_name:
        raise ValueError("请填写名称、主机、用户名和数据库")
    if not 1 <= port <= 65535:
        raise ValueError("端口不正确")
    if len(name) > 80:
        raise ValueError("名称不能超过 80 个字")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            if source_id:
                if not source_id.isdigit():
                    raise ValueError("数据源不存在")
                current = data_source_row(cur, source_id)
                if not password:
                    password = current["password"]
                cur.execute(
                    """
                    UPDATE data_sources
                    SET name=%s, host=%s, port=%s, username=%s, password=%s, database_name=%s
                    WHERE id=%s
                    """,
                    (name, host, port, username, password, database_name, int(source_id)),
                )
            else:
                if not password:
                    raise ValueError("请填写密码")
                cur.execute(
                    """
                    INSERT INTO data_sources (name, host, port, username, password, database_name, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (name, host, port, username, password, database_name, datetime.now()),
                )


def delete_data_source(source_id):
    if not str(source_id).isdigit():
        raise ValueError("数据源不存在")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            cur.execute("DELETE FROM data_sources WHERE id=%s", (int(source_id),))


def test_data_source(form):
    source_id = (form.get("id") or "").strip()
    if source_id.isdigit() and not (form.get("password") or ""):
        with connect() as conn:
            with conn.cursor() as cur:
                ensure_data_sources(cur)
                row = data_source_row(cur, source_id)
    else:
        try:
            port = int((form.get("port") or "3306").strip())
        except ValueError:
            raise ValueError("端口不正确")
        row = {
            "host": (form.get("host") or "").strip(),
            "port": port,
            "username": (form.get("username") or "").strip(),
            "password": form.get("password") or "",
            "database_name": (form.get("database") or "").strip(),
        }
    link = connect_data_source(row)
    try:
        with link.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
    finally:
        link.close()


def read_only_sql(sql):
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise ValueError("请填写查询语句")
    if ";" in text:
        raise ValueError("一次只能执行一条查询")
    lowered = text.lstrip("(").lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("只允许查询语句")
    blocked = (" into ", " outfile", " dumpfile", " for update", " lock in ", " sleep(")
    padded = " " + lowered + " "
    if any(word in padded for word in blocked):
        raise ValueError("这条语句不能执行")
    return text


def plain_cell(value):
    from datetime import date, datetime as date_time
    from decimal import Decimal
    if isinstance(value, date_time):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def report_tables(source_id):
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            row = data_source_row(cur, source_id)
    link = connect_data_source(row)
    try:
        with link.cursor() as cur:
            cur.execute(
                "SELECT table_name AS name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name",
                (row["database_name"],),
            )
            names = [item["name"] for item in cur.fetchall()]
    finally:
        link.close()
    return names



def report_columns(source_id, table):
    if not re.fullmatch(r"[A-Za-z0-9_]+", table or ""):
        raise ValueError("请选择数据表")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            row = data_source_row(cur, source_id)
    link = connect_data_source(row)
    try:
        with link.cursor() as cur:
            cur.execute("SELECT * FROM `%s` LIMIT 0" % table)
            return [item[0] for item in cur.description or []]
    finally:
        link.close()


def report_query(source_id, sql):
    statement = read_only_sql(sql)
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            row = data_source_row(cur, source_id)
    link = connect_data_source(row)
    try:
        with link.cursor() as cur:
            cur.execute(statement)
            found = cur.fetchall()
    finally:
        link.close()
    columns = list(found[0].keys()) if found else []
    rows = [{key: plain_cell(value) for key, value in item.items()} for item in found]
    return {"columns": columns, "rows": rows, "truncated": False}


def gpu_bounds(window):
    now = datetime.now().replace(microsecond=0)
    if window == "1h":
        return now - timedelta(hours=1), now
    if window == "6h":
        return now - timedelta(hours=6), now
    if window == "today":
        return now.replace(hour=0, minute=0, second=0), now
    if window == "7d":
        return now - timedelta(days=7), now
    if window == "30d":
        return now - timedelta(days=30), now
    return now - timedelta(hours=24), now


def gpu_months(start, end):
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append("%04d%02d" % (year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def gpu_existing_tables(link, database, prefix, months):
    wanted = ["%s_%s" % (prefix, item) for item in months]
    safe = [name for name in wanted if re.fullmatch(r"vsr_task_(timing|shot)_\d{6}", name)]
    if not safe:
        return []
    with link.cursor() as cur:
        marks = ",".join(["%s"] * len(safe))
        cur.execute(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema=%s AND table_name IN (" + marks + ")",
            tuple([database] + safe),
        )
        found = {item["name"] for item in cur.fetchall()}
    return [name for name in safe if name in found]


def gpu_add(bucket, item, fields):
    for name in fields:
        bucket[name] = bucket.get(name, 0) + (plain_cell(item.get(name)) or 0)


def gpu_avg(total, count):
    total = plain_cell(total) or 0
    count = plain_cell(count) or 0
    if not count:
        return None
    return round(float(total) / float(count))


def gpu_report(source_id, window):
    start, end = gpu_bounds(window)
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            row = data_source_row(cur, source_id)
    link = connect_data_source(row, 40)
    try:
        months = gpu_months(start, end)
        timing_tables = gpu_existing_tables(link, row["database_name"], "vsr_task_timing", months)
        shot_tables = gpu_existing_tables(link, row["database_name"], "vsr_task_shot", months)
        if not timing_tables and not shot_tables:
            raise ValueError("这个数据源没有视频增强任务表")
        machines = {}
        gpus = {}
        with link.cursor() as cur:
            for name in timing_tables:
                cur.execute(
                    """
                    SELECT private_server_ip AS ip, COUNT(*) AS tasks, SUM(status='failed') AS failed,
                           SUM(shots_infer_wall_ms) AS infer_sum, SUM(shots_infer_wall_ms IS NOT NULL) AS infer_n,
                           SUM(TIMESTAMPDIFF(SECOND, video_decode_start, video_decode_end)) AS decode_sum,
                           SUM(video_decode_start IS NOT NULL AND video_decode_end IS NOT NULL) AS decode_n,
                           SUM(TIMESTAMPDIFF(SECOND, video_encode_start, video_encode_end)) AS encode_sum,
                           SUM(video_encode_start IS NOT NULL AND video_encode_end IS NOT NULL) AS encode_n,
                           SUM(total_frames) AS frames
                    FROM `%s`
                    WHERE created_at >= %%s AND created_at < %%s AND private_server_ip <> ''
                    GROUP BY private_server_ip
                    """ % name,
                    (start, end),
                )
                for item in cur.fetchall():
                    ip = str(item.get("ip") or "").strip()
                    if not ip:
                        continue
                    bucket = machines.setdefault(ip, {})
                    gpu_add(bucket, item, ("tasks", "failed", "infer_sum", "infer_n", "decode_sum", "decode_n", "encode_sum", "encode_n", "frames"))
            for name in shot_tables:
                cur.execute(
                    """
                    SELECT private_server_ip AS ip, gpu_id, COUNT(*) AS shots, SUM(status='failed') AS failed,
                           SUM(elapsed_ms) AS elapsed_sum, SUM(elapsed_ms IS NOT NULL) AS elapsed_n
                    FROM `%s`
                    WHERE created_at >= %%s AND created_at < %%s AND private_server_ip <> '' AND gpu_id IS NOT NULL
                    GROUP BY private_server_ip, gpu_id
                    """ % name,
                    (start, end),
                )
                for item in cur.fetchall():
                    ip = str(item.get("ip") or "").strip()
                    try:
                        gpu_id = int(item.get("gpu_id"))
                    except (TypeError, ValueError):
                        continue
                    if not ip or gpu_id < 0 or gpu_id > 15:
                        continue
                    bucket = gpus.setdefault((ip, gpu_id), {})
                    gpu_add(bucket, item, ("shots", "failed", "elapsed_sum", "elapsed_n"))
    finally:
        link.close()
    machine_rows = []
    for ip, item in machines.items():
        machine_rows.append({
            "ip": ip,
            "tasks": int(item.get("tasks") or 0),
            "failed": int(item.get("failed") or 0),
            "infer_ms": gpu_avg(item.get("infer_sum"), item.get("infer_n")),
            "decode_ms": None if gpu_avg(item.get("decode_sum"), item.get("decode_n")) is None else gpu_avg(item.get("decode_sum"), item.get("decode_n")) * 1000,
            "encode_ms": None if gpu_avg(item.get("encode_sum"), item.get("encode_n")) is None else gpu_avg(item.get("encode_sum"), item.get("encode_n")) * 1000,
            "frames": int(item.get("frames") or 0),
        })
    machine_rows.sort(key=lambda item: (-(item["infer_ms"] or 0), item["ip"]))
    gpu_rows = []
    for (ip, gpu_id), item in gpus.items():
        gpu_rows.append({
            "ip": ip,
            "gpu_id": gpu_id,
            "shots": int(item.get("shots") or 0),
            "failed": int(item.get("failed") or 0),
            "avg_ms": gpu_avg(item.get("elapsed_sum"), item.get("elapsed_n")),
        })
    gpu_rows.sort(key=lambda item: (item["ip"], item["gpu_id"]))
    return {
        "machines": machine_rows,
        "gpus": gpu_rows,
        "from": start.strftime("%Y-%m-%d %H:%M"),
        "to": end.strftime("%Y-%m-%d %H:%M"),
    }



def gpu_step(window):
    if window == "1h":
        return 600, "10m"
    if window in ("7d", "30d"):
        return 86400, "day"
    return 3600, "hour"


def gpu_median(values):
    rows = sorted(value for value in values if value)
    if not rows:
        return None
    return rows[len(rows) // 2]


def gpu_label(moment, kind):
    if kind == "day":
        return moment.strftime("%m-%d")
    if kind == "10m":
        return moment.strftime("%H:%M")
    return moment.strftime("%m-%d %H:%M")


def gpu_trend(source_id, window, ip):
    if not re.fullmatch(r"[0-9A-Za-z.:_-]{1,64}", ip or ""):
        raise ValueError("请选择机器")
    start, end = gpu_bounds(window)
    step, kind = gpu_step(window)
    query_start = start if kind == "day" else start - timedelta(days=1)
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            row = data_source_row(cur, source_id)
    link = connect_data_source(row, 40)
    try:
        timing_tables = gpu_existing_tables(link, row["database_name"], "vsr_task_timing", gpu_months(query_start, end))
        shot_tables = gpu_existing_tables(link, row["database_name"], "vsr_task_shot", gpu_months(start, end))
        if not timing_tables:
            raise ValueError("这个数据源没有视频增强任务表")
        timing = {}
        shots = {}
        bucket_expr = "DATE(created_at)" if kind == "day" else "FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(created_at) / %s) * %s)"
        bucket_args = () if kind == "day" else (step, step)
        with link.cursor() as cur:
            for name in timing_tables:
                cur.execute(
                    """
                    SELECT private_server_ip AS ip,
                           {bucket} AS bucket,
                           COUNT(*) AS tasks, SUM(status='failed') AS failed,
                           SUM(shots_infer_wall_ms) AS infer_sum, SUM(shots_infer_wall_ms IS NOT NULL) AS infer_n,
                           SUM(total_frames) AS frames
                    FROM `{table}`
                    WHERE created_at >= %s AND created_at < %s AND private_server_ip <> ''
                    GROUP BY ip, bucket
                    """.format(bucket=bucket_expr, table=name),
                    bucket_args + (query_start, end),
                )
                for item in cur.fetchall():
                    host = str(item.get("ip") or "").strip()
                    bucket = item.get("bucket")
                    if not host or not bucket:
                        continue
                    slot = timing.setdefault((host, bucket), {})
                    gpu_add(slot, item, ("tasks", "failed", "infer_sum", "infer_n", "frames"))
            for name in shot_tables:
                cur.execute(
                    """
                    SELECT {bucket} AS bucket,
                           gpu_id, COUNT(*) AS shots, SUM(status='failed') AS failed,
                           SUM(elapsed_ms) AS elapsed_sum, SUM(elapsed_ms IS NOT NULL) AS elapsed_n
                    FROM `{table}`
                    WHERE created_at >= %s AND created_at < %s AND private_server_ip = %s AND gpu_id IS NOT NULL
                    GROUP BY bucket, gpu_id
                    """.format(bucket=bucket_expr, table=name),
                    bucket_args + (start, end, ip),
                )
                for item in cur.fetchall():
                    bucket = item.get("bucket")
                    try:
                        gpu_id = int(item.get("gpu_id"))
                    except (TypeError, ValueError):
                        continue
                    if not bucket or gpu_id < 0 or gpu_id > 15:
                        continue
                    slot = shots.setdefault((gpu_id, bucket), {})
                    gpu_add(slot, item, ("shots", "failed", "elapsed_sum", "elapsed_n"))
    finally:
        link.close()
    visible_from = start.date() if kind == "day" else start
    buckets = sorted(moment for host, moment in timing if host == ip and moment >= visible_from)
    fleet = {}
    for (host, moment), item in timing.items():
        if int(item.get("tasks") or 0) < 5:
            continue
        value = gpu_avg(item.get("infer_sum"), item.get("infer_n"))
        if value:
            fleet.setdefault(moment, []).append(value)
    points = []
    for moment in buckets:
        item = timing.get((ip, moment), {})
        prev = timing.get((ip, moment - timedelta(days=1)), {})
        infer_ms = gpu_avg(item.get("infer_sum"), item.get("infer_n"))
        fleet_ms = gpu_median(fleet.get(moment, []))
        tasks = int(item.get("tasks") or 0)
        points.append({
            "time": gpu_label(moment, kind),
            "tasks": tasks,
            "failed": int(item.get("failed") or 0),
            "infer_ms": infer_ms,
            "frames": int(item.get("frames") or 0),
            "fleet_infer_ms": None if fleet_ms is None else int(fleet_ms),
            "prev_tasks": int(prev.get("tasks") or 0),
            "prev_infer_ms": gpu_avg(prev.get("infer_sum"), prev.get("infer_n")),
            "prev_frames": int(prev.get("frames") or 0),
        })
    task_mid = gpu_median([item["tasks"] for item in points if item["tasks"] >= 5]) or 0
    for item in points:
        quiet = bool(task_mid and item["tasks"] < task_mid * 0.5)
        comparable = item["prev_tasks"] >= 5 and item["tasks"] >= item["prev_tasks"] * 0.5
        item["quiet"] = quiet
        item["slow"] = bool(not quiet and comparable and item["infer_ms"] and item["prev_infer_ms"] and item["infer_ms"] > item["prev_infer_ms"] * 1.15)
    gpu_rows = []
    for (gpu_id, moment), item in shots.items():
        gpu_rows.append({
            "gpu_id": gpu_id,
            "time": gpu_label(moment, kind),
            "order": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "shots": int(item.get("shots") or 0),
            "failed": int(item.get("failed") or 0),
            "avg_ms": gpu_avg(item.get("elapsed_sum"), item.get("elapsed_n")),
        })
    by_time = {}
    for item in gpu_rows:
        if item.get("avg_ms"):
            by_time.setdefault(item["time"], []).append(item["avg_ms"])
    for item in gpu_rows:
        middle = gpu_median(by_time.get(item["time"], []))
        item["slow"] = bool(middle and item.get("avg_ms") and item["avg_ms"] > middle * 1.25)
    gpu_rows.sort(key=lambda item: (item["gpu_id"], item["order"]))
    for item in gpu_rows:
        item.pop("order", None)
    return {"ip": ip, "step": kind, "points": points, "gpus": gpu_rows}


def handle_gpu_trend(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        if not module_allowed(user, "can_use_report"):
            raise ValueError("没有日志报表权限")
        source_id = (form.get("sourceId") or "").strip()
        if not source_id.isdigit():
            raise ValueError("请选择数据源")
        if not can_query_source(user, source_id):
            raise ValueError("没有这个数据源的权限")
        window = (form.get("window") or "24h").strip()
        if window not in ("1h", "6h", "24h", "today", "7d", "30d"):
            window = "24h"
        self._json(200, gpu_trend(source_id, window, (form.get("ip") or "").strip()))
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})


def handle_gpu_report(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        if not module_allowed(user, "can_use_report"):
            raise ValueError("没有日志报表权限")
        source_id = (form.get("sourceId") or "").strip()
        if not source_id.isdigit():
            raise ValueError("请选择数据源")
        if not can_query_source(user, source_id):
            raise ValueError("没有这个数据源的权限")
        window = (form.get("window") or "24h").strip()
        if window not in ("1h", "6h", "24h", "today", "7d", "30d"):
            window = "24h"
        self._json(200, gpu_report(source_id, window))
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})


def handle_data_sources(self, action):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        if action == "list":
            report_scope = (form.get("scope") or "") == "report"
            if account_flags(user).get("is_admin") or (module_allowed(user, "can_use_datasource") and not report_scope):
                sources = list_data_sources()
            elif module_allowed(user, "can_use_report"):
                sources = list_data_sources(user)
            else:
                raise ValueError("没有权限")
            self._json(200, {"sources": sources})
            return
        if not module_allowed(user, "can_use_datasource"):
            raise ValueError("没有数据接入权限")
        elif action == "delete":
            delete_data_source(form.get("id") or "")
            self._json(200, {"ok": True})
        elif action == "test":
            test_data_source(form)
            self._json(200, {"ok": True})
        else:
            save_data_source(form)
            self._json(200, {"ok": True})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})


def handle_report(self, action):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        if not module_allowed(user, "can_use_report"):
            raise ValueError("没有日志报表权限")
        source_id = (form.get("sourceId") or "").strip()
        if not source_id.isdigit():
            raise ValueError("请选择数据源")
        if not can_query_source(user, source_id):
            raise ValueError("没有这个数据源的权限")
        if action == "tables":
            self._json(200, {"tables": report_tables(source_id)})
        elif action == "columns":
            self._json(200, {"columns": report_columns(source_id, form.get("table") or "")})
        else:
            self._json(200, report_query(source_id, form.get("sql") or ""))
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})




def saved_report_rows(username):
    admin = account_flags(username).get("is_admin")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            sql = """
                SELECT r.id, r.name, r.source_id, s.name AS source_name, r.table_name, r.chart_kind,
                       r.axis_x, r.axis_y, r.sql_text, r.created_by, r.updated_at
                FROM saved_reports r
                LEFT JOIN data_sources s ON s.id=r.source_id
            """
            args = ()
            if not admin:
                sql += " WHERE r.source_id IN (SELECT source_id FROM user_data_sources WHERE username=%s)"
                args = (username,)
            sql += " ORDER BY r.updated_at DESC, r.id DESC"
            cur.execute(sql, args)
            rows = []
            for item in cur.fetchall():
                updated = item.get("updated_at")
                item["updated_at"] = updated.strftime("%Y-%m-%d %H:%M:%S") if updated else ""
                item["source_name"] = item.get("source_name") or ""
                rows.append(item)
            return rows


def save_saved_report(username, form):
    name = (form.get("name") or "").strip()
    source_id = (form.get("sourceId") or "").strip()
    table_name = (form.get("table") or "").strip()
    kind = (form.get("kind") or "bar").strip()
    axis_x = (form.get("axisX") or "").strip()[:80]
    axis_y = (form.get("axisY") or "").strip()[:80]
    statement = read_only_sql(form.get("sql") or "")
    report_id = (form.get("id") or "").strip()
    if not name:
        raise ValueError("请填写报表名称")
    if len(name) > 80:
        raise ValueError("名称不能超过 80 个字")
    if kind not in ("bar", "line", "table"):
        raise ValueError("图表类型不正确")
    if not source_id.isdigit() or not can_query_source(username, source_id):
        raise ValueError("没有这个数据源的权限")
    if table_name and not re.fullmatch(r"[A-Za-z0-9_]+", table_name):
        raise ValueError("数据表不正确")
    if len(statement) > 20000:
        raise ValueError("查询语句过长")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_data_sources(cur)
            if report_id:
                if not report_id.isdigit():
                    raise ValueError("报表不存在")
                cur.execute("SELECT id FROM saved_reports WHERE id=%s", (int(report_id),))
                if not cur.fetchone():
                    raise ValueError("报表不存在")
                cur.execute(
                    """
                    UPDATE saved_reports
                    SET name=%s, source_id=%s, table_name=%s, chart_kind=%s, axis_x=%s, axis_y=%s, sql_text=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (name, int(source_id), table_name[:80], kind, axis_x, axis_y, statement, datetime.now(), int(report_id)),
                )
                return int(report_id)
            cur.execute(
                """
                INSERT INTO saved_reports
                  (name, source_id, table_name, chart_kind, axis_x, axis_y, sql_text, created_by, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (name, int(source_id), table_name[:80], kind, axis_x, axis_y, statement, username, datetime.now()),
            )
            return cur.lastrowid


def delete_saved_report(username, report_id):
    if not str(report_id).isdigit():
        raise ValueError("报表不存在")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source_id FROM saved_reports WHERE id=%s", (int(report_id),))
            row = cur.fetchone()
            if not row:
                raise ValueError("报表不存在")
            if not can_query_source(username, row["source_id"]):
                raise ValueError("没有这个数据源的权限")
            cur.execute("DELETE FROM saved_reports WHERE id=%s", (int(report_id),))


def handle_saved_reports(self, action):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        if not module_allowed(user, "can_use_report"):
            raise ValueError("没有日志报表权限")
        if action == "list":
            self._json(200, {"reports": saved_report_rows(user)})
        elif action == "delete":
            delete_saved_report(user, form.get("id") or "")
            self._json(200, {"ok": True})
        else:
            report_id = save_saved_report(user, form)
            self._json(200, {"ok": True, "id": report_id})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})

def ensure_feishu_report(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS feishu_slow_report (
          id TINYINT PRIMARY KEY,
          webhook VARCHAR(500) NOT NULL DEFAULT '',
          enabled TINYINT NOT NULL DEFAULT 0,
          send_hour TINYINT NOT NULL DEFAULT 9,
          send_minute TINYINT NOT NULL DEFAULT 0,
          last_sent_on DATE NULL,
          last_attempt DATETIME NULL
        )
        """
    )
    cur.execute("INSERT IGNORE INTO feishu_slow_report (id, webhook, enabled, send_hour, send_minute) VALUES (1,'',0,9,0)")


def feishu_settings():
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_feishu_report(cur)
            cur.execute("SELECT webhook, enabled, send_hour, send_minute, last_sent_on FROM feishu_slow_report WHERE id=1")
            row = cur.fetchone() or {}
    sent = row.get("last_sent_on")
    return {
        "webhook": row.get("webhook") or "",
        "enabled": int(row.get("enabled") or 0),
        "time": "%02d:%02d" % (int(row.get("send_hour") or 9), int(row.get("send_minute") or 0)),
        "lastSentOn": sent.strftime("%Y-%m-%d") if sent else "",
    }


def save_feishu_settings(form):
    webhook = (form.get("webhook") or "").strip()
    enabled = 1 if (form.get("enabled") or "") in ("1", "on", "true") else 0
    raw = (form.get("time") or "09:00").strip()
    parts = raw.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError("发送时间不正确")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("发送时间不正确")
    if enabled or webhook:
        check_feishu_webhook(webhook)
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_feishu_report(cur)
            cur.execute(
                "UPDATE feishu_slow_report SET webhook=%s, enabled=%s, send_hour=%s, send_minute=%s WHERE id=1",
                (webhook, enabled, hour, minute),
            )


def check_feishu_webhook(webhook):
    from urllib.parse import urlparse
    parsed = urlparse(webhook or "")
    if parsed.scheme != "https" or (parsed.hostname or "") not in ("open.feishu.cn", "open.larksuite.com"):
        raise ValueError("请填写飞书机器人地址")


def send_feishu_text(webhook, text):
    import json as json_lib
    import urllib.request
    check_feishu_webhook(webhook)
    payload = json_lib.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8", "replace")
    if '"code":0' not in body.replace(" ", "") and '"StatusCode":0' not in body.replace(" ", ""):
        raise ValueError("飞书没有接收：" + body[:120])


def ensure_project_feishu(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS project_feishu (
          project_id BIGINT PRIMARY KEY,
          webhook VARCHAR(500) NOT NULL DEFAULT '',
          enabled TINYINT NOT NULL DEFAULT 0,
          send_hour TINYINT NOT NULL DEFAULT 9,
          send_minute TINYINT NOT NULL DEFAULT 0,
          time_window VARCHAR(16) NOT NULL DEFAULT '15m',
          time_from VARCHAR(40) NOT NULL DEFAULT '',
          time_to VARCHAR(40) NOT NULL DEFAULT '',
          container_name VARCHAR(128) NOT NULL DEFAULT '',
          realtime TINYINT NOT NULL DEFAULT 0,
          last_sent_on DATE NULL,
          last_attempt DATETIME NULL
        )
        """
    )


def project_feishu_rows(username):
    admin = bool(account_flags(username).get("is_admin"))
    sql = """
        SELECT p.id AS project_id, p.name,
               f.webhook, f.enabled, f.send_hour, f.send_minute, f.time_window, f.time_from, f.time_to,
               f.container_name, f.realtime, f.last_sent_on
        FROM projects p
        LEFT JOIN project_feishu f ON f.project_id=p.id
    """
    args = ()
    if not admin:
        sql += " WHERE p.id IN (SELECT project_id FROM user_projects WHERE username=%s)"
        args = (username,)
    sql += " ORDER BY p.id DESC"
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_project_feishu(cur)
            cur.execute(sql, args)
            rows = []
            for row in cur.fetchall():
                sent = row.get("last_sent_on")
                rows.append({
                    "project_id": row["project_id"],
                    "name": row["name"],
                    "webhook": (row.get("webhook") or "") if admin else "",
                    "enabled": int(row.get("enabled") or 0),
                    "time": "%02d:%02d" % (int(row.get("send_hour") or 9), int(row.get("send_minute") or 0)),
                    "time_window": row.get("time_window") or "15m",
                    "time_from": row.get("time_from") or "",
                    "time_to": row.get("time_to") or "",
                    "container_name": row.get("container_name") or "",
                    "realtime": int(row.get("realtime") or 0),
                    "lastSentOn": sent.strftime("%Y-%m-%d") if sent else "",
                })
    return rows


def save_project_feishu(username, form):
    project_id = (form.get("projectId") or "").strip()
    if not project_id.isdigit() or not can_use_project(username, project_id):
        raise ValueError("没有这个项目的权限")
    window = (form.get("window") or "15m").strip()
    if window not in ("15m", "1h", "6h", "custom"):
        window = "15m"
    admin = bool(account_flags(username).get("is_admin"))
    webhook = (form.get("webhook") or "").strip()
    enabled = 1 if (form.get("enabled") or "") in ("1", "on", "true") else 0
    raw = (form.get("time") or "09:00").strip() or "09:00"
    hour_text, minute_text = (raw.split(":") + ["0"])[:2]
    if not hour_text.isdigit() or not minute_text.isdigit():
        raise ValueError("发送时间不正确")
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("发送时间不正确")
    if admin and (enabled or webhook):
        check_feishu_webhook(webhook)
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_project_feishu(cur)
            cur.execute("SELECT webhook, enabled, send_hour, send_minute FROM project_feishu WHERE project_id=%s", (int(project_id),))
            current = cur.fetchone() or {}
            if not admin:
                webhook = current.get("webhook") or ""
                enabled = int(current.get("enabled") or 0)
                hour = int(current.get("send_hour") or hour)
                minute = int(current.get("send_minute") or minute)
            cur.execute(
                """
                INSERT INTO project_feishu
                  (project_id, webhook, enabled, send_hour, send_minute, time_window, time_from, time_to, container_name, realtime)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  webhook=VALUES(webhook), enabled=VALUES(enabled), send_hour=VALUES(send_hour), send_minute=VALUES(send_minute),
                  time_window=VALUES(time_window), time_from=VALUES(time_from), time_to=VALUES(time_to),
                  container_name=VALUES(container_name), realtime=VALUES(realtime)
                """,
                (
                    int(project_id), webhook, enabled, hour, minute, window,
                    (form.get("timeFrom") or "")[:40], (form.get("timeTo") or "")[:40],
                    (form.get("container") or "")[:128],
                    1 if (form.get("realtime") or "") in ("1", "on", "true") else 0,
                ),
            )


def send_slow_report_now(project_id=None):
    import summary
    project_id = str(project_id or "").strip()
    if not project_id.isdigit():
        raise ValueError("请选择项目")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_project_feishu(cur)
            cur.execute("SELECT webhook FROM project_feishu WHERE project_id=%s", (int(project_id),))
            row = cur.fetchone() or {}
    webhook = row.get("webhook") or ""
    if not webhook:
        raise ValueError("请先填写飞书机器人地址")
    text = summary.slow_interface_text(project_id=project_id)
    send_feishu_text(webhook, text)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE project_feishu SET last_sent_on=%s, last_attempt=%s WHERE project_id=%s",
                (datetime.now().date(), datetime.now(), int(project_id)),
            )
    return text


def maybe_send_slow_report():
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_project_feishu(cur)
            cur.execute("SELECT project_id, webhook, enabled, send_hour, send_minute, last_sent_on, last_attempt FROM project_feishu WHERE enabled=1 AND webhook<>''")
            rows = cur.fetchall()
    now = datetime.now(SHANGHAI)
    for settings_row in rows:
        scheduled = now.replace(hour=int(settings_row["send_hour"]), minute=int(settings_row["send_minute"]), second=0, microsecond=0)
        if now < scheduled:
            continue
        sent = settings_row.get("last_sent_on")
        if sent and str(sent)[:10] == now.strftime("%Y-%m-%d"):
            continue
        attempt = settings_row.get("last_attempt")
        if attempt and (datetime.now() - attempt).total_seconds() < 600:
            continue
        project_id = settings_row["project_id"]
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE project_feishu SET last_attempt=%s WHERE project_id=%s", (datetime.now(), project_id))
        try:
            send_slow_report_now(project_id)
            print("慢接口报表已发送 %s" % project_id)
        except Exception as exc:
            print("慢接口报表发送失败: %s" % exc)




def handle_project_feishu(self, action):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        if action == "list":
            self._json(200, {"rows": project_feishu_rows(user)})
        else:
            save_project_feishu(user, form)
            self._json(200, {"ok": True})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})


def handle_feishu_report(self, action):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        require_admin(user)
        if action == "get":
            self._json(200, feishu_settings())
        elif action == "send":
            send_slow_report_now(form.get("projectId") or "")
            self._json(200, {"ok": True})
        else:
            save_feishu_settings(form)
            self._json(200, {"ok": True})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})



def handle_source_update(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        require_admin(user)
        source_id = (form.get("id") or "").strip()
        name = (form.get("name") or "").strip()
        base_url = (form.get("baseUrl") or "").strip().rstrip("/")
        account_id = (form.get("accountId") or "1").strip() or "1"
        project_id = (form.get("projectId") or "1").strip() or "1"
        if not source_id.isdigit():
            raise ValueError("日志接入不存在")
        if not name or not base_url:
            raise ValueError("请填写名称和地址")
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM log_sources WHERE id=%s", (int(source_id),))
                if not cur.fetchone():
                    raise ValueError("日志接入不存在")
                cur.execute(
                    "UPDATE log_sources SET name=%s, base_url=%s, account_id=%s, project_id=%s WHERE id=%s",
                    (name[:80], base_url[:300], account_id[:20], project_id[:20], int(source_id)),
                )
        self._json(200, {"ok": True})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})


def Handler_do_POST(self):
    _limits.flags = {"seedLimited": False, "requestLimited": False, "chainLimited": False, "focused": False, "seedCap": SEED_LIMIT}
    _limits.trace_ids = []
    path = urlparse(self.path).path
    if path == "/api/audit/list":
        return handle_audit_list(self)
    if path == "/api/sources/update":
        return handle_source_update(self)
    if path == "/api/login/anonymous":
        return handle_anonymous_login(self)
    if path == "/api/new-api-stats":
        return handle_new_api_stats(self)
    if path == "/api/hosts":
        return handle_hosts(self)
    if path == "/api/saved-stats/list":
        return handle_saved_stats(self, "list")
    if path == "/api/saved-stats/delete":
        return handle_saved_stats(self, "delete")
    if path == "/api/saved-stats":
        return handle_saved_stats(self, "save")
    if path == "/api/users/permissions":
        return handle_user_permissions(self)
    if path == "/api/users":
        return handle_user_create(self)
    if path == "/api/data-sources/list":
        return handle_data_sources(self, "list")
    if path == "/api/data-sources/delete":
        return handle_data_sources(self, "delete")
    if path == "/api/data-sources/test":
        return handle_data_sources(self, "test")
    if path == "/api/data-sources":
        return handle_data_sources(self, "save")
    if path == "/api/reports/saved/list":
        return handle_saved_reports(self, "list")
    if path == "/api/reports/saved/delete":
        return handle_saved_reports(self, "delete")
    if path == "/api/reports/saved":
        return handle_saved_reports(self, "save")
    if path == "/api/reports/gpu/trend":
        return handle_gpu_trend(self)
    if path == "/api/reports/gpu":
        return handle_gpu_report(self)
    if path == "/api/reports/tables":
        return handle_report(self, "tables")
    if path == "/api/reports/columns":
        return handle_report(self, "columns")
    if path == "/api/reports/query":
        return handle_report(self, "query")
    if path == "/api/feishu-report/get":
        return handle_feishu_report(self, "get")
    if path == "/api/feishu-report/send":
        return handle_feishu_report(self, "send")
    if path == "/api/feishu-report":
        return handle_feishu_report(self, "save")
    if path == "/api/project-feishu/list":
        return handle_project_feishu(self, "list")
    if path == "/api/project-feishu":
        return handle_project_feishu(self, "save")
    return _orig_do_post(self)


Handler.do_POST = Handler_do_POST


_orig_read_form = read_form


def read_form(handler):
    form = _orig_read_form(handler)
    handler._audit_form = form
    return form


_AUDIT_SECRET = ("password", "oldpassword", "newpassword", "webhook")
_AUDIT_ACTIONS = {
    ("POST", "/api/login"): "登录",
    ("POST", "/api/login/anonymous"): "匿名登录",
    ("POST", "/api/logout"): "退出",
    ("POST", "/api/password"): "修改密码",
    ("POST", "/api/search"): "查询链路",
    ("POST", "/api/projects"): "新建项目",
    ("POST", "/api/projects/update"): "修改项目",
    ("POST", "/api/projects/delete"): "删除项目",
    ("GET", "/api/projects"): "查看项目",
    ("POST", "/api/links"): "保存链路",
    ("POST", "/api/links/delete"): "删除链路",
    ("GET", "/api/links"): "查看链路",
    ("POST", "/api/api-stats"): "查询接口统计",
    ("POST", "/api/stats/containers"): "查看容器",
    ("GET", "/api/namespaces/containers"): "按命名空间查看容器",
    ("POST", "/api/sources"): "添加日志接入",
    ("POST", "/api/sources/update"): "修改日志接入",
    ("POST", "/api/sources/delete"): "删除日志接入",
    ("GET", "/api/sources"): "查看日志接入",
    ("POST", "/api/fields"): "添加查询条件",
    ("POST", "/api/fields/delete"): "删除查询条件",
    ("GET", "/api/fields"): "查看查询条件",
    ("POST", "/api/users"): "添加用户",
    ("GET", "/api/users"): "查看用户",
    ("POST", "/api/users/permissions"): "修改用户权限",
    ("POST", "/api/data-sources"): "保存数据接入",
    ("POST", "/api/data-sources/delete"): "删除数据接入",
    ("POST", "/api/data-sources/test"): "测试数据接入",
    ("POST", "/api/data-sources/list"): "查看数据接入",
    ("POST", "/api/reports/gpu"): "查看GPU机器",
    ("POST", "/api/reports/gpu/trend"): "查看GPU机器变化",
    ("POST", "/api/reports/query"): "绘制报表",
    ("POST", "/api/reports/saved"): "保存报表",
    ("POST", "/api/reports/saved/delete"): "删除报表",
    ("POST", "/api/reports/saved/list"): "查看报表",
    ("POST", "/api/reports/tables"): "查看报表数据表",
    ("POST", "/api/reports/columns"): "查看报表字段",
    ("POST", "/api/saved-stats"): "保存统计",
    ("POST", "/api/saved-stats/delete"): "删除统计",
    ("POST", "/api/saved-stats/list"): "查看统计",
    ("POST", "/api/feishu-report"): "保存飞书设置",
    ("POST", "/api/feishu-report/send"): "发送飞书报表",
    ("POST", "/api/feishu-report/get"): "查看飞书设置",
    ("POST", "/api/project-feishu"): "保存项目飞书设置",
    ("POST", "/api/project-feishu/list"): "查看项目飞书设置",
    ("POST", "/api/hosts"): "查看主机",
    ("POST", "/api/new-api-stats"): "查询新增接口",
    ("GET", "/api/home"): "查看首页",
    ("POST", "/api/audit/list"): "查看操作审计",
}


def audit_action(method, path):
    return _AUDIT_ACTIONS.get((method, path), path.replace("/api/", "") or path)


def audit_detail(handler):
    form = getattr(handler, "_audit_form", None) or {}
    query = parse_qs(urlparse(handler.path).query)
    parts = []
    items = []
    if hasattr(form, "items"):
        items.extend(form.items())
    for key, values in query.items():
        items.append((key, values[0] if isinstance(values, list) and values else values))
    for key, value in items:
        name = str(key or "")
        if name.lower() in _AUDIT_SECRET or "password" in name.lower():
            continue
        text = str(value or "").replace("\n", " ").strip()
        if not text:
            continue
        parts.append("%s=%s" % (name, text[:160]))
    return "；".join(parts)[:1000]


def audit_username(handler, path):
    form = getattr(handler, "_audit_form", None) or {}
    if path == "/api/login/anonymous":
        return "anonymous"
    if path == "/api/login":
        return (form.get("username") if hasattr(form, "get") else "") or "未知"
    try:
        name = current_username(handler)
    except Exception:
        name = ""
    return name or "未登录"


def write_audit(handler, path, state):
    method = getattr(handler, "command", "POST") or "POST"
    address = ""
    if getattr(handler, "client_address", None):
        address = str(handler.client_address[0] or "")
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_audit_logs(cur)
            cur.execute(
                """
                INSERT INTO audit_logs (username, action, method, path, detail, success, ip, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    audit_username(handler, path)[:64],
                    audit_action(method, path)[:80],
                    method[:8],
                    path[:191],
                    audit_detail(handler),
                    1 if int(state.get("code") or 200) < 400 and not state.get("error") else 0,
                    address[:64],
                    datetime.now(),
                ),
            )


def run_audited(handler, method):
    path = urlparse(handler.path).path.split("?")[0]
    if not path.startswith("/api/") or path == "/api/me":
        return method(handler)
    state = {"code": 200, "error": ""}
    original_json = handler._json

    def wrapped_json(code, payload, extra=None):
        state["code"] = int(code or 200)
        if isinstance(payload, dict) and payload.get("error"):
            state["error"] = str(payload.get("error"))[:180]
        if extra is None:
            return original_json(code, payload)
        return original_json(code, payload, extra)

    handler._json = wrapped_json
    try:
        return method(handler)
    except Exception as exc:
        state["code"] = 500
        state["error"] = str(exc)[:180]
        raise
    finally:
        handler._json = original_json
        try:
            write_audit(handler, path, state)
        except Exception:
            pass


def handle_audit_list(self):
    form = read_form(self)
    user = self._user()
    if not user:
        return
    try:
        require_admin(user)
        username = (form.get("username") or "").strip()
        action = (form.get("action") or "").strip()
        sql = "SELECT id, username, action, detail, success, ip, created_at FROM audit_logs WHERE 1=1"
        args = []
        if username:
            sql += " AND username=%s"
            args.append(username)
        if action:
            sql += " AND action LIKE %s"
            args.append("%" + action + "%")
        sql += " ORDER BY id DESC LIMIT 500"
        with connect() as conn:
            with conn.cursor() as cur:
                ensure_audit_logs(cur)
                cur.execute(sql, tuple(args))
                rows = []
                for item in cur.fetchall():
                    created = item.get("created_at")
                    item["created_at"] = created.strftime("%Y-%m-%d %H:%M:%S") if created else ""
                    item["success"] = int(item.get("success") or 0)
                    rows.append(item)
        self._json(200, {"rows": rows, "limited": len(rows) >= 500})
    except ValueError as exc:
        self._json(400, {"error": str(exc)})
    except Exception as exc:
        self._json(400, {"error": str(exc)[:180]})


_plain_post = Handler_do_POST


def Handler_do_POST(self):
    return run_audited(self, _plain_post)


Handler.do_POST = Handler_do_POST



def query_logs(source, conditions, window, namespace_name, time_from="", time_to="", limit=SEED_LIMIT, deploy_mode="kubernetes", containers=""):
    span = time_filter(window, time_from, time_to)
    groups = []
    keys = []
    trace_ids = []
    for field, keyword in conditions:
        needles = search_needles(field, keyword)
        groups.append("(" + " OR ".join(json.dumps(item, ensure_ascii=False) for item in needles) + ")")
        if field["kind"] in ("trace_id", "log_id"):
            value = keyword.strip()
            if value and value not in trace_ids:
                trace_ids.append(value)
            if value and value not in keys:
                keys.append(value)
    focused = bool(trace_ids)
    _limits.trace_ids = trace_ids if focused else []
    fetch_limit = TRACE_FETCH_LIMIT if focused else limit
    quoted = " AND ".join(groups)
    scope = scope_filter(deploy_mode, namespace_name)
    containers_clause = container_filter(containers)
    query = '%s %s%s(%s) AND NOT "PING" | first %s by (_time desc) | fields _time, container_name, _msg' % (span, scope, containers_clause, quoted, fetch_limit)
    rows = fetch_rows(source, query, fetch_limit)
    flags = _flag_box()
    flags["focused"] = flags.get("focused") or focused
    flags["seedCap"] = fetch_limit if focused else (flags.get("seedCap") or limit)
    flags["seedLimited"] = flags["seedLimited"] or len(rows) >= fetch_limit
    if focused:
        return rows
    request_limited = False
    for row in rows:
        key = parse_event(row, conditions[0][1])["request_key"]
        if key != UNLINKED_KEY and key not in keys:
            keys.append(key)
        if len(keys) >= REQUEST_LIMIT:
            request_limited = True
            break
    flags["requestLimited"] = flags["requestLimited"] or request_limited
    if not keys:
        return rows
    quoted_ids = " OR ".join(json.dumps(key) for key in keys)
    chain_query = "%s %s%s(%s) | first %s by (_time asc) | fields _time, container_name, _msg" % (span, scope, containers_clause, quoted_ids, CHAIN_LIMIT)
    chain = fetch_rows(source, chain_query, CHAIN_LIMIT)
    flags["chainLimited"] = flags["chainLimited"] or len(chain) >= CHAIN_LIMIT
    seen = set()
    merged = []
    for row in rows + chain:
        marker = (row.get("_time"), row.get("container_name"), row.get("_msg"))
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(row)
    return merged


def save_search(project_id, user_id, window, events):
    loose = 0
    stored = []
    for event in events:
        item = dict(event)
        if item.get("request_key") == UNLINKED_KEY:
            loose += 1
            item["request_key"] = "%s%d" % (LOOSE_PREFIX, loose)
        item["message"] = item.get("message") or ""
        stored.append(item)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO searches (project_id, user_id, time_window, created_at, hit_count, request_count) VALUES (%s,%s,%s,%s,%s,%s)",
                (project_id, user_id, window, datetime.now(), len(stored), len({item["request_key"] for item in stored})),
            )
            search_id = cur.lastrowid
            if stored:
                cur.executemany(
                    """
                    INSERT INTO events
                    (search_id, event_time, container_name, request_key, user_id, uri, http_method, status_code, duration, summary, span_id, parent_span_id, message)
                    VALUES (%(search_id)s,%(event_time)s,%(container_name)s,%(request_key)s,%(user_id)s,%(uri)s,%(http_method)s,%(status_code)s,%(duration)s,%(summary)s,%(span_id)s,%(parent_span_id)s,%(message)s)
                    """,
                    [{**item, "search_id": search_id} for item in stored],
                )
    return search_id


_orig_load_result = load_result


def load_result(search_id):
    result = _orig_load_result(search_id)
    for item in result.get("requests") or []:
        item["unlinked"] = str(item.get("request_key") or "").startswith(LOOSE_PREFIX)
    result["requests"] = sorted(
        result.get("requests") or [],
        key=lambda item: (
            1 if item.get("unlinked") else 0,
            0 if item.get("error_count") else 1,
            -(item.get("max_duration_ms") or 0),
            str(item.get("started_at") or ""),
        ),
    )
    result["note"] = trace_limit_note(_flag_box())
    return result



def api_stats(sources, deploy_mode, namespaces, containers, window, time_from="", time_to="", include_other_formats=True, offset_days=0):
    span = time_filter(window, time_from, time_to, offset_days)
    prefix = "%s %s%s" % (span, scope_filter(deploy_mode, namespaces), container_filter(containers))
    stats = (
        "stats by (container_name, method, uri) count() as hits, min(%s) as min_ms, max(%s) as max_ms, "
        "avg(%s) as avg_ms, quantile(0.95, %s) as p95_ms, count() if (status:>=400) as errors | sort by (hits desc) | limit 1000"
    )
    queries = [(
        1,
        prefix + '"Uri=[" | extract_regexp `Uri=\\[(?P<uri>[^?\\]]+)` | extract_regexp `Method=\\[(?P<method>[A-Z]+)\\]` | '
        'extract `Time=[<duration>ms]` | extract `Status=[<status>]` | ' + (stats % ("duration", "duration", "duration", "duration")),
    )]
    if include_other_formats:
        queries.append((
            1000,
            prefix + '" HTTP/" | extract_regexp `"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (?P<uri>/[^ ?"]*)[^"]* HTTP/` | '
            'extract_regexp `HTTP/[^"]+" (?:status:)?(?P<status>\\d{3})` | extract_regexp `req_time:(?P<seconds>[0-9.]+)` | '
            + (stats % ("seconds", "seconds", "seconds", "seconds")),
        ))
        queries.append((
            1,
            prefix + '`path":"` | unpack_json | stats by (container_name, path) count() as hits, min(latency) as min_ms, '
            "max(latency) as max_ms, avg(latency) as avg_ms, quantile(0.95, latency) as p95_ms, count() if (status:>=400) as errors | "
            "sort by (hits desc) | limit 1000",
        ))
    from concurrent.futures import ThreadPoolExecutor, wait

    def run(scale, query):
        found = []
        failure = None
        for source in sources:
            try:
                found.extend(fetch_rows(source, query, 1000, 25))
            except Exception as exc:
                failure = exc
        return scale, found, failure

    pool = ThreadPoolExecutor(max_workers=max(1, len(queries)))
    futures = [pool.submit(run, scale, query) for scale, query in queries]
    done, pending = wait(futures, timeout=28)
    pool.shutdown(wait=False, cancel_futures=True)
    merged = {}
    failures = []
    allowed = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    for fut in done:
        if fut.cancelled():
            continue
        error = fut.exception()
        if error:
            failures.append(error)
            continue
        scale, found, failure = fut.result()
        if failure:
            failures.append(failure)
        for row in found:
                uri = str(row.get("uri") or row.get("path") or "").split("?")[0].split("#")[0].strip()
                container = str(row.get("container_name") or "").strip()
                method = str(row.get("method") or "").strip().upper()
                if method not in allowed:
                    method = ""
                hits = int(num(row.get("hits")) or 0)
                if not uri or not container or hits <= 0 or uri == "on":
                    continue
                def scaled(field, scale=scale):
                    value = num(row.get(field))
                    return None if value is None else value * scale
                key = (container, method, uri)
                item = merged.get(key)
                avg_ms, p95_ms, min_ms, max_ms = scaled("avg_ms"), scaled("p95_ms"), scaled("min_ms"), scaled("max_ms")
                errors = int(num(row.get("errors")) or 0)
                if not item:
                    merged[key] = {
                        "container_name": container, "method": method, "uri": uri, "hits": hits, "errors": errors,
                        "min_ms": min_ms, "max_ms": max_ms, "avg_ms": avg_ms, "p95_ms": p95_ms,
                    }
                    continue
                total = item["hits"] + hits
                if avg_ms is not None and item.get("avg_ms") is not None and total:
                    item["avg_ms"] = (item["avg_ms"] * item["hits"] + avg_ms * hits) / total
                elif item.get("avg_ms") is None:
                    item["avg_ms"] = avg_ms
                for field, value in (("p95_ms", p95_ms), ("max_ms", max_ms)):
                    if value is not None:
                        item[field] = value if item.get(field) is None else max(item[field], value)
                if min_ms is not None:
                    item["min_ms"] = min_ms if item.get("min_ms") is None else min(item["min_ms"], min_ms)
                item["hits"] = total
                item["errors"] += errors
    if not merged and (pending or failures):
        timed_out = bool(pending) or any(("超时" in str(item)) or ("timed out" in str(item).lower()) for item in failures)
        if timed_out:
            raise ValueError("实时统计超时，请先选择一个容器，或取消实时改用汇总")
        raise ValueError("实时统计失败：" + str(failures[0])[:80])
    return sorted(merged.values(), key=lambda item: item["hits"], reverse=True)[:1000]


if __name__ == "__main__":
    init_db()
    import summary
    summary.start()
    host = CFG["server"].get("host") or "0.0.0.0"
    port = int(CFG["server"].get("port") or 8789)
    print("TraceLog 监听 http://%s:%s" % (host, port))
    ThreadingHTTPServer((host, port), Handler).serve_forever()

_orig_do_get = Handler.do_GET


def Handler_do_GET(self):
    path = urlparse(self.path).path
    if path == "/api/me":
        return handle_me(self)
    if path == "/api/users":
        return handle_users_get(self)
    return _orig_do_get(self)


Handler.do_GET = Handler_do_GET

_plain_get = Handler_do_GET


def Handler_do_GET(self):
    return run_audited(self, _plain_get)


Handler.do_GET = Handler_do_GET
