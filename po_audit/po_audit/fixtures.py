"""可生成夹具：确定性生成完整仓（同版四表）与请求清单。

覆盖语义边角：重号、空单、空项目号（null 与 "" 有别）、整单/同项目挂起、
不适用挂起、不命中任何项目的到货、受限行（整案拒发）、共享原行、热点键。
另生成随机填充行模拟大表。crafted.json 记录构造事实供测试断言。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys


def generate(out_dir: str, seed: int = 7, filler_docs: int = 200,
             hot_docs: int = 300, hot_items: int = 2000, hot_arrivals: int = 20000,
             version: str = "v2026-11-24") -> dict:
    rng = random.Random(seed)
    docs, items, arrivals, susps = [], [], [], []
    counters = {"D": 0, "I": 0, "A": 0, "S": 0}

    def nid(p):
        counters[p] += 1
        return f"{p}-{counters[p]:06d}"

    def add_doc(sup, no, permit=True, restriction=None):
        r = {"doc_row_id": nid("D"), "supplier": sup, "doc_no": no,
             "permit": permit, "restriction": restriction}
        docs.append(r)
        return r["doc_row_id"]

    def add_item(sup, no, ino, exp, permit=True, restriction=None):
        r = {"item_row_id": nid("I"), "supplier": sup, "doc_no": no, "item_no": ino,
             "expected_qty": exp, "permit": permit, "restriction": restriction}
        items.append(r)
        return r["item_row_id"]

    def add_arr(sup, no, ino, qty, permit=True, restriction=None):
        r = {"arrival_row_id": nid("A"), "supplier": sup, "doc_no": no, "item_no": ino,
             "qty": qty, "permit": permit, "restriction": restriction}
        arrivals.append(r)
        return r["arrival_row_id"]

    def add_susp(sup, no, ino, reason, permit=True, restriction=None):
        r = {"susp_row_id": nid("S"), "supplier": sup, "doc_no": no, "item_no": ino,
             "reason": reason, "permit": permit, "restriction": restriction}
        susps.append(r)
        return r["susp_row_id"]

    crafted = {}

    # --- ACME/PO-1001：干净单，齐到+短到；含不命中项目的到货与不适用挂起（不得入包）
    d1 = add_doc("ACME", "PO-1001")
    i1 = add_item("ACME", "PO-1001", "WIDGET", 10)
    i2 = add_item("ACME", "PO-1001", "GADGET", 5)
    a1 = add_arr("ACME", "PO-1001", "WIDGET", 4)
    a2 = add_arr("ACME", "PO-1001", "WIDGET", 6)
    a3 = add_arr("ACME", "PO-1001", "GADGET", 3)
    stray_arr = add_arr("ACME", "PO-1001", "NOPE", 9)          # 无此项目 → 范围外
    stray_susp = add_susp("ACME", "PO-1001", "NOPE", "不适用挂起")  # 不适用 → 范围外
    crafted["clean"] = {"doc_row_ids": [d1], "items": {"WIDGET": i1, "GADGET": i2},
                        "arrivals": [a1, a2, a3], "stray_arrival": stray_arr,
                        "stray_suspension": stray_susp}

    # --- ACME/PO-1002：三重号 + 重复项目行（同项目号两行），检验扇出不乘量、共享只交一份
    d2 = [add_doc("ACME", "PO-1002") for _ in range(3)]
    i3 = add_item("ACME", "PO-1002", "BOLT", 8)
    i4 = add_item("ACME", "PO-1002", "BOLT", 8)
    a4 = add_arr("ACME", "PO-1002", "BOLT", 8)
    crafted["dup_items"] = {"doc_row_ids": d2, "item_row_ids": [i3, i4], "arrival": a4}

    # --- ACME/PO-1003：重号 + 空单 + 整单挂起（互不掩盖）
    d3 = [add_doc("ACME", "PO-1003") for _ in range(2)]
    s1 = add_susp("ACME", "PO-1003", None, "quality hold #7")
    crafted["empty_suspended"] = {"doc_row_ids": d3, "suspension": s1}

    # --- GLOBEX/PO-2001：空字符串项目号（不是空值）；"" 挂起只命中 "" 项目
    d4 = add_doc("GLOBEX", "PO-2001")
    i5 = add_item("GLOBEX", "PO-2001", "", 2)
    i6 = add_item("GLOBEX", "PO-2001", "X", 1)
    a5 = add_arr("GLOBEX", "PO-2001", "", 5)                    # "" 项目超到
    s2 = add_susp("GLOBEX", "PO-2001", "", "empty-string hold")  # 仅挂起 "" 项目
    s3 = add_susp("GLOBEX", "PO-2001", "ZZZ", "not applicable")  # 不适用 → 范围外
    crafted["empty_string"] = {"doc_row_ids": [d4], "items": {"": i5, "X": i6},
                               "arrival": a5, "suspension_empty": s2, "stray_suspension": s3}

    # --- INITECH/PO-3001：必要到货行受限 → 整案拒发（类别 legal-hold）
    d5 = add_doc("INITECH", "PO-3001")
    i7 = add_item("INITECH", "PO-3001", "VALVE", 3)
    a6 = add_arr("INITECH", "PO-3001", "VALVE", 3, permit=False, restriction="legal-hold")
    crafted["refused_arrival"] = {"doc_row_ids": [d5], "item": i7, "restricted_arrival": a6}

    # --- INITECH/PO-3002：必要挂起行受限 → 整案拒发（类别 privacy）
    d6 = add_doc("INITECH", "PO-3002")
    i8 = add_item("INITECH", "PO-3002", "SEAL", 1)
    s4 = add_susp("INITECH", "PO-3002", "SEAL", "do not disclose",
                  permit=False, restriction="privacy")
    crafted["refused_suspension"] = {"doc_row_ids": [d6], "item": i8, "restricted_suspension": s4}

    # --- Umbrella/PO-4001：独立干净案（验证拒发不影响独立案交付）
    d7 = add_doc("Umbrella", "PO-4001")
    i9 = add_item("Umbrella", "PO-4001", "ARM", 4)
    a7 = add_arr("Umbrella", "PO-4001", "ARM", 2)
    a8 = add_arr("Umbrella", "PO-4001", "ARM", 2)
    crafted["independent"] = {"doc_row_ids": [d7], "item": i9, "arrivals": [a7, a8]}

    # --- Umbrella/PO-4002：两个请求ID点名同键不同物理行 → 共享原行只交一份
    d8a = add_doc("Umbrella", "PO-4002")
    d8b = add_doc("Umbrella", "PO-4002")
    i10 = add_item("Umbrella", "PO-4002", "LEG", 1)
    a9 = add_arr("Umbrella", "PO-4002", "LEG", 1)
    crafted["shared"] = {"doc_row_ids": [d8a, d8b], "item": i10, "arrival": a9}

    # --- STARK/PO-9000：热点键（大量重号单据/项目/到货），检验索引化切片
    hot_doc_ids = [add_doc("STARK", "PO-9000") for _ in range(hot_docs)]
    for j in range(hot_items):
        add_item("STARK", "PO-9000", f"H{j:05d}", rng.randint(0, 5))
    for _ in range(hot_arrivals):
        add_arr("STARK", "PO-9000", f"H{rng.randrange(hot_items):05d}", rng.randint(0, 3))
    crafted["hot"] = {"doc_row_ids": hot_doc_ids, "item_count": hot_items,
                      "arrival_count": hot_arrivals}

    # --- 随机填充（模拟大表；键域与构造案例不相交）
    for _ in range(filler_docs):
        sup = rng.choice(["ACME", "GLOBEX", "INITECH", "Umbrella", "Wayne"])
        no = f"PO-{rng.randint(5000, 8999)}"
        add_doc(sup, no)
        for _ in range(rng.randint(0, 4)):
            ino = rng.choice(["A", "B", "C", ""])
            add_item(sup, no, ino, rng.randint(0, 9))
            for _ in range(rng.randint(0, 3)):
                add_arr(sup, no, ino, rng.randint(0, 4))
        if rng.random() < 0.2:
            add_susp(sup, no, rng.choice([None, "A", "B", ""]), "random hold")
    # 少量受限填充行（均不被点名，不得入包）
    for r in rng.sample(docs, min(5, len(docs))):
        if r["doc_no"] not in ("PO-1001", "PO-1002", "PO-1003", "PO-2001",
                               "PO-3001", "PO-3002", "PO-4001", "PO-4002", "PO-9000"):
            r["permit"] = False
            r["restriction"] = "bulk-restricted"

    os.makedirs(out_dir, exist_ok=True)
    for name, rows in (("documents", docs), ("items", items),
                       ("arrivals", arrivals), ("suspensions", susps)):
        with open(os.path.join(out_dir, f"{name}.jsonl"), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "version.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": version}, fh, ensure_ascii=False)
        fh.write("\n")

    requests = [
        {"request_id": "REQ-A", "doc_row_id": d1},
        {"request_id": "REQ-B", "doc_row_id": d2[0]},
        {"request_id": "REQ-C", "doc_row_id": d3[0]},
        {"request_id": "REQ-D", "doc_row_id": d4},
        {"request_id": "REQ-E", "doc_row_id": d5},
        {"request_id": "REQ-F", "doc_row_id": d6},
        {"request_id": "REQ-U", "doc_row_id": d7},
        {"request_id": "REQ-G1", "doc_row_id": d8a},
        {"request_id": "REQ-G2", "doc_row_id": d8b},
        {"request_id": "REQ-HOT", "doc_row_id": hot_doc_ids[0]},
        {"request_id": "REQ-UNKNOWN", "doc_row_id": "D-999999"},
    ]
    with open(os.path.join(out_dir, "requests.json"), "w", encoding="utf-8") as fh:
        json.dump(requests, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "crafted.json"), "w", encoding="utf-8") as fh:
        json.dump(crafted, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    return {
        "out_dir": out_dir,
        "counts": {"documents": len(docs), "items": len(items),
                   "arrivals": len(arrivals), "suspensions": len(susps)},
        "requests": len(requests),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m po_audit.fixtures",
        description="生成测试用完整仓与请求清单（确定性）",
    )
    ap.add_argument("--out", required=True, help="输出目录（须不存在或为空）")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--filler-docs", type=int, default=200)
    ap.add_argument("--hot-docs", type=int, default=300)
    ap.add_argument("--hot-items", type=int, default=2000)
    ap.add_argument("--hot-arrivals", type=int, default=20000)
    ap.add_argument("--version", default="v2026-11-24")
    args = ap.parse_args(argv)
    if os.path.exists(args.out) and os.listdir(args.out):
        raise SystemExit(f"输出目录非空: {args.out}")
    summary = generate(args.out, seed=args.seed, filler_docs=args.filler_docs,
                       hot_docs=args.hot_docs, hot_items=args.hot_items,
                       hot_arrivals=args.hot_arrivals, version=args.version)
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
