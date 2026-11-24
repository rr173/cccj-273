"""四表模式、行校验与规范哈希。

同版四表（物理行ID各表唯一）：
  documents   单据: doc_row_id, supplier, doc_no, permit, restriction
  items       项目: item_row_id, supplier, doc_no, item_no, expected_qty(>=0), permit, restriction
  arrivals    到货: arrival_row_id, supplier, doc_no, item_no, qty(>=0), permit, restriction
  suspensions 挂起: susp_row_id, supplier, doc_no, item_no(可为 null), reason, permit, restriction

要点：
  * 每行都有交付许可 permit；permit=false 时 restriction 必须给出受限类别。
  * 空字符串不是空值：items/arrivals 的 item_no 必须是字符串（可为 ""），
    suspensions 的 item_no 为 null 时表示整单挂起，"" 只匹配 item_no 为 "" 的项目。
"""
from __future__ import annotations

import hashlib
import json

TABLES = ("documents", "items", "arrivals", "suspensions")

ID_FIELD = {
    "documents": "doc_row_id",
    "items": "item_row_id",
    "arrivals": "arrival_row_id",
    "suspensions": "susp_row_id",
}

# 各表除物理行ID外用于关联的键
DOC_KEY_FIELDS = ("supplier", "doc_no")
ITEM_KEY_FIELDS = ("supplier", "doc_no", "item_no")


class SchemaError(ValueError):
    """行不符合四表模式。"""


def _is_nonneg_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0


def canonical_bytes(obj) -> bytes:
    """JSON 规范化序列化（排序键、紧凑分隔符、UTF-8），用于可检验哈希。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def row_hash(row) -> str:
    return hashlib.sha256(canonical_bytes(row)).hexdigest()


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_key_of(row):
    return (row["supplier"], row["doc_no"])


def item_key_of(row):
    return (row["supplier"], row["doc_no"], row["item_no"])


def validate_row(table: str, row) -> None:
    if table not in TABLES:
        raise SchemaError(f"未知表: {table!r}")
    if not isinstance(row, dict):
        raise SchemaError(f"{table}: 行必须是 JSON 对象")
    idf = ID_FIELD[table]
    expected = {idf, "supplier", "doc_no", "permit", "restriction"}
    if table == "items":
        expected |= {"item_no", "expected_qty"}
    elif table == "arrivals":
        expected |= {"item_no", "qty"}
    elif table == "suspensions":
        expected |= {"item_no", "reason"}
    missing = expected - row.keys()
    extra = row.keys() - expected
    if missing:
        raise SchemaError(f"{table}: 缺字段 {sorted(missing)}")
    if extra:
        raise SchemaError(f"{table}: 多字段 {sorted(extra)}")

    if not isinstance(row[idf], str) or not row[idf]:
        raise SchemaError(f"{table}: {idf} 必须是非空字符串")
    for f in DOC_KEY_FIELDS:
        if not isinstance(row[f], str):
            raise SchemaError(f"{table}: {f} 必须是字符串")
    if not isinstance(row["permit"], bool):
        raise SchemaError(f"{table}: permit 必须是布尔值")
    if row["permit"]:
        if row["restriction"] is not None:
            raise SchemaError(f"{table}: permit=true 时 restriction 必须为 null")
    else:
        if not isinstance(row["restriction"], str) or not row["restriction"]:
            raise SchemaError(f"{table}: permit=false 时 restriction 必须给出受限类别")

    if table in ("items", "arrivals"):
        # 空字符串合法；null 不合法（空字符串不是空值）
        if not isinstance(row["item_no"], str):
            raise SchemaError(f"{table}: item_no 必须是字符串（可为空字符串，不可为 null）")
    if table == "items":
        if not _is_nonneg_number(row["expected_qty"]):
            raise SchemaError(f"{table}: expected_qty 必须是非负数值")
    if table == "arrivals":
        if not _is_nonneg_number(row["qty"]):
            raise SchemaError(f"{table}: qty 必须是非负数值")
    if table == "suspensions":
        if row["item_no"] is not None and not isinstance(row["item_no"], str):
            raise SchemaError(f"{table}: item_no 必须是字符串或 null")
        if not isinstance(row["reason"], str):
            raise SchemaError(f"{table}: reason 必须是字符串")


def parse_table_lines(lines, table: str, source: str):
    """解析并校验一张表的 JSONL 行，返回行列表；物理行ID必须唯一。"""
    rows = []
    seen = set()
    idf = ID_FIELD[table]
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise SchemaError(f"{source}:{lineno}: JSON 解析失败: {e}") from e
        try:
            validate_row(table, row)
        except SchemaError as e:
            raise SchemaError(f"{source}:{lineno}: {e}") from e
        rid = row[idf]
        if rid in seen:
            raise SchemaError(f"{source}:{lineno}: {idf} 重复: {rid!r}")
        seen.add(rid)
        rows.append(row)
    return rows


def load_table(path, table: str):
    with open(path, "r", encoding="utf-8") as fh:
        return parse_table_lines(fh, table, str(path))
