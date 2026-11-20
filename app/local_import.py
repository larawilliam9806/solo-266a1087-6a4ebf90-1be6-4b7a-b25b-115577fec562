"""本地时刻 CSV 导入（IANA 时区 + 夏令时消歧）。

有些电表导出的文件只记录当地墙钟时间（wall time），不带 UTC 偏移。
本模块负责把同一当地日内、按设备记录顺序排列的读数/换表行，借助计量点所在
IANA 时区解释成 UTC 时刻，并在 *夏令时回拨* 造成的一钟两刻之间消歧。

解释必须同时满足：

1. 解释出的 UTC 时刻按 CSV 行序不递减；
2. 同一 UTC 时刻最多一条读数、一条换表；
3. 与库中已提交的读数/换表合并后，不允许“同刻异值”，也不允许无换表的读数倒退
   （区间 (t1, t2] 内有换表 s 满足 t1 < s <= t2 时为缺口，不校验增量）。

回拨时同一个当地时刻可对应两个 UTC 时刻，因此可行解释可能不止一种。
本模块用记忆化动态规划枚举可行解释（计数到 2 即止）：仅当可行解释 *恰好一种*
时才允许导入；无解或多解都给出相关 CSV 行号并由调用方整批拒绝。

核对只读取该点候选时刻范围内的明细，加上判断两端相邻区间所需的边界读数与换表；
核对与入库在调用方开启的同一个 ``BEGIN IMMEDIATE`` 事务内完成，以同一份已提交
明细为准。
"""

import bisect
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .logic import (
    BatchRejected,
    CsvError,
    canon_ts,
    find_negative_deltas,
    parse_value,
    value_str,
)

MAX_LOCAL_ROWS = 120
LOCAL_REQUIRED = ["local_ts", "type", "value"]

# 规范化 UTC 时刻的字典序上界（定宽 ASCII 格式不可能超过它）
TS_HIGH = "9999-12-31T23:59:59.999999Z"

READING_TYPES = {"reading", "readings", "r", "读数", "抄表"}
SWAP_TYPES = {"swap", "s", "换表"}

# 页面下拉框优先展示的常用时区
COMMON_TIMEZONES = [
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Asia/Dubai",
    "Europe/London",
    "Europe/Berlin",
    "Australia/Sydney",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "UTC",
]


def get_zone(name):
    """按 IANA 名称加载时区，非法名称抛 CsvError。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise CsvError(f"未知或不可用的 IANA 时区：{name!r}")


def list_timezones():
    """返回 (常用时区列表, 全部时区排序列表)。"""
    from zoneinfo import available_timezones

    all_tz = sorted(available_timezones())
    common = [tz for tz in COMMON_TIMEZONES if tz in set(all_tz)]
    return common, all_tz


def fmt_lines(numbers):
    return "、".join(f"第 {n} 行" for n in numbers)


# ---------------------------------------------------------------- CSV 解析

def parse_local_dt(raw, where):
    """解析 *不带时区* 的当地时刻；显式带偏移/ Z 的一律拒绝。"""
    s = (raw or "").strip()
    if not s:
        raise CsvError(f"{where}本地时刻为空")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise CsvError(
            f"{where}无法解析的本地时刻 {s!r}（如 2026-11-01 01:30:00）"
        )
    if dt.tzinfo is not None:
        raise CsvError(
            f"{where}本地时刻 {s!r} 不能带时区偏移或 Z（本文件只记录当地时刻）"
        )
    return dt


def parse_local_csv(text):
    """解析本地时刻 CSV。

    表头：``local_ts,type,value``。返回 (rows, raw_count)，row 形如：
    ``{'lineno', 'kind'('R'|'S'), 'local'(naive datetime), 'value'(Decimal|None)}``。
    """
    from .logic import _iter_rows  # 复用既有 CSV 骨架（空行跳过、表头兼容）

    rows = []
    raw_count = 0
    local_day = None
    prev_local = None
    prev_lineno = None
    for lineno, cells in _iter_rows(text, LOCAL_REQUIRED):
        raw_count += 1
        if raw_count > MAX_LOCAL_ROWS:
            raise CsvError(
                f"第 {lineno} 行：单个文件最多 {MAX_LOCAL_ROWS} 行数据，整批拒绝"
            )
        where = f"第 {lineno} 行："
        kind_raw = cells["type"].strip().lower()
        if kind_raw in READING_TYPES:
            kind = "R"
        elif kind_raw in SWAP_TYPES:
            kind = "S"
        else:
            raise CsvError(
                f"{where}未知记录类型 {cells['type'].strip()!r}"
                f"（应为读数 reading 或换表 swap）"
            )

        local = parse_local_dt(cells["local_ts"], where)
        if local_day is None:
            local_day = local.date()
        elif local.date() != local_day:
            raise CsvError(
                f"{where}本地日期 {local.date()} 与首行日期 {local_day} 不一致"
                f"（一个文件只能包含同一当地日的数据），整批拒绝"
            )
        if prev_local is not None and local < prev_local:
            raise CsvError(
                f"{where}本地时刻早于第 {prev_lineno} 行，设备记录顺序要求"
                f"时刻不递减，整批拒绝"
            )

        if kind == "R":
            value = parse_value(cells["value"], where)
        else:
            if cells["value"].strip():
                raise CsvError(f"{where}换表行的读数必须留空")
            value = None

        rows.append(
            {"lineno": lineno, "kind": kind, "local": local, "value": value}
        )
        prev_local, prev_lineno = local, lineno

    if not rows:
        raise CsvError("CSV 没有数据行")
    return rows, raw_count


# ---------------------------------------------------------------- 时区候选

def local_candidates(naive, tz):
    """当地 naive 时刻 -> 可能的规范化 UTC 时刻列表（升序、去重）。

    * 普通时刻：1 个候选；
    * 夏令时回拨的重复时刻：2 个候选（fold=0 的较早 UTC 在前）；
    * 夏令时跳变空档中不存在的时刻：返回空列表。
    """
    z0 = naive.replace(tzinfo=tz, fold=0)
    # 圆整校验：不存在的墙钟时间转 UTC 再转回来会落到别的墙钟时间
    if z0.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        return []
    out = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        c = canon_ts(aware.astimezone(timezone.utc))
        if c not in out:
            out.append(c)
    return out


def attach_candidates(rows, tz):
    """为每行挂上 UTC 候选；存在“不存在的当地时刻”则一次性报全行号拒绝。"""
    missing = []
    for row in rows:
        cands = local_candidates(row["local"], tz)
        if not cands:
            missing.append(row["lineno"])
        row["cands"] = cands
    if missing:
        raise CsvError(
            f"{fmt_lines(missing)}的当地时刻在时区 {tz.key} 不存在"
            f"（落在夏令时跳变空档），整批拒绝"
        )


# ---------------------------------------------------------------- 边界读取

def load_bounded_detail(conn, point, min_cand, max_cand):
    """只读取消歧核对所需的最小明细集合。

    返回 (db_events, seed_reading)：
      * db_events —— 候选时刻范围内的读数/换表，加上右端相邻区间所需的
        第一条更晚读数，按 (ts, 换表在前读数在后) 排序，元素为
        ``(ts, 'S'|'R', value_str|None)``；
      * seed_reading —— 左端相邻区间所需的第一条更早读数 ``(ts, value_str)``，
        没有则为 None。
    换表只取 (左端读数时刻, 右端读数时刻] 之间的部分——这正是可能影响
    单调性/缺口判定的全部换表。
    """
    left = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? AND ts < ?"
        " ORDER BY ts DESC LIMIT 1",
        (point, min_cand),
    ).fetchone()
    right = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? AND ts > ?"
        " ORDER BY ts ASC LIMIT 1",
        (point, max_cand),
    ).fetchone()

    events = [
        (r["ts"], "R", r["value"])
        for r in conn.execute(
            "SELECT ts, value FROM readings WHERE point = ? AND ts BETWEEN ? AND ?"
            " ORDER BY ts",
            (point, min_cand, max_cand),
        ).fetchall()
    ]
    if right is not None:
        events.append((right["ts"], "R", right["value"]))

    lo = left["ts"] if left is not None else None
    hi = right["ts"] if right is not None else None
    swap_rows = conn.execute(
        "SELECT ts FROM swaps WHERE point = ?"
        " AND (? IS NULL OR ts > ?) AND (? IS NULL OR ts <= ?) ORDER BY ts",
        (point, lo, lo, hi, hi),
    ).fetchall()
    events.extend((r["ts"], "S", None) for r in swap_rows)

    # 同一时刻先换表后读数：换表落在 (t1, t2] 含 t2，读数时刻的换表先造成缺口
    events.sort(key=lambda e: (e[0], 0 if e[1] == "S" else 1))
    seed = (left["ts"], left["value"]) if left is not None else None
    return events, seed


# ---------------------------------------------------------------- 消歧求解

def _after_reading(last_r, last_swap, ts, val):
    """合并流中读到一条读数后的状态。返回 (new_last_r, error)。

    区间 (last_r.ts, ts] 内有换表即为缺口，不校验增量；否则读数不得倒退。
    """
    if last_r is not None and last_r[0] < ts:
        gap = last_swap is not None and last_r[0] < last_swap <= ts
        if not gap and val < Decimal(last_r[1]):
            return None, (
                f"读数由 {last_r[1]} 倒退至 {value_str(val)}"
                f"且区间内没有换表"
            )
    return (ts, value_str(val)), None


class _Solver:
    """对给定文件行与库内边界明细求全部可行解释（计数上限 2）。"""

    def __init__(self, rows, db_events, seed_reading):
        self.rows = rows
        self.db_events = db_events
        self.db_ts = [e[0] for e in db_events]
        self.db_reading_by_ts = {
            e[0]: e[2] for e in db_events if e[1] == "R"
        }
        self.seed = seed_reading

    def _advance_db(self, lo_ts, hi_ts, last_r, last_swap):
        """处理库中满足 lo_ts < ts <= hi_ts 的事件，返回 (last_r, last_swap, error)。"""
        lo = bisect.bisect_right(self.db_ts, lo_ts) if lo_ts is not None else 0
        hi = bisect.bisect_right(self.db_ts, hi_ts)
        for ts, kind, val in self.db_events[lo:hi]:
            if kind == "S":
                last_swap = ts if last_swap is None else max(last_swap, ts)
            else:
                last_r, err = _after_reading(last_r, last_swap, ts, Decimal(val))
                if err:
                    return last_r, last_swap, f"库内读数在 {ts} 校验失败：{err}"
        return last_r, last_swap, None

    def try_candidate(self, i, c, prev_ts, last_r, last_swap):
        """尝试把第 i 行解释为 UTC 时刻 c。成功返回新状态，失败返回 (None, error)。"""
        last_r, last_swap, err = self._advance_db(
            prev_ts, c, last_r, last_swap
        )
        if err:
            return None, err

        row = self.rows[i]
        if row["kind"] == "S":
            # 同刻换表唯一性只约束本文件；库内同刻换表属于幂等重传，入库时跳过
            if (
                prev_ts == c
                and i > 0
                and self.rows[i - 1]["kind"] == "S"
            ):
                return None, "同一 UTC 时刻最多只能有一条换表记录"
            last_swap = c if last_swap is None else max(last_swap, c)
            return (c, last_r, last_swap), None

        # 读数行：同刻读数唯一性只约束本文件（库内同刻同值走幂等跳过，异值另检）
        if prev_ts == c and i > 0 and self.rows[i - 1]["kind"] == "R":
            return None, "同一 UTC 时刻最多只能有一条读数"
        db_val = self.db_reading_by_ts.get(c)
        if db_val is not None and Decimal(db_val) != row["value"]:
            return None, (
                f"与库中 {c} 的既有读数 {db_val} 同刻异值"
                f"（本次 {value_str(row['value'])}）"
            )
        last_r, err = _after_reading(last_r, last_swap, c, row["value"])
        if err:
            return None, err
        return (c, last_r, last_swap), None

    def solve(self):
        """返回 (count(0|1|2), 唯一时的 UTC 时刻列表|None, 多解相关行号集合)。"""
        rows = self.rows
        n = len(rows)
        seed_ts, seed_val = self.seed if self.seed else ("", "")

        @lru_cache(maxsize=None)
        def dp(i, prev_ts, lr_ts, lr_val, last_swap):
            """返回 (count 上限 2, 路径元组, 产生分歧的行号 frozenset)。"""
            last_r = (lr_ts, lr_val) if lr_ts else None
            if i == n:
                # 收尾：处理右端相邻区间（含更晚的边界读数与其前换表）
                _, _, err = self._advance_db(prev_ts, TS_HIGH, last_r, last_swap)
                if err:
                    return 0, (), frozenset()
                return 1, (), frozenset()

            count = 0
            path = ()
            flex = set()
            distinct_ok = 0
            for c in rows[i]["cands"]:
                # UTC 不递减只约束不同当地时刻之间；同一当地时刻的多行（回拨重复）
                # 候选集合相同，折叠先后属于待消歧的解释自由度，不在此剪枝
                if (
                    i > 0
                    and rows[i - 1]["local"] < rows[i]["local"]
                    and prev_ts is not None
                    and c < prev_ts
                ):
                    continue
                new_state, err = self.try_candidate(
                    i, c, prev_ts, last_r, last_swap
                )
                if err is not None:
                    continue
                _, new_lr, new_swap = new_state
                sub_count, sub_path, sub_flex = dp(
                    i + 1,
                    c,
                    new_lr[0] if new_lr else "",
                    new_lr[1] if new_lr else "",
                    new_swap or "",
                )
                if sub_count == 0:
                    continue
                distinct_ok += 1
                count = min(2, count + sub_count)
                if not path:
                    path = (c,) + sub_path
                flex |= set(sub_flex)
                if count >= 2:
                    break
            if distinct_ok >= 2:
                flex.add(i)
            return count, path, frozenset(flex)

        count, path, flex = dp(0, None, seed_ts, seed_val, None)
        flex_lines = {rows[i]["lineno"] for i in flex}
        return count, (list(path) if count == 1 else None), flex_lines

    def diagnose(self):
        """无解时的贪心诊断：返回第一个走不通的行号与具体原因。"""
        prev_ts = None
        last_r = self.seed
        last_swap = None
        for i, row in enumerate(self.rows):
            last_err = None
            for c in row["cands"]:
                new_state, err = self.try_candidate(
                    i, c, prev_ts, last_r, last_swap
                )
                if err is None:
                    prev_ts, last_r, last_swap = new_state
                    break
                last_err = err
            else:
                return row["lineno"], last_err or "没有满足约束的 UTC 解释"
        _, _, err = self._advance_db(prev_ts, TS_HIGH, last_r, last_swap)
        if err:
            return self.rows[-1]["lineno"], err
        return None, None


def resolve_unique(conn, point, rows):
    """在当前事务快照内消歧。返回每行对应的规范化 UTC 时刻列表，否则 BatchRejected。"""
    min_cand = min(r["cands"][0] for r in rows)
    max_cand = max(r["cands"][-1] for r in rows)
    db_events, seed = load_bounded_detail(conn, point, min_cand, max_cand)
    solver = _Solver(rows, db_events, seed)
    count, assignment, flex_lines = solver.solve()
    if count == 0:
        lineno, reason = solver.diagnose()
        raise BatchRejected(
            f"第 {lineno} 行：{reason}，所有 UTC 解释均不成立，整批拒绝"
        )
    if count >= 2:
        raise BatchRejected(
            f"{fmt_lines(sorted(flex_lines))}处于夏令时回拨的重复当地时刻，"
            f"存在多种 UTC 解释且均能通过校验，无法唯一确定，整批拒绝"
        )
    return assignment


# ---------------------------------------------------------------- 入库

def apply_local_batch(conn, upload_id, point, rows, chosen_ts):
    """在调用方事务内写入读数与换表（同生共死）。返回 (新增, 幂等跳过)。"""
    applied = skipped = 0
    for row, ts in zip(rows, chosen_ts):
        if row["kind"] == "R":
            existing = conn.execute(
                "SELECT value FROM readings WHERE point = ? AND ts = ?",
                (point, ts),
            ).fetchone()
            if existing is not None:
                # 消歧阶段已确认同值，走到这里只可能是幂等重传
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO readings(point, ts, value, upload_id)"
                " VALUES (?,?,?,?)",
                (point, ts, value_str(row["value"]), upload_id),
            )
            applied += 1
        else:
            existing = conn.execute(
                "SELECT 1 FROM swaps WHERE point = ? AND ts = ?", (point, ts)
            ).fetchone()
            if existing is not None:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO swaps(point, ts, upload_id) VALUES (?,?,?)",
                (point, ts, upload_id),
            )
            applied += 1

    errors = find_negative_deltas(conn, point)
    if errors:  # 兜底：消歧 DP 已逐区间校验，理论上不会触发
        raise BatchRejected("读数倒退，整批拒绝：" + "；".join(errors[:5]))
    return applied, skipped
