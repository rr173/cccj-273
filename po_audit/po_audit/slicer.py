"""切片器：从完整仓按案件切出必要原行，生成交付包与仓主范围清单。

语义要点：
  * 必要范围恰为同单键全部单据/项目、命中项目键全部到货、适用挂起，不带其他行。
  * 同一原行被多案共享时只交付一份（包内物理去重）。
  * 每行有交付许可；任一必要行受限则整案拒发（绝不删掉受限证据后宣称合格），
    其他独立案件仍可交付。
  * 拒发只给请求ID及受限类别，不外露受限行身份、数量或内容。
  * 范围清单（manifest）逐案列出每个查询的行ID；确实无行的查询显式标 empty，
    并汇总于 empty_queries；全部交付行带 SHA-256，内容完整性可检验。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone

from . import PACKAGE_FORMAT, model
from .warehouse import Warehouse


class SliceError(ValueError):
    pass


def _empty_label(query):
    t = query["table"]
    if t == "arrivals":
        return f"arrivals[item_no={query['key']['item_no']!r}]"
    return t


def build_case_entry(scope) -> dict:
    """把 CaseScope 转成清单中的 delivered 案件条目，并收集行。"""
    supplier, doc_no = scope.doc_key
    queries = []

    doc_ids = sorted(r["doc_row_id"] for r in scope.documents)
    queries.append({
        "table": "documents",
        "key": {"supplier": supplier, "doc_no": doc_no},
        "row_ids": doc_ids,
        "empty": not doc_ids,
    })

    item_ids = sorted(r["item_row_id"] for r in scope.items)
    queries.append({
        "table": "items",
        "key": {"supplier": supplier, "doc_no": doc_no},
        "row_ids": item_ids,
        "empty": not item_ids,
    })

    for k3, arr_rows in scope.arrival_keys:
        ids = sorted(r["arrival_row_id"] for r in arr_rows)
        queries.append({
            "table": "arrivals",
            "key": {"supplier": k3[0], "doc_no": k3[1], "item_no": k3[2]},
            "row_ids": ids,
            "empty": not ids,
        })

    susp_ids = sorted(r["susp_row_id"] for r in scope.suspensions)
    queries.append({
        "table": "suspensions",
        "key": {"supplier": supplier, "doc_no": doc_no},
        "row_ids": susp_ids,
        "empty": not susp_ids,
    })

    empty_queries = [_empty_label(q) for q in queries if q["empty"]]
    return {
        "request_id": scope.request_id,
        "status": "delivered",
        "doc_row_id": scope.doc_row_id,
        "doc_key": {"supplier": supplier, "doc_no": doc_no},
        "queries": queries,
        "empty_queries": empty_queries,
    }


def slice_package(warehouse: Warehouse, requests, out_dir: str, hmac_key: bytes | None = None) -> dict:
    """执行切片，写出交付包，返回逐案摘要。"""
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise SliceError(f"交付包目录非空: {out_dir}")
    os.makedirs(out_dir, exist_ok=True)

    seen_req = set()
    cases = []
    pkg_rows = {t: {} for t in model.TABLES}  # 物理行ID -> 原行（跨案去重）

    for req in requests:
        if not isinstance(req, dict):
            raise SliceError("请求必须是对象")
        rid = req.get("request_id")
        did = req.get("doc_row_id")
        if not isinstance(rid, str) or not rid:
            raise SliceError("request_id 必须是非空字符串")
        if not isinstance(did, str) or not did:
            raise SliceError(f"{rid}: doc_row_id 必须是非空字符串")
        if rid in seen_req:
            raise SliceError(f"request_id 重复: {rid}")
        seen_req.add(rid)

        scope = warehouse.compute_scope(rid, did)
        if scope is None:
            cases.append({"request_id": rid, "status": "unknown_request"})
            continue

        # 许可检查：任一必要行受限 → 整案拒发（不删行充合格）
        restricted = [row for _t, row in scope.rows() if not row["permit"]]
        if restricted:
            cats = sorted({r["restriction"] for r in restricted})
            # 只暴露请求ID与受限类别；不含行身份、数量、内容
            cases.append({
                "request_id": rid,
                "status": "refused",
                "restricted_categories": cats,
            })
            continue

        entry = build_case_entry(scope)
        cases.append(entry)
        for table, row in scope.rows():
            bucket = pkg_rows[table]
            rid_ = row[model.ID_FIELD[table]]
            prev = bucket.get(rid_)
            if prev is None:
                bucket[rid_] = row            # 共享原行只收一份
            elif prev is not row and prev != row:
                raise SliceError(f"{table}: 物理行ID冲突 {rid_}")

    # 写四表切片文件（按物理行ID排序，规范化JSON，逐行一行）
    files_meta = {}
    row_hashes = {}
    for t in model.TABLES:
        rows = [pkg_rows[t][rid] for rid in sorted(pkg_rows[t])]
        rel = f"{t}.jsonl"
        data = b"".join(model.canonical_bytes(r) + b"\n" for r in rows)
        with open(os.path.join(out_dir, rel), "wb") as fh:
            fh.write(data)
        files_meta[t] = {
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "row_count": len(rows),
        }
        row_hashes[t] = {r[model.ID_FIELD[t]]: model.row_hash(r) for r in rows}

    manifest = {
        "format": PACKAGE_FORMAT,
        "warehouse_version": warehouse.version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files_meta,
        "row_hashes": row_hashes,
        "cases": cases,
    }
    if hmac_key is not None:
        sig = hmac.new(hmac_key, model.canonical_bytes(manifest), hashlib.sha256).hexdigest()
        manifest["signature"] = {"algo": "HMAC-SHA256", "hex": sig}

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    summary = {
        "warehouse_version": warehouse.version,
        "package_dir": out_dir,
        "cases": [
            {k: v for k, v in c.items() if k in ("request_id", "status", "restricted_categories")}
            for c in cases
        ],
        "rows_delivered": {t: files_meta[t]["row_count"] for t in model.TABLES},
    }
    return summary


def _load_requests(path):
    with open(path, "r", encoding="utf-8") as fh:
        reqs = json.load(fh)
    if not isinstance(reqs, list):
        raise SliceError("请求文件必须是 JSON 数组")
    return reqs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m po_audit.slicer",
        description="从完整仓切片生成交付包（含仓主范围清单）",
    )
    ap.add_argument("--warehouse", required=True, help="完整仓目录（version.json + 四表 JSONL）")
    ap.add_argument("--requests", required=True, help="请求文件 JSON：[{request_id, doc_row_id}]")
    ap.add_argument("--package", required=True, help="交付包输出目录（须不存在或为空）")
    ap.add_argument("--hmac-key-file", help="可选：仓主密钥文件，用于签署范围清单")
    args = ap.parse_args(argv)

    key = None
    if args.hmac_key_file:
        with open(args.hmac_key_file, "rb") as fh:
            key = fh.read()
        if not key:
            raise SliceError("HMAC 密钥文件为空")

    wh = Warehouse.load(args.warehouse)
    requests = _load_requests(args.requests)
    summary = slice_package(wh, requests, args.package, hmac_key=key)
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
