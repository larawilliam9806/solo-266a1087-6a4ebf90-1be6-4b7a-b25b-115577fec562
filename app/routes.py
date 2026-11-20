import sqlite3
from datetime import timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import logic, local_import
from .db import get_db

bp = Blueprint("main", __name__)

KIND_NAMES = {"readings": "读数", "swaps": "换表", "local": "本地时刻混合导入"}


def data_version(conn):
    """数据版本号 = 最近一个生效批次 id。同一事务内读取，保证版本与内容一致。"""
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS v FROM uploads WHERE status = 'applied'"
    ).fetchone()["v"]


def list_points(conn):
    return [
        r["point"]
        for r in conn.execute(
            "SELECT point FROM ("
            "  SELECT point FROM readings UNION SELECT point FROM swaps"
            ") ORDER BY point"
        )
    ]


def record_rejected(conn, kind, filename, message, rows_received):
    """被拒批次单独落一条审计记录（不影响既有数据）；审计失败不掩盖原始错误。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, filename, "rejected", message, rows_received, logic.now_ts()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")


@bp.route("/")
def index():
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：页面看到的是同一版快照
    try:
        points = list_points(conn)
        uploads = conn.execute(
            "SELECT * FROM uploads ORDER BY id DESC LIMIT 50"
        ).fetchall()
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "index.html",
        points=points,
        uploads=uploads,
        version=version,
        kind_names=KIND_NAMES,
    )


@bp.route("/upload/<kind>", methods=["POST"])
def upload(kind):
    if kind not in KIND_NAMES:
        abort(404)
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.index"))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("上传被拒绝：文件编码须为 UTF-8", "error")
        return redirect(url_for("main.index"))

    conn = get_db()
    # 1) 解析与格式校验（不进事务）
    try:
        if kind == "readings":
            parsed, raw_count = logic.parse_readings_csv(text)
        else:
            parsed, raw_count = logic.parse_swaps_csv(text)
    except logic.CsvError as e:
        record_rejected(conn, kind, f.filename, str(e), 0)
        flash(f"上传被拒绝：{e}", "error")
        return redirect(url_for("main.index"))

    # 2) 单个写事务：原始记录与受影响结果一起生效；失败整体回滚，保持旧数据
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, f.filename, "applied", "", raw_count, logic.now_ts()),
        )
        upload_id = cur.lastrowid
        if kind == "readings":
            applied, skipped = logic.apply_readings(conn, upload_id, parsed)
        else:
            applied, skipped = logic.apply_swaps(conn, upload_id, parsed)
        msg = f"接收 {raw_count} 行，新增 {applied} 条，重复跳过 {skipped} 条"
        conn.execute(
            "UPDATE uploads SET rows_applied = ?, message = ? WHERE id = ?",
            (applied, msg, upload_id),
        )
        conn.execute("COMMIT")
        flash(f"{KIND_NAMES[kind]}上传成功：{msg}", "success")
    except logic.BatchRejected as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, kind, f.filename, str(e), raw_count)
        flash(f"上传被拒绝：{e}", "error")
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
    return redirect(url_for("main.index"))


@bp.route("/import/local")
def local_import_page():
    """本地时刻 CSV 导入页：选择计量点与 IANA 时区后上传。"""
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：页面看到的是同一版快照
    try:
        points = list_points(conn)
        version = data_version(conn)
        common_tz, all_tz = local_import.list_timezones()
    finally:
        conn.execute("COMMIT")
    return render_template(
        "local_import.html",
        points=points,
        version=version,
        common_tz=common_tz,
        all_tz=all_tz,
        max_rows=local_import.MAX_LOCAL_ROWS,
    )


@bp.route("/upload/local", methods=["POST"])
def upload_local():
    """本地时刻混合 CSV：读数与换表在同一文件、同一事务内消歧并生效。"""
    point = (request.form.get("point") or "").strip()
    tz_name = (request.form.get("timezone") or "").strip()
    f = request.files.get("file")

    if not point:
        flash("上传被拒绝：请选择或填写计量点", "error")
        return redirect(url_for("main.local_import_page"))
    if len(point) > logic.MAX_POINT_LEN:
        flash(
            f"上传被拒绝：计量点名称过长（>{logic.MAX_POINT_LEN} 字符）", "error"
        )
        return redirect(url_for("main.local_import_page"))
    if f is None or not f.filename:
        flash("上传被拒绝：请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.local_import_page"))
    try:
        tz = local_import.get_zone(tz_name)
    except logic.CsvError as e:
        flash(f"上传被拒绝：{e}", "error")
        return redirect(url_for("main.local_import_page"))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("上传被拒绝：文件编码须为 UTF-8", "error")
        return redirect(url_for("main.local_import_page"))

    conn = get_db()
    # 1) 解析、同当地日/顺序/行数等静态校验，以及 UTC 候选生成（不进写事务）
    try:
        rows, raw_count = local_import.parse_local_csv(text)
        local_import.attach_candidates(rows, tz)
    except logic.CsvError as e:
        record_rejected(conn, "local", f.filename, str(e), 0)
        flash(f"上传被拒绝：{e}", "error")
        return redirect(url_for("main.local_import_page"))

    # 2) 单个写事务：核对（消歧）与入库以同一份已提交明细为准，
    #    读数与换表同生共死；任何失败整体回滚，旧数据与数据版本保持不变
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            ("local", f.filename, "applied", "", raw_count, logic.now_ts()),
        )
        upload_id = cur.lastrowid
        # BatchRejected 由外层统一回滚并写审计
        chosen_ts = local_import.resolve_unique(conn, point, rows)
        applied, skipped = local_import.apply_local_batch(
            conn, upload_id, point, rows, chosen_ts
        )
        n_read = sum(1 for r in rows if r["kind"] == "R")
        n_swap = raw_count - n_read
        msg = (
            f"接收 {raw_count} 行（读数 {n_read}、换表 {n_swap}），"
            f"新增 {applied} 条，重复跳过 {skipped} 条；时区 {tz.key}"
        )
        conn.execute(
            "UPDATE uploads SET rows_applied = ?, message = ? WHERE id = ?",
            (applied, msg, upload_id),
        )
        conn.execute("COMMIT")
        flash(f"本地时刻导入成功：{msg}", "success")
    except logic.BatchRejected as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, "local", f.filename, str(e), raw_count)
        flash(f"上传被拒绝：{e}", "error")
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
    return redirect(url_for("main.local_import_page"))


def _resolve_range(conn, point, start, end):
    """校验/补全日期范围，返回 (start_day, end_day)。缺省时取该点数据最近 31 天。"""
    if start and end:
        d0, d1 = logic.parse_day(start), logic.parse_day(end)
    else:
        row = conn.execute(
            "SELECT MIN(substr(ts,1,10)) AS lo, MAX(substr(ts,1,10)) AS hi"
            " FROM readings WHERE point = ?",
            (point,),
        ).fetchone()
        if row is None or row["lo"] is None:
            raise ValueError("该计量点暂无读数，请指定日期范围")
        d1 = logic.parse_day(end) if end else logic.parse_day(row["hi"])
        d0 = logic.parse_day(start) if start else logic.parse_day(row["lo"])
        if (d1 - d0).days > 30:
            d0 = d1 - timedelta(days=30)
    if d1 < d0:
        raise ValueError("结束日期早于开始日期")
    if (d1 - d0).days > logic.MAX_RANGE_DAYS:
        raise ValueError(f"日期范围过大（最多 {logic.MAX_RANGE_DAYS} 天）")
    return d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")


@bp.route("/usage")
def usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：同一版完整结果
    try:
        points = list_points(conn)
        version = data_version(conn)
        days = gaps = total = None
        error = None
        if point:
            try:
                start, end = _resolve_range(conn, point, start, end)
                days, gaps, total = logic.usage_range(conn, point, start, end)
            except ValueError as e:
                error = str(e)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "usage.html",
        points=points,
        point=point,
        start=start,
        end=end,
        days=days,
        gaps=gaps,
        total=total,
        error=error,
        version=version,
    )


@bp.route("/api/usage")
def api_usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if not (point and start and end):
        return jsonify(error="需要 point、start、end 参数（日期为 YYYY-MM-DD）"), 400
    conn = get_db()
    conn.execute("BEGIN")
    try:
        try:
            start, end = _resolve_range(conn, point, start, end)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        days, gaps, total = logic.usage_range(conn, point, start, end)
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return jsonify(
        point=point,
        start=start,
        end=end,
        version=version,
        total_kwh=logic.fmt_num(total),
        days=[
            {"day": d["day"], "kwh": logic.fmt_num(d["kwh"]), "gap": d["gap"]}
            for d in days
        ],
        gaps=[{"start": g["start"], "end": g["end"]} for g in gaps],
    )
