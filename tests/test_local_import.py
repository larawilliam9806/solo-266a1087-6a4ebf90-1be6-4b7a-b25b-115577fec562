import io
import os
import sqlite3
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app import db as db_module  # noqa: E402
from app import local_import  # noqa: E402


NY = "America/New_York"
SH = "Asia/Shanghai"


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "t"}
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, kind, csv_text, filename="data.csv"):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    return client.post(
        f"/upload/{kind}", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def local_upload(client, point, tz, csv_text, filename="local.csv"):
    data = {
        "point": point,
        "timezone": tz,
        "file": (io.BytesIO(csv_text.encode("utf-8")), filename),
    }
    return client.post(
        "/upload/local", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def usage(client, point, start, end):
    resp = client.get(f"/api/usage?point={point}&start={start}&end={end}")
    assert resp.status_code == 200
    return resp.get_json()


def raw_tables(app, point):
    conn = sqlite3.connect(app.config["DATABASE"])
    readings = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? ORDER BY ts", (point,)
    ).fetchall()
    swaps = [
        r[0]
        for r in conn.execute(
            "SELECT ts FROM swaps WHERE point = ? ORDER BY ts", (point,)
        ).fetchall()
    ]
    conn.close()
    return readings, swaps


def last_upload(app):
    conn = sqlite3.connect(app.config["DATABASE"])
    row = conn.execute(
        "SELECT kind, status, message, rows_received, rows_applied"
        " FROM uploads ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------- 页面与基础映射

def test_page_renders(client):
    resp = client.get("/import/local")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "IANA 时区" in body and "local_ts,type,value" in body
    assert NY in body and SH in body


def test_basic_local_mapping_and_usage(client):
    # 上海 UTC+8，无 DST：当地 08:00 -> 00:00Z
    csv_text = (
        "local_ts,type,value\n"
        "2026-01-01 00:00:00,reading,100\n"
        "2026-01-01 08:00:00,reading,160\n"
        "2026-01-01 20:00:00,reading,200\n"
    )
    resp = local_upload(client, "MP1", SH, csv_text)
    assert "导入成功" in resp.get_data(as_text=True)
    readings, swaps = raw_tables(client.application, "MP1")
    assert readings == [
        ("2025-12-31T16:00:00.000000Z", "100"),
        ("2026-01-01T00:00:00.000000Z", "160"),
        ("2026-01-01T12:00:00.000000Z", "200"),
    ]
    assert swaps == []
    r = usage(client, "MP1", "2026-01-01", "2026-01-01")
    assert r["total_kwh"] == "100"  # 60 + 40，增量按后次读数 UTC 日归属，原口径不变


def test_header_case_insensitive_and_swap_keywords(client):
    csv_text = (
        "LOCAL_TS,TYPE,VALUE\n"
        "2026-01-01 10:00:00,读数,10\n"
        "2026-01-01 11:00:00,换表,\n"
        "2026-01-01 12:00:00,R,12\n"
    )
    resp = local_upload(client, "MP1", SH, csv_text)
    assert "导入成功" in resp.get_data(as_text=True)
    readings, swaps = raw_tables(client.application, "MP1")
    assert len(readings) == 2 and len(swaps) == 1


# ---------------------------------------------------------------- 静态校验

def test_unknown_timezone_rejected(client):
    resp = local_upload(
        client, "MP1", "Mars/Olympus",
        "local_ts,type,value\n2026-01-01 10:00:00,reading,1\n",
    )
    assert "未知或不可用的 IANA 时区" in resp.get_data(as_text=True)
    readings, _ = raw_tables(client.application, "MP1")
    assert readings == []  # 参数级错误，不产生任何明细


def test_missing_point_and_file(client):
    data = {
        "point": "",
        "timezone": SH,
        "file": (io.BytesIO(b"local_ts,type,value\n"), "x.csv"),
    }
    resp = client.post("/upload/local", data=data,
                       content_type="multipart/form-data", follow_redirects=True)
    assert "计量点" in resp.get_data(as_text=True)


def test_nonexistent_local_time_spring_gap(client):
    # 2026-03-08 02:30 纽约处于春跳空档（02:00 -> 03:00）
    csv_text = (
        "local_ts,type,value\n"
        "2026-03-08 00:00:00,reading,10\n"
        "2026-03-08 02:30:00,reading,20\n"
        "2026-03-08 03:30:00,reading,30\n"
    )
    resp = local_upload(client, "MP1", NY, csv_text)
    body = resp.get_data(as_text=True)
    assert "不存在" in body and "第 3 行" in body
    readings, _ = raw_tables(client.application, "MP1")
    assert readings == []  # 整批拒绝，无任何落库


def test_explicit_offset_rejected(client):
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n2026-01-01T10:00:00+08:00,reading,1\n",
    )
    assert "不能带时区" in resp.get_data(as_text=True)


def test_two_local_days_rejected(client):
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n"
        "2026-01-01 23:00:00,reading,10\n"
        "2026-01-02 01:00:00,reading,12\n",
    )
    assert "同一当地日" in resp.get_data(as_text=True)


def test_local_order_must_be_nondecreasing(client):
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n"
        "2026-01-01 10:00:00,reading,10\n"
        "2026-01-01 09:00:00,reading,12\n",
    )
    assert "不递减" in resp.get_data(as_text=True)


def test_swap_row_must_have_blank_value(client):
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n2026-01-01 10:00:00,swap,5\n",
    )
    assert "换表行的读数必须留空" in resp.get_data(as_text=True)


def test_unknown_record_type(client):
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n2026-01-01 10:00:00,reset,5\n",
    )
    assert "未知记录类型" in resp.get_data(as_text=True)


def test_row_limit_120(client):
    # 121 行超限拒绝
    lines = ["local_ts,type,value"]
    for i in range(121):
        h, m = divmod(i, 60)
        lines.append(f"2026-01-01 {h:02d}:{m:02d}:00,reading,{i}")
    resp = local_upload(client, "MP1", SH, "\n".join(lines) + "\n")
    assert "最多 120 行" in resp.get_data(as_text=True)

    lines = ["local_ts,type,value"]
    for i in range(120):
        h, m = divmod(i, 60)
        lines.append(f"2026-01-01 {h:02d}:{m:02d}:00,reading,{i}")
    resp = local_upload(client, "MP1", SH, "\n".join(lines) + "\n")
    assert "导入成功" in resp.get_data(as_text=True)
    readings, _ = raw_tables(client.application, "MP1")
    assert len(readings) == 120


# ---------------------------------------------------------------- 夏令时回拨消歧

def test_fallback_ambiguous_multiple_solutions_rejected(client):
    # 01:00 与 01:30 都是回拨重复时刻；单调读数下两种折叠都成立 -> 多解
    csv_text = (
        "local_ts,type,value\n"
        "2026-11-01 00:00:00,reading,5\n"
        "2026-11-01 01:00:00,reading,10\n"
        "2026-11-01 01:30:00,reading,20\n"
        "2026-11-01 02:30:00,reading,30\n"
    )
    resp = local_upload(client, "MP1", NY, csv_text)
    body = resp.get_data(as_text=True)
    assert "多种 UTC 解释" in body and "整批拒绝" in body
    assert "第 3 行" in body or "第 4 行" in body  # 指出相关 CSV 行
    readings, _ = raw_tables(client.application, "MP1")
    assert readings == []


def test_fallback_unique_resolved_against_committed_detail(app, client):
    # 库中已提交：06:00Z 读数 15（纽约当地 01:00 的 fold=1 时刻）
    upload(
        client, "readings",
        "point,ts,value\nMP1,2026-11-01T06:00:00Z,15\n",
    )
    csv_text = (
        "local_ts,type,value\n"
        "2026-11-01 01:00:00,reading,10\n"   # fold1=06:00Z 与库内 15 同刻异值 -> 只能 fold0=05:00Z
        "2026-11-01 01:30:00,reading,20\n"   # fold0=05:30Z 相对库内 06:00=15 倒退 -> 只能 06:30Z
    )
    resp = local_upload(client, "MP1", NY, csv_text)
    assert "导入成功" in resp.get_data(as_text=True)
    readings, _ = raw_tables(app, "MP1")
    assert readings == [
        ("2026-11-01T05:00:00.000000Z", "10"),
        ("2026-11-01T06:00:00.000000Z", "15"),
        ("2026-11-01T06:30:00.000000Z", "20"),
    ]


def test_fallback_no_solution_rejected(app, client):
    # 库内 06:00Z=5：fold0 05:00Z 会造成 (05,06] 倒退 10->5；fold1 06:00Z 同刻异值
    upload(
        client, "readings",
        "point,ts,value\nMP1,2026-11-01T06:00:00Z,5\n",
    )
    csv_text = (
        "local_ts,type,value\n2026-11-01 01:00:00,reading,10\n"
    )
    resp = local_upload(client, "MP1", NY, csv_text)
    body = resp.get_data(as_text=True)
    assert "所有 UTC 解释均不成立" in body and "第 2 行" in body
    readings, _ = raw_tables(app, "MP1")
    assert readings == [("2026-11-01T06:00:00.000000Z", "5")]  # 旧数据不动


def test_two_readings_same_ambiguous_local_time_is_multi_solution(client):
    # 同一回拨时刻两行读数：两种折叠分配都成立 -> 多解拒绝
    csv_text = (
        "local_ts,type,value\n"
        "2026-11-01 01:30:00,reading,10\n"
        "2026-11-01 01:30:00,reading,20\n"
    )
    resp = local_upload(client, "MP1", NY, csv_text)
    body = resp.get_data(as_text=True)
    assert "多种 UTC 解释" in body and "整批拒绝" in body
    readings, _ = raw_tables(client.application, "MP1")
    assert readings == []


def test_two_swaps_same_ambiguous_local_time_is_multi_solution(client):
    csv_text = (
        "local_ts,type,value\n"
        "2026-11-01 01:30:00,swap,\n"
        "2026-11-01 01:30:00,swap,\n"
    )
    resp = local_upload(client, "MP1", NY, csv_text)
    assert "多种 UTC 解释" in resp.get_data(as_text=True)


def test_two_readings_same_unambiguous_local_time_no_solution(app, client):
    # 无 DST 时区：同一当地时刻只有一个 UTC，两条读数无法同刻共存 -> 无解
    csv_text = (
        "local_ts,type,value\n"
        "2026-01-01 10:00:00,reading,10\n"
        "2026-01-01 10:00:00,reading,20\n"
    )
    resp = local_upload(client, "MP1", SH, csv_text)
    body = resp.get_data(as_text=True)
    assert "所有 UTC 解释均不成立" in body and "最多只能有一条读数" in body
    readings, _ = raw_tables(app, "MP1")
    assert readings == []


def test_reading_and_swap_same_local_time_one_of_each(app, client):
    # 库内 04:00Z 读数 100，以及 05:30Z 读数 999（与本次 12 同刻异值）：
    # 文件 01:30 的读数不能落在 05:30Z，整组解释被唯一压到 fold=1（06:30Z）；
    # 读数与换表同刻各一条，允许。
    upload(
        client, "readings",
        "point,ts,value\n"
        "MP1,2026-11-01T04:00:00Z,100\n"
        "MP1,2026-11-01T05:30:00Z,999\n",
    )
    csv_text = (
        "local_ts,type,value\n"
        "2026-11-01 01:30:00,swap,\n"
        "2026-11-01 01:30:00,reading,12\n"
        "2026-11-01 02:30:00,reading,30\n"
    )
    resp = local_upload(client, "MP1", NY, csv_text)
    assert "导入成功" in resp.get_data(as_text=True), resp.get_data(as_text=True)
    readings, swaps = raw_tables(app, "MP1")
    assert ("2026-11-01T05:30:00.000000Z", "999") in readings  # 库内明细保持
    assert ("2026-11-01T06:30:00.000000Z", "12") in readings
    assert ("2026-11-01T07:30:00.000000Z", "30") in readings
    assert "2026-11-01T06:30:00.000000Z" in swaps
    # (05:30=999, 06:30=12] 内含换表 -> 缺口；(06:30, 07:30] 正常计量 18；
    # 当日合计 = (100->999)=899 + 18（缺口区间不计）
    r = usage(client, "MP1", "2026-11-01", "2026-11-01")
    assert r["total_kwh"] == "917"
    assert len(r["gaps"]) == 1
    assert r["gaps"][0] == {
        "start": "2026-11-01T05:30:00.000000Z",
        "end": "2026-11-01T06:30:00.000000Z",
    }


# ---------------------------------------------------------------- 与库内明细合并

def test_conflict_same_ts_different_value_rejected_atomically(app, client):
    upload(
        client, "readings",
        "point,ts,value\nMP1,2026-01-01T02:00:00Z,100\n",
    )
    version_before = usage(client, "MP1", "2026-01-01", "2026-01-01")["version"]
    # 上海当地 10:00 == 02:00Z，同刻异值
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n"
        "2026-01-01 10:00:00,reading,999\n"
        "2026-01-01 11:00:00,reading,1000\n",
    )
    assert "同刻异值" in resp.get_data(as_text=True)
    readings, swaps = raw_tables(app, "MP1")
    assert readings == [("2026-01-01T02:00:00.000000Z", "100")]  # 第二行也未生效
    version_after = usage(client, "MP1", "2026-01-01", "2026-01-01")["version"]
    assert version_before == version_after  # 数据版本不变


def test_backwards_without_swap_rejected_with_swap_accepted(app, client):
    upload(
        client, "readings",
        "point,ts,value\nMP1,2026-01-01T02:00:00Z,100\n",
    )
    # 当地 11:00 -> 03:00Z，读数 90 < 100 且无换表 -> 拒绝
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n2026-01-01 11:00:00,reading,90\n",
    )
    assert "倒退" in resp.get_data(as_text=True)

    # 同刻前补换表 -> 区间变缺口，允许换表后的小读数
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n"
        "2026-01-01 11:00:00,swap,\n"
        "2026-01-01 11:00:00,reading,90\n"
        "2026-01-01 12:00:00,reading,95\n",
    )
    assert "导入成功" in resp.get_data(as_text=True), resp.get_data(as_text=True)
    r = usage(client, "MP1", "2026-01-01", "2026-01-01")
    assert r["total_kwh"] == "5"  # 缺口区间不计，只有 95-90=5
    assert len(r["gaps"]) == 1


def test_idempotent_reupload(app, client):
    csv_text = (
        "local_ts,type,value\n"
        "2026-01-01 10:00:00,reading,10\n"
        "2026-01-01 11:00:00,swap,\n"
        "2026-01-01 11:00:00,reading,12\n"
    )
    local_upload(client, "MP1", SH, csv_text)
    resp = local_upload(client, "MP1", SH, csv_text)
    body = resp.get_data(as_text=True)
    assert "导入成功" in body and "重复跳过 3 条" in body
    readings, swaps = raw_tables(app, "MP1")
    assert len(readings) == 2 and len(swaps) == 1


def test_version_advances_only_on_applied(app, client):
    # 一次被拒：applied 版本号仍为 0
    local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n2026-01-01 10:00:00,reading,-1\n",
    )
    conn = sqlite3.connect(app.config["DATABASE"])
    v_rej = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM uploads WHERE status='applied'"
    ).fetchone()[0]
    conn.close()
    assert v_rej == 0
    # 一次生效 -> 版本号前进
    local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n2026-01-01 10:00:00,reading,10\n",
    )
    r = usage(client, "MP1", "2026-01-01", "2026-01-01")
    assert r["version"] >= 1
    kind = last_upload(app)
    assert kind[0] == "local" and kind[1] == "applied"


# ---------------------------------------------------------------- 有界读取

def test_load_bounded_detail_reads_only_needed_window():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_module.SCHEMA)
    conn.execute(
        "INSERT INTO uploads(kind,filename,status,message,rows_received,"
        "rows_applied,created_at) VALUES('readings','x','applied','',0,0,'t')"
    )
    uid = conn.execute("SELECT MAX(id) FROM uploads").fetchone()[0]
    seeds = [
        ("2026-01-01T00:00:00.000000Z", "1"),   # 窗外更早（只有最近一条作 seed）
        ("2026-11-01T04:00:00.000000Z", "100"),  # seed：窗内左界之前最近读数
        ("2026-11-01T05:30:00.000000Z", "110"),  # 窗内
        ("2026-11-02T00:00:00.000000Z", "200"),  # right：窗外更晚第一条
        ("2026-11-03T00:00:00.000000Z", "300"),  # 更远，不应读取
    ]
    for ts, val in seeds:
        conn.execute(
            "INSERT INTO readings(point,ts,value,upload_id) VALUES('P',?,?,?)",
            (ts, val, uid),
        )
    for ts in (
        "2026-11-01T03:00:00.000000Z",  # 早于 seed，不应读取
        "2026-11-01T05:00:00.000000Z",  # (seed, right] 内，应读取
        "2026-11-02T01:00:00.000000Z",  # 晚于 right，不应读取
    ):
        conn.execute(
            "INSERT INTO swaps(point,ts,upload_id) VALUES('P',?,?)", (ts, uid)
        )

    events, seed = local_import.load_bounded_detail(
        conn, "P",
        "2026-11-01T05:00:00.000000Z",
        "2026-11-01T07:00:00.000000Z",
    )
    ts_kinds = [(ts, kind) for ts, kind, _ in events]
    assert ("2026-11-01T05:00:00.000000Z", "S") in ts_kinds
    assert ("2026-11-01T05:30:00.000000Z", "R") in ts_kinds
    assert ("2026-11-02T00:00:00.000000Z", "R") in ts_kinds  # 右端相邻读数
    assert all(ts != "2026-11-01T03:00:00.000000Z" for ts, _ in ts_kinds)
    assert all(ts != "2026-11-02T01:00:00.000000Z" for ts, _ in ts_kinds)
    assert all(ts != "2026-11-03T00:00:00.000000Z" for ts, _ in ts_kinds)
    assert seed == ("2026-11-01T04:00:00.000000Z", "100")
    conn.close()


def test_far_away_detail_does_not_disturb_import(app, client):
    # 大量窗外历史/未来数据：消歧与入库只依赖窗口+边界，结果不受影响
    old = "".join(
        f"MP1,2025-01-{d:02d}T00:00:00Z,{d}\n" for d in range(1, 20)
    )
    future = "".join(
        f"MP1,2027-01-{d:02d}T00:00:00Z,{1000 + d}\n" for d in range(1, 20)
    )
    upload(client, "readings", "point,ts,value\n" + old + future)
    resp = local_upload(
        client, "MP1", SH,
        "local_ts,type,value\n"
        "2026-01-01 10:00:00,reading,500\n"
        "2026-01-01 12:00:00,reading,530\n",
    )
    assert "导入成功" in resp.get_data(as_text=True)
    # 当日只统计终点落在 2026-01-01 的区间增量：(500,530]=30；
    # 窗外历史读数 -> 500 的大区间终点虽在当日，但可通过窗口/边界正确识别
    r = usage(client, "MP1", "2026-01-01", "2026-01-01")
    day = next(d for d in r["days"] if d["day"] == "2026-01-01")
    assert day["kwh"] == "511"  # (19->500)=481 + (500->530)=30，无缺口
    readings, _ = raw_tables(app, "MP1")
    assert ("2026-01-01T02:00:00.000000Z", "500") in readings
    assert ("2026-01-01T04:00:00.000000Z", "530") in readings


# ---------------------------------------------------------------- 候选生成单元测试

def test_local_candidates():
    ny = local_import.get_zone(NY)
    # 春跳空档
    assert local_import.local_candidates(datetime(2026, 3, 8, 2, 30), ny) == []
    # 回拨重复
    cands = local_import.local_candidates(datetime(2026, 11, 1, 1, 30), ny)
    assert cands == [
        "2026-11-01T05:30:00.000000Z",
        "2026-11-01T06:30:00.000000Z",
    ]
    # 普通时刻
    assert local_import.local_candidates(
        datetime(2026, 11, 1, 10, 0), ny
    ) == ["2026-11-01T15:00:00.000000Z"]
    # 无 DST 时区
    assert local_import.local_candidates(
        datetime(2026, 1, 1, 8, 0), local_import.get_zone(SH)
    ) == ["2026-01-01T00:00:00.000000Z"]
