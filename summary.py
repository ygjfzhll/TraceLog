import hashlib
import re
import threading
import time
from datetime import datetime, timedelta

BUCKET_NAMES = ("b10", "b50", "b100", "b500", "b1000", "b3000", "bover")
MS_EDGES = (10, 50, 100, 500, 1000, 3000)
SEC_EDGES = (0.01, 0.05, 0.1, 0.5, 1, 3)
DONE_HASH = hashlib.sha1(b"__bucket_done__").hexdigest()


def _app():
    import app
    return app


def han_initial(ch):
    if "a" <= ch.lower() <= "z" or ch.isdigit():
        return ch.lower()
    try:
        gb = ch.encode("gbk")
    except UnicodeEncodeError:
        return ""
    if len(gb) == 1:
        return gb.decode("gbk").lower()
    asc = gb[0] * 256 + gb[1] - 65536
    ranges = [
        (-20319, -20284, "a"), (-20283, -19776, "b"), (-19775, -19219, "c"),
        (-19218, -18711, "d"), (-18710, -18527, "e"), (-18526, -18240, "f"),
        (-18239, -17923, "g"), (-17922, -17418, "h"), (-17417, -16475, "j"),
        (-16474, -16213, "k"), (-16212, -15641, "l"), (-15640, -15166, "m"),
        (-15165, -14923, "n"), (-14922, -14915, "o"), (-14914, -14631, "p"),
        (-14630, -14150, "q"), (-14149, -14091, "r"), (-14090, -13319, "s"),
        (-13318, -12839, "t"), (-12838, -12557, "w"), (-12556, -11848, "x"),
        (-11847, -11056, "y"), (-11055, -10247, "z"),
    ]
    for start, end, letter in ranges:
        if start <= asc <= end:
            return letter
    return ""


def project_initials(project_name):
    letters = "".join(han_initial(ch) for ch in (project_name or "") if han_initial(ch))
    return letters or "p"


def namespace_parts(namespaces):
    parts = []
    for item in re.split(r"[\s,;]+", namespaces or ""):
        token = "".join(ch.lower() if ch.isalnum() else "_" for ch in item.strip())
        token = "_".join(piece for piece in token.split("_") if piece)
        if token and token not in parts and token != "on":
            parts.append(token)
    return parts


def summary_table_name(project_name, namespaces, project_id=None):
    base = "_".join([project_initials(project_name)] + namespace_parts(namespaces)) + "_summary"
    base = re.sub(r"_+", "_", base).strip("_")
    if project_id and len(base) > 64:
        suffix = "_%s" % int(project_id)
        base = base[:64 - len(suffix)].rstrip("_") + suffix
    return base[:64]


def table_exists(cur, name):
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s",
        (name,),
    )
    return int(cur.fetchone()["n"]) > 0


def naive(moment):
    app = _app()
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(app.SHANGHAI).replace(tzinfo=None)


def floor_bucket(moment):
    moment = naive(moment).replace(second=0, microsecond=0)
    return moment.replace(minute=moment.minute - (moment.minute % 5))


def ensure_registry(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stats_tables (
          project_id BIGINT PRIMARY KEY,
          project_name VARCHAR(80) NOT NULL,
          table_name VARCHAR(64) NOT NULL,
          namespaces TEXT NULL,
          updated_at DATETIME NOT NULL
        )
        """
    )


def _create_sql(name):
    return """
        CREATE TABLE IF NOT EXISTS `%s` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          project_name VARCHAR(80) NOT NULL,
          namespace_name VARCHAR(191) NOT NULL,
          container_name VARCHAR(128) NOT NULL,
          http_method VARCHAR(16) NOT NULL DEFAULT '',
          uri VARCHAR(512) NOT NULL,
          uri_hash CHAR(40) NOT NULL,
          bucket_start DATETIME NOT NULL,
          hits INT NOT NULL,
          errors INT NOT NULL,
          min_ms DOUBLE NULL,
          max_ms DOUBLE NULL,
          total_ms DOUBLE NOT NULL DEFAULT 0,
          duration_hits INT NOT NULL DEFAULT 0,
          b10 INT NOT NULL DEFAULT 0,
          b50 INT NOT NULL DEFAULT 0,
          b100 INT NOT NULL DEFAULT 0,
          b500 INT NOT NULL DEFAULT 0,
          b1000 INT NOT NULL DEFAULT 0,
          b3000 INT NOT NULL DEFAULT 0,
          bover INT NOT NULL DEFAULT 0,
          UNIQUE KEY uniq_stat (namespace_name(128), container_name, http_method, uri_hash, bucket_start),
          KEY idx_bucket (bucket_start)
        )
    """ % name


def ensure_project_table(cur, project_id, project_name, namespaces):
    ensure_registry(cur)
    desired = summary_table_name(project_name, namespaces, project_id)
    cur.execute("SELECT table_name FROM stats_tables WHERE project_name=%s AND project_id<>%s", ((project_name or "")[:80], int(project_id)))
    # collision is checked by table name below
    cur.execute("SELECT table_name FROM stats_tables WHERE project_id=%s", (int(project_id),))
    current = cur.fetchone()
    legacy = "tl_stats_%s" % int(project_id)
    existing = ""
    if current and current.get("table_name"):
        existing = current["table_name"]
    elif table_exists(cur, legacy):
        existing = legacy
    if desired != existing:
        cur.execute("SELECT project_id FROM stats_tables WHERE table_name=%s AND project_id<>%s", (desired, int(project_id)))
        if cur.fetchone() or (table_exists(cur, desired) and existing != desired):
            suffix = "_%s" % int(project_id)
            desired = (desired[:64 - len(suffix)].rstrip("_") + suffix)[:64]
    if existing and existing != desired and table_exists(cur, existing) and not table_exists(cur, desired):
        cur.execute("RENAME TABLE `%s` TO `%s`" % (existing, desired))
    cur.execute(_create_sql(desired))
    ensure_method_column(cur, desired)
    cur.execute(
        """
        INSERT INTO stats_tables (project_id, project_name, table_name, namespaces, updated_at)
        VALUES (%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE project_name=VALUES(project_name), table_name=VALUES(table_name), namespaces=VALUES(namespaces), updated_at=VALUES(updated_at)
        """,
        (int(project_id), (project_name or "")[:80], desired, namespaces or "", datetime.now()),
    )
    cur.execute("UPDATE `%s` SET project_name=%%s" % desired, ((project_name or "")[:80],))
    return desired


def sync_project(project_id, project_name, namespaces):
    app = _app()
    with app.connect() as conn:
        with conn.cursor() as cur:
            ensure_project_table(cur, project_id, project_name, namespaces)


def drop_project(project_id):
    app = _app()
    with app.connect() as conn:
        with conn.cursor() as cur:
            ensure_registry(cur)
            cur.execute("SELECT table_name FROM stats_tables WHERE project_id=%s", (int(project_id),))
            row = cur.fetchone()
            names = []
            if row and row.get("table_name"):
                names.append(row["table_name"])
            names.append("tl_stats_%s" % int(project_id))
            for name in dict.fromkeys(names):
                cur.execute("DROP TABLE IF EXISTS `%s`" % name)
            cur.execute("DELETE FROM stats_tables WHERE project_id=%s", (int(project_id),))


def ensure_all():
    app = _app()
    with app.connect() as conn:
        with conn.cursor() as cur:
            ensure_registry(cur)
            cur.execute("SELECT id, name, namespace_name FROM projects")
            for project in cur.fetchall():
                ensure_project_table(cur, project["id"], project["name"], project.get("namespace_name") or "")


def _bucket_sql(field, edges):
    parts = ["count() if (%s:<%s) as b10" % (field, edges[0])]
    previous = edges[0]
    names = BUCKET_NAMES[1:-1]
    for edge, bucket_name in zip(edges[1:], names):
        parts.append("count() if (%s:>=%s AND %s:<%s) as %s" % (field, previous, field, edge, bucket_name))
        previous = edge
    parts.append("count() if (%s:>=%s) as bover" % (field, previous))
    return ", ".join(parts)


def _queries(span, scope):
    buckets_ms = _bucket_sql("duration", MS_EDGES)
    buckets_sec = _bucket_sql("seconds", SEC_EDGES)
    buckets_lat = _bucket_sql("latency", MS_EDGES)
    common = (
        " | stats by (container_name, method, uri) count() as hits, count() if (status:>=400) as errors, "
        "min(%s) as min_v, max(%s) as max_v, sum(%s) as total_v, %s | sort by (hits desc) | limit 1000"
    )
    return [
        (
            "ms",
            span + " " + scope + '"Uri=[" | extract_regexp `Uri=\\[(?P<uri>[^?\\]]+)` | extract_regexp `Method=\\[(?P<method>[A-Z]+)\\]` | extract `Time=[<duration>ms]` | extract `Status=[<status>]`'
            + (common % ("duration", "duration", "duration", buckets_ms)),
        ),
        (
            "sec",
            span + " " + scope + '" HTTP/" | extract_regexp `"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (?P<uri>/[^ ?"]*)[^"]* HTTP/` '
            '| extract_regexp `HTTP/[^"]+" (?:status:)?(?P<status>\\d{3})` | extract_regexp `req_time:(?P<seconds>[0-9.]+)`'
            + (common % ("seconds", "seconds", "seconds", buckets_sec)),
        ),
        (
            "ms",
            span + " " + scope + '`path":"` | unpack_json | stats by (container_name, path) count() as hits, '
            "count() if (status:>=400) as errors, min(latency) as min_v, max(latency) as max_v, sum(latency) as total_v, "
            + buckets_lat + " | sort by (hits desc) | limit 1000",
        ),
    ]


def _scale(unit, value):
    number = _app().num(value)
    if number is None:
        return None
    return number * 1000 if unit == "sec" else number


def _remember(merged, unit, row):
    app = _app()
    uri = str(row.get("uri") or row.get("path") or "").split("?")[0].split("#")[0].strip()
    container = str(row.get("container_name") or "").strip()
    method = str(row.get("method") or "").strip().upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        method = ""
    if not uri or not container or uri == "on" or uri.lower().endswith(app.STATIC_SUFFIXES):
        return
    hits = int(app.num(row.get("hits")) or 0)
    if hits <= 0:
        return
    errors = int(app.num(row.get("errors")) or 0)
    buckets = [int(app.num(row.get(bucket_name)) or 0) for bucket_name in BUCKET_NAMES]
    total = _scale(unit, row.get("total_v")) or 0
    min_ms = _scale(unit, row.get("min_v"))
    max_ms = _scale(unit, row.get("max_v"))
    current = merged.get((container, method, uri))
    if not current:
        merged[(container, method, uri)] = {
            "container_name": container, "method": method, "uri": uri, "hits": hits, "errors": errors,
            "min_ms": min_ms, "max_ms": max_ms, "total_ms": total,
            "duration_hits": sum(buckets), "buckets": buckets,
        }
        return
    current["hits"] += hits
    current["errors"] += errors
    current["total_ms"] += total
    current["duration_hits"] += sum(buckets)
    current["buckets"] = [left + right for left, right in zip(current["buckets"], buckets)]
    if min_ms is not None:
        current["min_ms"] = min_ms if current["min_ms"] is None else min(current["min_ms"], min_ms)
    if max_ms is not None:
        current["max_ms"] = max_ms if current["max_ms"] is None else max(current["max_ms"], max_ms)


def collect_bucket(sources, deploy_mode, namespace, start, end):
    app = _app()
    span = "_time:[%s, %s]" % (start.isoformat(), end.isoformat())
    scope = app.scope_filter(deploy_mode, namespace)
    merged = {}
    for unit, query in _queries(span, scope):
        for source in sources:
            try:
                for row in app.fetch_rows(source, query, 1000, 18):
                    _remember(merged, unit, row)
            except Exception:
                continue
    return list(merged.values())


def mark_bucket(cur, table, project_name, bucket_start):
    cur.execute(
        "INSERT INTO `%s` (project_name, namespace_name, container_name, uri, uri_hash, bucket_start, hits, errors, total_ms, duration_hits) "
        "VALUES (%%s,'','','',%%s,%%s,0,0,0,0) ON DUPLICATE KEY UPDATE project_name=VALUES(project_name)" % table,
        (project_name[:80], DONE_HASH, bucket_start),
    )


def save_bucket(cur, table, project_name, namespace, bucket_start, rows):
    if not rows:
        return
    values = []
    for row in rows:
        uri = row["uri"][:512]
        values.append((
            project_name[:80], namespace[:191], row["container_name"][:128], (row.get("method") or "")[:16], uri,
            hashlib.sha1(uri.encode("utf-8")).hexdigest(), bucket_start, row["hits"], row["errors"],
            row["min_ms"], row["max_ms"], row["total_ms"], row["duration_hits"], *row["buckets"],
        ))
    cur.executemany(
        "INSERT INTO `%s` (project_name, namespace_name, container_name, http_method, uri, uri_hash, bucket_start, hits, errors, "
        "min_ms, max_ms, total_ms, duration_hits, b10, b50, b100, b500, b1000, b3000, bover) "
        "VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s) "
        "ON DUPLICATE KEY UPDATE hits=VALUES(hits), errors=VALUES(errors), min_ms=VALUES(min_ms), max_ms=VALUES(max_ms), "
        "total_ms=VALUES(total_ms), duration_hits=VALUES(duration_hits), b10=VALUES(b10), b50=VALUES(b50), b100=VALUES(b100), "
        "b500=VALUES(b500), b1000=VALUES(b1000), b3000=VALUES(b3000), bover=VALUES(bover), project_name=VALUES(project_name)" % table,
        values,
    )


def project_sources(cur, project_id):
    cur.execute(
        """
        SELECT p.id, p.name, p.namespace_name, p.deploy_mode, s.base_url, s.account_id, s.project_id
        FROM projects p
        JOIN project_sources ps ON ps.project_id=p.id
        JOIN log_sources s ON s.id=ps.source_id
        WHERE p.id=%s
        """,
        (int(project_id),),
    )
    return cur.fetchall()


def catch_up_project(cur, project, last_closed, horizon):
    app = _app()
    rows = project_sources(cur, project["id"])
    if not rows:
        return False
    name = ensure_project_table(cur, project["id"], project["name"], project.get("namespace_name") or "")
    cur.execute("DELETE FROM `%s` WHERE bucket_start < %%s" % name, (naive(datetime.now(app.SHANGHAI) - timedelta(days=90)),))
    namespaces = namespace_parts(project.get("namespace_name") or "")
    if not namespaces:
        return False
    raw_namespaces = [item for item in re.split(r"[\s,;]+", project.get("namespace_name") or "") if item.strip() and item.strip() != "on"]
    cur.execute(
        "SELECT bucket_start FROM `%s` WHERE uri_hash=%%s AND bucket_start >= %%s AND bucket_start <= %%s" % name,
        (DONE_HASH, horizon, last_closed),
    )
    have = {item["bucket_start"] for item in cur.fetchall()}
    cursor = horizon
    while cursor <= last_closed:
        if cursor not in have:
            aware_start = cursor.replace(tzinfo=app.SHANGHAI)
            aware_end = (cursor + timedelta(minutes=5)).replace(tzinfo=app.SHANGHAI)
            for namespace in raw_namespaces:
                found = collect_bucket(rows, rows[0].get("deploy_mode") or "kubernetes", namespace, aware_start, aware_end)
                save_bucket(cur, name, project["name"], namespace, cursor, found)
            mark_bucket(cur, name, project["name"], cursor)
            return True
        cursor += timedelta(minutes=5)
    return False


def catch_up_once():
    app = _app()
    now = datetime.now(app.SHANGHAI)
    closed = naive(floor_bucket(now) - timedelta(minutes=5))
    horizon = naive(floor_bucket(now - timedelta(hours=6)))
    worked = False
    with app.connect() as conn:
        with conn.cursor() as cur:
            ensure_registry(cur)
            cur.execute("SELECT id, name, namespace_name FROM projects ORDER BY id")
            for project in cur.fetchall():
                try:
                    if catch_up_project(cur, project, closed, horizon):
                        worked = True
                except Exception as exc:
                    print("汇总项目 %s 失败: %s" % (project.get("id"), exc))
    return worked


def _p95(duration_hits, buckets):
    if not duration_hits:
        return None
    edges = (10, 50, 100, 500, 1000, 3000, 10000)
    target = duration_hits * 0.95
    seen = 0
    previous = 0
    for edge, count in zip(edges, buckets):
        if seen + count >= target and count:
            return previous + (edge - previous) * ((target - seen) / count)
        seen += count
        previous = edge
    return edges[-1]


def _bounds(window, time_from, time_to):
    app = _app()
    now = datetime.now(app.SHANGHAI)
    if window == "5m":
        return now - timedelta(minutes=5), now
    if window == "15m":
        return now - timedelta(minutes=15), now
    if window == "1h":
        return now - timedelta(hours=1), now
    if window == "6h":
        return now - timedelta(hours=6), now
    return app.parse_local_time(time_from), app.parse_local_time(time_to)


def _load(cur, table, namespace, container, start, end):
    sql = (
        "SELECT namespace_name, container_name, http_method, uri, SUM(hits) AS hits, SUM(errors) AS errors, "
        "MIN(min_ms) AS min_ms, MAX(max_ms) AS max_ms, SUM(total_ms) AS total_ms, SUM(duration_hits) AS duration_hits, "
        "SUM(b10) AS b10, SUM(b50) AS b50, SUM(b100) AS b100, SUM(b500) AS b500, SUM(b1000) AS b1000, "
        "SUM(b3000) AS b3000, SUM(bover) AS bover FROM `%s` "
        "WHERE bucket_start >= %%s AND bucket_start < %%s AND container_name <> '' " % table
    )
    args = [naive(start), naive(end)]
    if namespace:
        sql += " AND namespace_name=%s"
        args.append(namespace)
    if container:
        sql += " AND container_name=%s"
        args.append(container)
    sql += " GROUP BY namespace_name, container_name, http_method, uri ORDER BY hits DESC LIMIT 1000"
    cur.execute(sql, args)
    rows = []
    for item in cur.fetchall():
        duration_hits = int(item.get("duration_hits") or 0)
        buckets = [int(item.get(bucket_name) or 0) for bucket_name in BUCKET_NAMES]
        total = float(item.get("total_ms") or 0)
        rows.append({
            "container_name": item["container_name"],
            "method": item.get("http_method") or "",
            "uri": item["uri"],
            "hits": int(item.get("hits") or 0),
            "errors": int(item.get("errors") or 0),
            "min_ms": item.get("min_ms"),
            "max_ms": item.get("max_ms"),
            "avg_ms": (total / duration_hits) if duration_hits else None,
            "p95_ms": _p95(duration_hits, buckets),
        })
    return rows


def read_stats(project_id, namespace, container, window, time_from, time_to):
    app = _app()
    start, end = _bounds(window, time_from, time_to)
    with app.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, namespace_name FROM projects WHERE id=%s", (int(project_id),))
            project = cur.fetchone()
            if not project:
                raise ValueError("项目不存在")
            name = ensure_project_table(cur, project["id"], project["name"], project.get("namespace_name") or "")
            cur.execute(
                "SELECT MIN(bucket_start) AS started, MAX(bucket_start) AS ended FROM `%s` WHERE uri_hash=%%s" % name,
                (DONE_HASH,),
            )
            span = cur.fetchone() or {}
            rows = _load(cur, name, namespace, container, start, end)
    started = span.get("started")
    ended = span.get("ended")
    notes = []
    if not started:
        notes.append("汇总表 %s 还没有数据，后台每 5 分钟写入一段。可先改成直接查日志" % name)
    else:
        if naive(start) < started:
            notes.append("汇总从 %s 开始，更早的时段还没有" % started.strftime("%m-%d %H:%M"))
        if ended and naive(end) > ended + timedelta(minutes=10):
            notes.append("汇总到 %s" % (ended + timedelta(minutes=5)).strftime("%m-%d %H:%M"))
    return {
        "rows": rows,
        "kind": "api" if rows else "empty",
        "source": "summary",
        "tableName": name,
        "projectName": project["name"],
        "namespaces": project.get("namespace_name") or "",
        "note": "。".join(notes),
        "limited": len(rows) >= 1000,
    }


def recent_attention(projects):
    app = _app()
    start, end = _bounds("1h", "", "")
    found = []
    ready = False
    with app.connect() as conn:
        with conn.cursor() as cur:
            for project in projects:
                cur.execute("SELECT table_name FROM stats_tables WHERE project_id=%s", (int(project["id"]),))
                saved = cur.fetchone()
                table = saved.get("table_name") if saved else ""
                if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", table or "") or not table_exists(cur, table):
                    continue
                cur.execute(
                    "SELECT 1 FROM `%s` WHERE uri_hash=%%s AND bucket_start >= %%s AND bucket_start < %%s LIMIT 1" % table,
                    (DONE_HASH, naive(start), naive(end)),
                )
                if not cur.fetchone():
                    continue
                ready = True
                limit = int(project.get("slow_ms") or 500)
                cur.execute(
                    "SELECT container_name, uri, SUM(hits) AS hits, SUM(errors) AS errors, MAX(max_ms) AS max_ms "
                    "FROM `%s` WHERE bucket_start >= %%s AND bucket_start < %%s AND container_name <> '' "
                    "GROUP BY namespace_name, container_name, uri "
                    "HAVING SUM(errors) > 0 "
                    "ORDER BY SUM(errors) DESC, MAX(max_ms) DESC LIMIT 20" % table,
                    (naive(start), naive(end)),
                )
                for item in cur.fetchall():
                    found.append({
                        "project_id": project["id"],
                        "project_name": project["name"],
                        "container_name": item.get("container_name") or "",
                        "uri": item.get("uri") or "",
                        "hits": int(item.get("hits") or 0),
                        "errors": int(item.get("errors") or 0),
                        "max_ms": item.get("max_ms"),
                        "slow_ms": limit,
                    })
    found.sort(key=lambda item: (item.get("errors") or 0, item.get("max_ms") or 0), reverse=True)
    return found[:8], ready


def catch_up_once():
    app = _app()
    now = datetime.now(app.SHANGHAI)
    closed = naive(floor_bucket(now) - timedelta(minutes=5))
    horizon = naive(floor_bucket(now - timedelta(hours=6)))
    worked = False
    with app.connect() as conn:
        with conn.cursor() as cur:
            ensure_registry(cur)
            cur.execute("SELECT id, name, namespace_name FROM projects ORDER BY id")
            for project in cur.fetchall():
                try:
                    if catch_up_project(cur, project, closed, horizon):
                        worked = True
                except Exception as exc:
                    print("汇总项目 %s 失败: %s" % (project.get("id"), exc))
    return worked


def catch_up_project(cur, project, last_closed, horizon):
    app = _app()
    rows = project_sources(cur, project["id"])
    if not rows:
        return False
    name = ensure_project_table(cur, project["id"], project["name"], project.get("namespace_name") or "")
    cur.execute("DELETE FROM `%s` WHERE bucket_start < %%s" % name, (naive(datetime.now(app.SHANGHAI) - timedelta(days=90)),))
    raw_namespaces = [item for item in re.split(r"[\s,;]+", project.get("namespace_name") or "") if item.strip() and item.strip() != "on"]
    if not raw_namespaces:
        return False
    cur.execute(
        "SELECT bucket_start FROM `%s` WHERE uri_hash=%%s AND bucket_start >= %%s AND bucket_start <= %%s" % name,
        (DONE_HASH, horizon, last_closed),
    )
    have = {item["bucket_start"] for item in cur.fetchall()}
    cursor = horizon
    while cursor <= last_closed:
        if cursor not in have:
            aware_start = cursor.replace(tzinfo=app.SHANGHAI)
            aware_end = (cursor + timedelta(minutes=5)).replace(tzinfo=app.SHANGHAI)
            for namespace in raw_namespaces:
                found = collect_bucket(rows, rows[0].get("deploy_mode") or "kubernetes", namespace, aware_start, aware_end)
                save_bucket(cur, name, project["name"], namespace, cursor, found)
            mark_bucket(cur, name, project["name"], cursor)
            return True
        cursor += timedelta(minutes=5)
    return False



def ensure_method_column(cur, table):
    cur.execute("SHOW COLUMNS FROM `%s` LIKE 'http_method'" % table)
    if cur.fetchone():
        return
    cur.execute("ALTER TABLE `%s` ADD COLUMN http_method VARCHAR(16) NOT NULL DEFAULT '' AFTER container_name" % table)
    cur.execute("ALTER TABLE `%s` DROP INDEX uniq_stat" % table)
    cur.execute(
        "ALTER TABLE `%s` ADD UNIQUE KEY uniq_stat (namespace_name(128), container_name, http_method, uri_hash, bucket_start)" % table
    )


def refill_method_bucket():
    app = _app()
    horizon = naive(datetime.now(app.SHANGHAI) - timedelta(hours=6))
    with app.connect() as conn:
        with conn.cursor() as cur:
            ensure_registry(cur)
            cur.execute("SELECT id, name, namespace_name FROM projects ORDER BY id")
            for project in cur.fetchall():
                name = ensure_project_table(cur, project["id"], project["name"], project.get("namespace_name") or "")
                cur.execute(
                    "SELECT bucket_start FROM `%s` WHERE http_method='' AND container_name<>'' AND uri<>'' AND bucket_start>=%%s "
                    "GROUP BY bucket_start ORDER BY bucket_start DESC LIMIT 1" % name,
                    (horizon,),
                )
                found = cur.fetchone()
                if not found:
                    continue
                bucket = found["bucket_start"]
                sources = project_sources(cur, project["id"])
                if not sources:
                    continue
                namespaces = [item for item in re.split(r"[\s,;]+", project.get("namespace_name") or "") if item.strip() and item.strip() != "on"]
                if not namespaces:
                    continue
                mode = sources[0].get("deploy_mode") or "kubernetes"
                aware_start = bucket.replace(tzinfo=app.SHANGHAI)
                aware_end = (bucket + timedelta(minutes=5)).replace(tzinfo=app.SHANGHAI)
                collected = []
                for namespace in namespaces:
                    collected.append((namespace, collect_bucket(sources, mode, namespace, aware_start, aware_end)))
                if not any(rows for _, rows in collected):
                    cur.execute(
                        "UPDATE `%s` SET http_method='-' WHERE bucket_start=%%s AND http_method='' AND container_name<>''" % name,
                        (bucket,),
                    )
                    return True
                cur.execute("DELETE FROM `%s` WHERE bucket_start=%%s AND container_name<>'' AND uri<>''" % name, (bucket,))
                for namespace, rows in collected:
                    if rows:
                        save_bucket(cur, name, project["name"], namespace, bucket, rows)
                cur.execute(
                    "UPDATE `%s` SET http_method='-' WHERE bucket_start=%%s AND http_method='' AND container_name<>''" % name,
                    (bucket,),
                )
                return True
    return False



def slow_interface_text(hours=24, limit=20, project_id=None):
    app = _app()
    end = datetime.now(app.SHANGHAI)
    start = end - timedelta(hours=hours)
    lines = ["慢接口报表 %s" % end.strftime("%Y-%m-%d %H:%M"), "最近 %s 小时，超过项目阈值的接口：" % hours]
    found = []
    with app.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, namespace_name, slow_ms FROM projects ORDER BY id")
            projects = cur.fetchall()
            for project in projects:
                cur.execute("SELECT table_name FROM stats_tables WHERE project_id=%s", (int(project["id"]),))
                saved = cur.fetchone()
                table = saved.get("table_name") if saved else ""
                if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", table or "") or not table_exists(cur, table):
                    continue
                threshold = int(project.get("slow_ms") or 500)
                for item in _load(cur, table, "", "", start, end):
                    slowest = item.get("p95_ms")
                    if slowest is None:
                        slowest = item.get("max_ms")
                    if slowest is None or float(slowest) < threshold:
                        continue
                    found.append((float(slowest), threshold, project["name"], item))
    found.sort(key=lambda row: row[0], reverse=True)
    if not found:
        lines.append("没有超过阈值的接口。")
        return "\n".join(lines)
    for _, threshold, project_name, item in found[:limit]:
        method = item.get("method") or "-"
        p95 = item.get("p95_ms")
        lines.append("%s  %s %s  %s  P95 %sms  最大 %sms  失败 %s  次数 %s  阈值 %sms" % (
            project_name,
            method,
            item.get("uri") or "",
            item.get("container_name") or "",
            int(p95) if p95 is not None else "-",
            int(item.get("max_ms") or 0) if item.get("max_ms") is not None else "-",
            int(item.get("errors") or 0),
            int(item.get("hits") or 0),
            threshold,
        ))
    if len(found) > limit:
        lines.append("其余 %s 条未列出。" % (len(found) - limit))
    return "\n".join(lines)


def _loop():
    time.sleep(8)
    while True:
        try:
            worked = catch_up_once()
            if refill_method_bucket():
                worked = True
            try:
                _app().maybe_send_slow_report()
            except Exception as exc:
                print("慢接口报表检查失败: %s" % exc)
        except Exception as exc:
            print("汇总任务失败: %s" % exc)
            worked = False
        time.sleep(5 if worked else 30)


def start():
    try:
        ensure_all()
    except Exception as exc:
        print("汇总表初始化失败: %s" % exc)
    threading.Thread(target=_loop, name="stats-summary", daemon=True).start()


def read_new_stats(project_id, namespace, container, window, time_from, time_to):
    app = _app()
    app.time_filter(window or "15m", time_from or "", time_to or "", 0)
    start, end = _bounds(window or "15m", time_from, time_to)
    start_n, end_n = naive(start), naive(end)
    with app.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, namespace_name, slow_ms FROM projects WHERE id=%s", (int(project_id),))
            project = cur.fetchone()
            if not project:
                raise ValueError("项目不存在")
            name = ensure_project_table(cur, project["id"], project["name"], project.get("namespace_name") or "")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name or ""):
                raise ValueError("汇总表名称不正确")
            cur.execute(
                "SELECT MIN(bucket_start) AS started, MAX(bucket_start) AS ended FROM `%s` WHERE uri_hash=%%s" % name,
                (DONE_HASH,),
            )
            span = cur.fetchone() or {}
            started, ended = span.get("started"), span.get("ended")
            rows = []
            if started and started < start_n:
                sql = (
                    "SELECT cur.namespace_name, cur.container_name, cur.uri, MIN(cur.bucket_start) AS first_seen, "
                    "SUM(cur.hits) AS hits, SUM(cur.errors) AS errors, MIN(cur.min_ms) AS min_ms, MAX(cur.max_ms) AS max_ms, "
                    "SUM(cur.total_ms) AS total_ms, SUM(cur.duration_hits) AS duration_hits, "
                    "SUM(cur.b10) AS b10, SUM(cur.b50) AS b50, SUM(cur.b100) AS b100, SUM(cur.b500) AS b500, "
                    "SUM(cur.b1000) AS b1000, SUM(cur.b3000) AS b3000, SUM(cur.bover) AS bover "
                    "FROM `%s` cur WHERE cur.bucket_start >= %%s AND cur.bucket_start < %%s "
                    "AND cur.container_name <> '' AND cur.uri <> '' AND cur.uri_hash <> %%s "
                    "AND NOT EXISTS (SELECT 1 FROM `%s` prev WHERE prev.namespace_name=cur.namespace_name "
                    "AND prev.container_name=cur.container_name AND prev.uri_hash=cur.uri_hash AND prev.bucket_start < %%s)"
                ) % (name, name)
                args = [start_n, end_n, DONE_HASH, start_n]
                names = [item.strip() for item in re.split(r"[\s,;]+", namespace or "") if item.strip() and item.strip() != "on"]
                if names:
                    sql += " AND cur.namespace_name IN (" + ",".join(["%s"] * len(names)) + ")"
                    args.extend(names)
                if container:
                    sql += " AND cur.container_name=%s"
                    args.append(container)
                sql += " GROUP BY cur.namespace_name, cur.container_name, cur.uri ORDER BY first_seen DESC LIMIT 1000"
                cur.execute(sql, args)
                for item in cur.fetchall():
                    duration_hits = int(item.get("duration_hits") or 0)
                    buckets = [int(item.get(bucket_name) or 0) for bucket_name in BUCKET_NAMES]
                    total = float(item.get("total_ms") or 0)
                    first = item.get("first_seen")
                    rows.append({
                        "namespace_name": item.get("namespace_name") or "",
                        "container_name": item.get("container_name") or "",
                        "uri": item.get("uri") or "",
                        "first_seen": first.strftime("%Y-%m-%d %H:%M") if first else "",
                        "hits": int(item.get("hits") or 0),
                        "errors": int(item.get("errors") or 0),
                        "min_ms": item.get("min_ms"),
                        "max_ms": item.get("max_ms"),
                        "avg_ms": (total / duration_hits) if duration_hits else None,
                        "p95_ms": _p95(duration_hits, buckets),
                    })
    notes = []
    if not started:
        notes.append("汇总还没有数据，暂时不能判断新增接口")
    elif started >= start_n:
        notes.append("汇总从 %s 才开始，更早没有记录，不能判断是不是新增" % started.strftime("%m-%d %H:%M"))
    else:
        notes.append("只统计这段时间里第一次出现的接口，汇总从 %s 开始" % started.strftime("%m-%d %H:%M"))
        if ended and end_n > ended + timedelta(minutes=10):
            notes.append("汇总到 %s" % (ended + timedelta(minutes=5)).strftime("%m-%d %H:%M"))
    return {
        "rows": rows,
        "kind": "new",
        "source": "summary",
        "slowMs": int(project.get("slow_ms") or 500),
        "note": "。".join(notes),
        "limited": len(rows) >= 1000,
        "newOnly": True,
    }
