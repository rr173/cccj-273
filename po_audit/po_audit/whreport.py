"""仓侧（可信方）复核报告：直接在完整仓上计算，用于与离线复算结果交叉核对。"""
from __future__ import annotations

import argparse
import json
import sys

from .report import compute_report
from .warehouse import Warehouse


def warehouse_report(wh: Warehouse, doc_row_id: str) -> dict:
    scope = wh.compute_scope("__warehouse__", doc_row_id)
    if scope is None:
        raise KeyError(f"单据物理ID不存在: {doc_row_id!r}")
    arrivals = [r for _k, rows in scope.arrival_keys for r in rows]
    rep = compute_report(doc_row_id, scope.documents, scope.items, arrivals, scope.suspensions)
    rep["warehouse_version"] = wh.version
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m po_audit.whreport",
        description="在完整仓上直接计算点名单据的复核报告（可信侧参照）",
    )
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--doc-row-id", required=True)
    args = ap.parse_args(argv)
    wh = Warehouse.load(args.warehouse)
    try:
        rep = warehouse_report(wh, args.doc_row_id)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1
    json.dump(rep, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
