"""复核报告计算（仓侧与离线复算共用同一份逻辑，保证结果一致）。

报告内容（对点名单据物理ID，各项互不掩盖、同时给出）：
  * 重号 duplicate_doc_number：同 (supplier, doc_no) 的单据物理行是否多于一行
  * 空单 empty_document：该单键下是否没有任何项目行
  * 逐项实到 arrived_qty：该项目行三项键命中的到货数量合计（无到货为 0）
  * 短/齐/超到 fulfillment：short / exact / over
  * 挂起：整单挂起（挂起行 item_no 为 null）或同项目挂起（item_no 相等；
    空字符串不是空值，仅匹配 item_no 为 "" 的项目）

到货先按三项键一次性聚合，再供各项目行查询：一份到货对每个同项目行各
贡献一次，绝不因连接扇出乘量，也不物化"项目×到货"全组合。
"""
from __future__ import annotations


def compute_report(doc_row_id: str, documents, items, arrivals, suspensions) -> dict:
    doc = None
    for d in documents:
        if d["doc_row_id"] == doc_row_id:
            doc = d
            break
    if doc is None:
        raise ValueError(f"给定单据集合中不存在物理ID: {doc_row_id!r}")
    supplier, doc_no = doc["supplier"], doc["doc_no"]

    # 到货按三项键聚合（单遍），不物化项目×到货组合
    arr_sum = {}
    arr_ids = {}
    for a in arrivals:
        k = (a["supplier"], a["doc_no"], a["item_no"])
        arr_sum[k] = arr_sum.get(k, 0) + a["qty"]
        arr_ids.setdefault(k, []).append(a["arrival_row_id"])

    whole_doc_susp = sorted(
        s["susp_row_id"] for s in suspensions if s["item_no"] is None
    )
    item_susp = {}
    for s in suspensions:
        if s["item_no"] is not None:
            item_susp.setdefault(s["item_no"], []).append(s["susp_row_id"])
    document_suspended = bool(whole_doc_susp)

    item_entries = []
    for it in sorted(items, key=lambda r: r["item_row_id"]):
        k3 = (supplier, doc_no, it["item_no"])
        actual = arr_sum.get(k3, 0)  # 无到货为 0
        expected = it["expected_qty"]
        if actual < expected:
            fulfillment = "short"   # 短到
        elif actual == expected:
            fulfillment = "exact"   # 齐到
        else:
            fulfillment = "over"    # 超到
        susp_ids = sorted(whole_doc_susp + item_susp.get(it["item_no"], []))
        item_entries.append({
            "item_row_id": it["item_row_id"],
            "item_no": it["item_no"],
            "expected_qty": expected,
            "arrived_qty": actual,
            "arrival_row_ids": sorted(arr_ids.get(k3, [])),
            "fulfillment": fulfillment,
            "suspended": bool(susp_ids),
            "suspension_row_ids": susp_ids,
        })

    all_item_row_ids = sorted(it["item_row_id"] for it in items)
    susp_entries = []
    for s in sorted(suspensions, key=lambda r: r["susp_row_id"]):
        if s["item_no"] is None:
            scope = "document"
            applies = all_item_row_ids
        else:
            scope = "item"
            applies = sorted(
                it["item_row_id"] for it in items if it["item_no"] == s["item_no"]
            )
        susp_entries.append({
            "susp_row_id": s["susp_row_id"],
            "item_no": s["item_no"],
            "scope": scope,
            "applies_to_item_row_ids": applies,
        })

    return {
        "doc_row_id": doc_row_id,
        "doc_key": {"supplier": supplier, "doc_no": doc_no},
        "duplicate_doc_number": {
            "is_duplicate": len(documents) > 1,
            "doc_row_ids": sorted(d["doc_row_id"] for d in documents),
        },
        "empty_document": len(items) == 0,
        "document_suspended": document_suspended,
        "items": item_entries,
        "suspensions": susp_entries,
    }
