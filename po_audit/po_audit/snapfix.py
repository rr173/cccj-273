"""可复现的多快照夹具：三版完整仓 + 确定性演化事实，用于验证差异处置清单。

与 ``fixtures.py`` 一样产出标准仓布局（version.json + 四表 JSONL），可直接由
``Warehouse.load`` 装载；三版目录 snap-v1/snap-v2/snap-v3 共享稳定物理行ID，
按脚本化方式演化，使每一种处置清单语义都有可断言的事实对应：

* snap-v1 → snap-v2
    - 差异消失（短到补齐）：移出当前清单、历史保留；
    - 事实变化但差异仍在（短到 4→5 仍短）：重新待处理、旧处置可查；
    - 只影响一个项目的变化：同单另一项目的处置原样保留；
    - 挂起解除后再挂起：同ID再现，历史不丢；
    - 单据级差异（重号/空单）与项目级差异独立演化；
    - 单据整体删除：清单保留原处置，摘要报告 missing。
* 任一快照重复刷新：幂等，不重复生成处置项、不重置处置、不追加事件。
* 始终存在与本案无关的仓内行（ZULU/PO-Z），证明只处理点名范围。

``evolution.json`` 记录每版期望的差异集合与版本间期望动作（含确定性
discrepancy_id），供单元测试与 CLI 验证直接断言。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import model
from .dispositions import discrepancy_id_for

VERSIONS = ("snap-v1", "snap-v2", "snap-v3")


def _doc(rid, sup, no):
    return {"doc_row_id": rid, "supplier": sup, "doc_no": no,
            "permit": True, "restriction": None}


def _item(rid, sup, no, ino, exp):
    return {"item_row_id": rid, "supplier": sup, "doc_no": no, "item_no": ino,
            "expected_qty": exp, "permit": True, "restriction": None}


def _arr(rid, sup, no, ino, qty):
    return {"arrival_row_id": rid, "supplier": sup, "doc_no": no, "item_no": ino,
            "qty": qty, "permit": True, "restriction": None}


def _susp(rid, sup, no, ino, reason):
    return {"susp_row_id": rid, "supplier": sup, "doc_no": no, "item_no": ino,
            "reason": reason, "permit": True, "restriction": None}


def _build_rows(version: str) -> dict:
    """返回该版本四表的完整行集合。物理ID跨版本稳定；删除即不出现在后版。"""
    docs, items, arrivals, susps = [], [], [], []

    # ---- 单据（v2 起 FOXTROT/PO-F 整体删除，模拟仓内单据消失）----
    docs += [_doc("D-A", "ACME", "PO-A"), _doc("D-B", "BETA", "PO-B"),
             _doc("D-C", "GAMMA", "PO-C"), _doc("D-D", "DELTA", "PO-D")]
    docs += [_doc("D-E1", "EPSILON", "PO-E"), _doc("D-E2", "EPSILON", "PO-E")]
    if version == "snap-v1":
        docs.append(_doc("D-F", "FOXTROT", "PO-F"))
    docs.append(_doc("D-Z", "ZULU", "PO-Z"))  # 从不点名的无关单

    # ---- 项目 ----
    items += [
        _item("I-A1", "ACME", "PO-A", "WIDGET", 10),
        _item("I-A2", "ACME", "PO-A", "GADGET", 5),
        _item("I-B1", "BETA", "PO-B", "NUT", 8),
        _item("I-B2", "BETA", "PO-B", "BOLT", 4),
        _item("I-C1", "GAMMA", "PO-C", "VALVE", 6),
        _item("I-D1", "DELTA", "PO-D", "RING", 3),
    ]
    if version in ("snap-v2", "snap-v3"):
        # v2 起空单 EPSILON 增加一个项目（空单差异消失，产生短到差异）
        items.append(_item("I-E1", "EPSILON", "PO-E", "COG", 1))
    if version == "snap-v1":
        items.append(_item("I-F1", "FOXTROT", "PO-F", "PART", 2))
    items.append(_item("I-Z1", "ZULU", "PO-Z", "Q", 1))

    # ---- 到货 ----
    arrivals += [
        _arr("A-A1", "ACME", "PO-A", "WIDGET", 4),
        _arr("A-A2", "ACME", "PO-A", "WIDGET", 6),   # WIDGET 始终齐到
        _arr("A-A3", "ACME", "PO-A", "GADGET", 3),   # v1 短到 5
    ]
    if version in ("snap-v2", "snap-v3"):
        arrivals.append(_arr("A-A4", "ACME", "PO-A", "GADGET", 2))  # 补齐→差异消失

    arrivals.append(_arr("A-B1", "BETA", "PO-B", "NUT", 3))        # v1 短到 8
    if version in ("snap-v2", "snap-v3"):
        arrivals.append(_arr("A-B3", "BETA", "PO-B", "NUT", 5))    # NUT 补齐
    arrivals.append(_arr("A-B2", "BETA", "PO-B", "BOLT", 1))       # BOLT v1/v2 短到 4
    if version == "snap-v3":
        arrivals.append(_arr("A-B4", "BETA", "PO-B", "BOLT", 3))   # v3 BOLT 补齐

    arrivals += [
        _arr("A-C1", "GAMMA", "PO-C", "VALVE", 2),
        _arr("A-C2", "GAMMA", "PO-C", "VALVE", 2),                  # v1 实到 4 短
    ]
    if version in ("snap-v2", "snap-v3"):
        arrivals.append(_arr("A-C3", "GAMMA", "PO-C", "VALVE", 1))  # v2 实到 5 仍短→事实变化
    if version == "snap-v3":
        arrivals.append(_arr("A-C4", "GAMMA", "PO-C", "VALVE", 1))  # v3 实到 6 齐→差异消失

    if version in ("snap-v1", "snap-v2"):
        # v1/v2 RING 齐到（仅挂起差异）；v3 删除该到货并改为超到
        arrivals.append(_arr("A-D0", "DELTA", "PO-D", "RING", 3))
    if version == "snap-v3":
        arrivals.append(_arr("A-D1", "DELTA", "PO-D", "RING", 5))   # v3 超到（exp 3）

    if version == "snap-v1":
        arrivals.append(_arr("A-F1", "FOXTROT", "PO-F", "PART", 2))
    arrivals.append(_arr("A-Z1", "ZULU", "PO-Z", "Q", 1))

    # ---- 挂起 ----
    if version == "snap-v1":
        susps.append(_susp("S-D1", "DELTA", "PO-D", "RING", "ring hold"))
    if version == "snap-v3":
        # v2 解除、v3 再挂起：同自然键→同 discrepancy_id 再现
        susps.append(_susp("S-D2", "DELTA", "PO-D", "RING", "ring hold again"))
        susps.append(_susp("S-E1", "EPSILON", "PO-E", None, "whole-doc hold"))

    return {"documents": docs, "items": items,
            "arrivals": arrivals, "suspensions": susps}


# 自然键描述（kind, supplier, doc_no, item_row_id|None）
# 注意：整单挂起在 compute_report 中同时使每个项目行 suspended=true，
# 因此 v3 EPSILON 的整单挂起还会派生 I-E1 的 item_suspended 差异。
_NATURAL = [
    ("short", ("ACME", "PO-A"), "I-A2"),
    ("short", ("BETA", "PO-B"), "I-B1"),
    ("short", ("BETA", "PO-B"), "I-B2"),
    ("short", ("GAMMA", "PO-C"), "I-C1"),
    ("item_suspended", ("DELTA", "PO-D"), "I-D1"),
    ("over", ("DELTA", "PO-D"), "I-D1"),
    ("duplicate_doc_number", ("EPSILON", "PO-E"), None),
    ("empty_document", ("EPSILON", "PO-E"), None),
    ("document_suspended", ("EPSILON", "PO-E"), None),
    ("short", ("EPSILON", "PO-E"), "I-E1"),
    ("item_suspended", ("EPSILON", "PO-E"), "I-E1"),
]


def _did(kind, doc_key, item_row_id):
    return discrepancy_id_for(kind, doc_key, item_row_id)


def _expected_active(version: str) -> list:
    """该版本刷新后应处于 active 的差异ID集合（按自然键）。"""
    active = []
    for kind, key, iid in _NATURAL:
        d = _did(kind, key, iid)
        if version == "snap-v1":
            on = d in {
                _did("short", ("ACME", "PO-A"), "I-A2"),
                _did("short", ("BETA", "PO-B"), "I-B1"),
                _did("short", ("BETA", "PO-B"), "I-B2"),
                _did("short", ("GAMMA", "PO-C"), "I-C1"),
                _did("item_suspended", ("DELTA", "PO-D"), "I-D1"),
                _did("duplicate_doc_number", ("EPSILON", "PO-E"), None),
                _did("empty_document", ("EPSILON", "PO-E"), None),
            }
        elif version == "snap-v2":
            on = d in {
                _did("short", ("BETA", "PO-B"), "I-B2"),
                _did("short", ("GAMMA", "PO-C"), "I-C1"),
                _did("duplicate_doc_number", ("EPSILON", "PO-E"), None),
                _did("short", ("EPSILON", "PO-E"), "I-E1"),
            }
        else:  # snap-v3
            on = d in {
                _did("item_suspended", ("DELTA", "PO-D"), "I-D1"),
                _did("over", ("DELTA", "PO-D"), "I-D1"),
                _did("duplicate_doc_number", ("EPSILON", "PO-E"), None),
                _did("document_suspended", ("EPSILON", "PO-E"), None),
                _did("short", ("EPSILON", "PO-E"), "I-E1"),
                _did("item_suspended", ("EPSILON", "PO-E"), "I-E1"),
            }
        if on:
            active.append(d)
    return sorted(active)


def _expected_transition(version: str) -> dict:
    """从上一版刷新到本版时各动作桶应包含的差异ID（同版重复刷新时全部为空/unchanged）。"""
    if version == "snap-v1":
        return {"created": _expected_active("snap-v1"), "reopened": [],
                "removed": [], "reappeared": []}
    if version == "snap-v2":
        return {
            "created": [_did("short", ("EPSILON", "PO-E"), "I-E1")],
            "reopened": [_did("short", ("GAMMA", "PO-C"), "I-C1")],
            "removed": sorted([
                _did("short", ("ACME", "PO-A"), "I-A2"),
                _did("short", ("BETA", "PO-B"), "I-B1"),
                _did("item_suspended", ("DELTA", "PO-D"), "I-D1"),
                _did("empty_document", ("EPSILON", "PO-E"), None),
            ]),
            "reappeared": [],
        }
    return {
        "created": sorted([
            _did("over", ("DELTA", "PO-D"), "I-D1"),
            _did("document_suspended", ("EPSILON", "PO-E"), None),
            _did("item_suspended", ("EPSILON", "PO-E"), "I-E1"),
        ]),
        "reopened": [],
        "removed": sorted([
            _did("short", ("GAMMA", "PO-C"), "I-C1"),
            _did("short", ("BETA", "PO-B"), "I-B2"),
        ]),
        "reappeared": [_did("item_suspended", ("DELTA", "PO-D"), "I-D1")],
    }


def generate_multi_snapshot(out_dir: str) -> dict:
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise SystemExit(f"输出目录非空: {out_dir}")
    paths = {}
    for version in VERSIONS:
        vdir = os.path.join(out_dir, version)
        os.makedirs(vdir, exist_ok=True)
        rows = _build_rows(version)
        for table in model.TABLES:
            for r in rows[table]:
                model.validate_row(table, r)
            p = os.path.join(vdir, f"{table}.jsonl")
            with open(p, "wb") as fh:
                for r in rows[table]:
                    fh.write(model.canonical_bytes(r) + b"\n")
        with open(os.path.join(vdir, "version.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": version}, fh, ensure_ascii=False)
            fh.write("\n")
        paths[version] = vdir

    # v1 的点名单据（FOXTROT 用于验证 v2 单据消失；ZULU 不点名）
    requests = [{"request_id": f"REQ-{rid}", "doc_row_id": rid} for rid in (
        "D-A", "D-B", "D-C", "D-D", "D-E1", "D-F",
    )]
    with open(os.path.join(out_dir, "requests.json"), "w", encoding="utf-8") as fh:
        json.dump(requests, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    evolution = {
        "versions": list(VERSIONS),
        "requests": requests,
        "natural_keys": [
            {"kind": k, "doc_key": {"supplier": key[0], "doc_no": key[1]},
             "item_row_id": iid,
             "discrepancy_id": _did(k, key, iid)}
            for k, key, iid in _NATURAL
        ],
        "expected_active": {v: _expected_active(v) for v in VERSIONS},
        "expected_transition": {v: _expected_transition(v) for v in VERSIONS},
        "expected_missing_after": {"snap-v2": ["D-F"], "snap-v3": ["D-F"]},
        "scenario_notes": {
            "ACME/PO-A": "v1 短到，v2 补齐→移除；v3 无差异，历史保留",
            "BETA/PO-B": "两项目各自短到；v2 只改 NUT，BOLT 处置不得被重置",
            "GAMMA/PO-C": "v2 实到 4→5 仍短→事实变化再开，旧处置入历史；v3 齐到移除",
            "DELTA/PO-D": "v1/v2 齐到仅项目挂起；v2 解除移除，v3 再挂起同ID再现，且超到新建差异",
            "EPSILON/PO-E": "重号始终在（处置保留）；v1 空单，v2 空单消失转项目短到；v3 整单挂起",
            "FOXTROT/PO-F": "v2 起单据整体删除→missing，清单不产生新项",
            "ZULU/PO-Z": "从不点名，任何版本不得产生处置项",
        },
    }
    with open(os.path.join(out_dir, "evolution.json"), "w", encoding="utf-8") as fh:
        json.dump(evolution, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    return {"out_dir": out_dir, "snapshots": paths,
            "requests_file": os.path.join(out_dir, "requests.json"),
            "evolution_file": os.path.join(out_dir, "evolution.json")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m po_audit.snapfix",
        description="生成可复现的三版仓快照夹具（差异处置清单验证用）",
    )
    ap.add_argument("--out", required=True, help="输出目录（须不存在或为空）")
    args = ap.parse_args(argv)
    summary = generate_multi_snapshot(args.out)
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
