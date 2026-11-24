"""离线复算入口：检验交付包完整性，并从切片原行独立重算复核报告。

保证：
  * 缺文件、文件被改、行哈希不符、清单与内容不一致 → 拒绝（退出码 10）。
  * 越界查询（清单中不存在的请求ID）→ 拒绝（退出码 11）。
  * 报告完全由包内原行重算，不信任任何预算结果。
  * 范围内"确实无行"的查询以清单 empty 声明为准，并与包内容交叉核对。

退出码：0 成功；10 完整性失败；11 越界查询；12 案件被拒发；13 请求的单据物理ID在仓中不存在。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys

from . import PACKAGE_FORMAT, model
from .report import compute_report

EXIT_OK = 0
EXIT_INTEGRITY = 10
EXIT_OUT_OF_SCOPE = 11
EXIT_REFUSED = 12
EXIT_UNKNOWN_REQUEST = 13


class IntegrityError(ValueError):
    """交付包完整性或一致性检验失败。"""


class OutOfScopeError(ValueError):
    """查询超出仓主范围清单。"""


class Package:
    def __init__(self, root, manifest, rows):
        self.root = root
        self.manifest = manifest
        self.rows = rows  # table -> {物理行ID: row}
        self._cases = {c["request_id"]: c for c in manifest["cases"]}

    def case(self, request_id):
        try:
            return self._cases[request_id]
        except KeyError:
            raise OutOfScopeError(f"请求不在范围清单内: {request_id!r}") from None

    def case_rows(self, case):
        """按清单查询取回该案全部行：table -> [row]。"""
        out = {t: [] for t in model.TABLES}
        for q in case["queries"]:
            for rid in q["row_ids"]:
                out[q["table"]].append(self.rows[q["table"]][rid])
        return out


def _check(cond, msg):
    if not cond:
        raise IntegrityError(msg)


def _load_manifest(root):
    mpath = os.path.join(root, "manifest.json")
    if not os.path.isfile(mpath):
        raise IntegrityError(f"缺文件: {mpath}")
    try:
        with open(mpath, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except json.JSONDecodeError as e:
        raise IntegrityError(f"manifest.json 解析失败: {e}") from e
    _check(isinstance(manifest, dict), "manifest 必须是 JSON 对象")
    _check(manifest.get("format") == PACKAGE_FORMAT, f"不支持的包格式: {manifest.get('format')!r}")
    return manifest


def _verify_signature(manifest, hmac_key):
    sig = manifest.get("signature")
    if sig is None:
        _check(hmac_key is None, "范围清单未签名，但提供了密钥")
        return
    _check(hmac_key is not None, "范围清单已签名，必须提供 --hmac-key-file 验签")
    _check(isinstance(sig, dict) and sig.get("algo") == "HMAC-SHA256", "不支持的签名算法")
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    expect = hmac.new(hmac_key, model.canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    _check(hmac.compare_digest(expect, str(sig.get("hex", ""))), "范围清单签名验签失败")


def _query_matches(query, row, case_item_nos):
    """行是否落在该查询的键范围内（挂起查询含适用性判定）。"""
    key = query["key"]
    t = query["table"]
    if row["supplier"] != key["supplier"] or row["doc_no"] != key["doc_no"]:
        return False
    if t in ("documents", "items"):
        return True
    if t == "arrivals":
        return row["item_no"] == key["item_no"]
    # suspensions：整单挂起（null）或命中本案项目号
    return row["item_no"] is None or row["item_no"] in case_item_nos


def _build_indexes(rows):
    """对包内行一次性建索引（每表一遍），查询核对只做索引定位，不逐查询重扫。"""
    idx = {
        "documents": {}, "items": {}, "arrivals": {}, "suspensions": {},
    }
    for rid, r in rows["documents"].items():
        idx["documents"].setdefault(model.doc_key_of(r), set()).add(rid)
    for rid, r in rows["items"].items():
        idx["items"].setdefault(model.doc_key_of(r), set()).add(rid)
    for rid, r in rows["arrivals"].items():
        idx["arrivals"].setdefault(model.item_key_of(r), set()).add(rid)
    for rid, r in rows["suspensions"].items():
        idx["suspensions"].setdefault(model.doc_key_of(r), set()).add(rid)
    return idx


def _verify_cases(manifest, rows):
    referenced = {t: set() for t in model.TABLES}
    idx = _build_indexes(rows)
    seen_req = set()

    def matching_ids(table, key, case_item_nos):
        """包内落在该查询键范围内的行ID集合（索引定位）。"""
        if table == "arrivals":
            k = (key["supplier"], key["doc_no"], key["item_no"])
            return set(idx["arrivals"].get(k, ()))
        k = (key["supplier"], key["doc_no"])
        ids = idx[table].get(k, ())
        if table == "suspensions":
            return {
                rid for rid in ids
                if rows["suspensions"][rid]["item_no"] is None
                or rows["suspensions"][rid]["item_no"] in case_item_nos
            }
        return set(ids)

    for case in manifest["cases"]:
        _check(isinstance(case, dict), "案件条目必须是对象")
        rid_req = case.get("request_id")
        _check(isinstance(rid_req, str) and rid_req, "案件缺 request_id")
        _check(rid_req not in seen_req, f"request_id 重复: {rid_req}")
        seen_req.add(rid_req)
        status = case.get("status")

        if status == "refused":
            # 拒发条目只许含 请求ID/状态/受限类别，不得泄露行身份、数量或内容
            _check(set(case.keys()) == {"request_id", "status", "restricted_categories"},
                   f"{rid_req}: 拒发条目含多余字段")
            cats = case["restricted_categories"]
            _check(isinstance(cats, list) and cats and
                   all(isinstance(c, str) and c for c in cats),
                   f"{rid_req}: restricted_categories 必须是非空类别列表")
            continue
        if status == "unknown_request":
            _check(set(case.keys()) == {"request_id", "status"},
                   f"{rid_req}: unknown_request 条目含多余字段")
            continue
        _check(status == "delivered", f"{rid_req}: 未知状态 {status!r}")

        doc_key = case.get("doc_key")
        _check(isinstance(doc_key, dict) and
               isinstance(doc_key.get("supplier"), str) and isinstance(doc_key.get("doc_no"), str),
               f"{rid_req}: doc_key 非法")
        queries = case.get("queries")
        _check(isinstance(queries, list), f"{rid_req}: 缺 queries")

        by_table = {}
        for q in queries:
            _check(isinstance(q, dict), f"{rid_req}: 查询条目必须是对象")
            _check(set(q.keys()) == {"table", "key", "row_ids", "empty"},
                   f"{rid_req}: 查询条目字段异常")
            _check(q["table"] in model.TABLES, f"{rid_req}: 未知表 {q['table']!r}")
            _check(isinstance(q["key"], dict), f"{rid_req}: 查询缺 key")
            _check(isinstance(q["row_ids"], list) and
                   all(isinstance(x, str) for x in q["row_ids"]),
                   f"{rid_req}: row_ids 必须是字符串数组")
            _check(q["empty"] is (len(q["row_ids"]) == 0),
                   f"{rid_req}: empty 标记与 row_ids 不一致")
            by_table.setdefault(q["table"], []).append(q)

        _check(len(by_table.get("documents", [])) == 1, f"{rid_req}: 单据查询须恰为一条")
        _check(len(by_table.get("items", [])) == 1, f"{rid_req}: 项目查询须恰为一条")
        _check(len(by_table.get("suspensions", [])) == 1, f"{rid_req}: 挂起查询须恰为一条")
        for t in ("documents", "items", "suspensions"):
            _check(by_table[t][0]["key"] == doc_key, f"{rid_req}: {t} 查询键与 doc_key 不符")
        for q in by_table.get("arrivals", []):
            k = q["key"]
            _check(set(k.keys()) == {"supplier", "doc_no", "item_no"} and
                   k["supplier"] == doc_key["supplier"] and k["doc_no"] == doc_key["doc_no"] and
                   isinstance(k["item_no"], str),
                   f"{rid_req}: 到货查询键非法")

        # 本案项目号集合（来自清单所列项目行）
        item_rows = []
        for rid_ in by_table["items"][0]["row_ids"]:
            row = rows["items"].get(rid_)
            _check(row is not None, f"{rid_req}: 清单引用了不存在的项目行 {rid_}")
            item_rows.append(row)
        case_item_nos = {r["item_no"] for r in item_rows}

        # 到货查询必须逐项目键恰有一条（无行的项目键显式声明为空）
        declared_item_keys = {(q["key"]["item_no"]) for q in by_table.get("arrivals", [])}
        _check(declared_item_keys == case_item_nos,
               f"{rid_req}: 到货查询未逐项目键声明（含空声明）")

        # 点名单据物理ID必须在单据查询结果内
        _check(case.get("doc_row_id") in by_table["documents"][0]["row_ids"],
               f"{rid_req}: doc_row_id 不在单据查询结果内")

        # 逐查询核对：所列行存在且匹配查询；包内匹配该查询的行恰为所列（无隐瞒、无夹带）
        for q in queries:
            t = q["table"]
            listed = q["row_ids"]
            _check(len(set(listed)) == len(listed), f"{rid_req}: 查询 {t} 行ID重复")
            for rid_ in listed:
                row = rows[t].get(rid_)
                _check(row is not None, f"{rid_req}: 清单引用了不存在的行 {t}/{rid_}")
                _check(_query_matches(q, row, case_item_nos),
                       f"{rid_req}: 行 {t}/{rid_} 与查询键不符")
            actual = matching_ids(t, q["key"], case_item_nos)
            _check(actual == set(listed),
                   f"{rid_req}: 查询 {t} 声明与包内容不一致（漏报或夹带行）")
            referenced[t].update(listed)

    # 包内每一行都必须被至少一个案件查询引用（无未申报行）
    for t in model.TABLES:
        extra = set(rows[t]) - referenced[t]
        _check(not extra, f"包内存在未被任何案件引用的 {t} 行，如: {sorted(extra)[:3]}")


def load_and_verify(root: str, hmac_key: bytes | None = None) -> Package:
    manifest = _load_manifest(root)
    _verify_signature(manifest, hmac_key)

    files = manifest.get("files")
    row_hashes = manifest.get("row_hashes")
    _check(isinstance(files, dict) and isinstance(row_hashes, dict), "manifest 缺 files/row_hashes")

    rows = {}
    for t in model.TABLES:
        finfo = files.get(t)
        _check(isinstance(finfo, dict), f"manifest.files 缺 {t}")
        rel = finfo.get("path")
        _check(isinstance(rel, str) and rel and rel == os.path.basename(rel),
               f"{t}: 非法文件路径 {rel!r}")
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            raise IntegrityError(f"缺文件: {p}")
        with open(p, "rb") as fh:
            data = fh.read()
        _check(hashlib.sha256(data).hexdigest() == finfo.get("sha256"),
               f"{rel}: 文件哈希不符（内容被改动）")
        try:
            table_rows = model.parse_table_lines(
                data.decode("utf-8").splitlines(), t, rel)
        except model.SchemaError as e:
            raise IntegrityError(str(e)) from e
        _check(len(table_rows) == finfo.get("row_count"), f"{rel}: 行数与清单不符")

        mh = row_hashes.get(t)
        _check(isinstance(mh, dict), f"manifest.row_hashes 缺 {t}")
        ids = {r[model.ID_FIELD[t]] for r in table_rows}
        _check(set(mh.keys()) == ids, f"{t}: row_hashes 与文件行集合不一致")
        for r in table_rows:
            rid = r[model.ID_FIELD[t]]
            _check(model.row_hash(r) == mh[rid], f"{t}/{rid}: 行哈希不符（内容被改动）")
        rows[t] = {r[model.ID_FIELD[t]]: r for r in table_rows}

    cases = manifest.get("cases")
    _check(isinstance(cases, list), "manifest 缺 cases")
    _verify_cases(manifest, rows)
    return Package(root, manifest, rows)


def _read_key(path):
    with open(path, "rb") as fh:
        key = fh.read()
    if not key:
        raise IntegrityError("HMAC 密钥文件为空")
    return key


def _emit(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m po_audit.offline",
        description="离线复算：检验交付包并从切片原行重算复核报告",
    )
    ap.add_argument("--package", required=True, help="交付包目录")
    ap.add_argument("--hmac-key-file", help="仓主密钥文件（清单已签名时必填）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="仅检验完整性")
    p_rep = sub.add_parser("report", help="重算某请求的复核报告")
    p_rep.add_argument("--request", required=True)
    p_rows = sub.add_parser("rows", help="列出某请求范围内的行ID")
    p_rows.add_argument("--request", required=True)
    sub.add_parser("requests", help="列出范围清单中的全部请求")
    args = ap.parse_args(argv)

    key = _read_key(args.hmac_key_file) if args.hmac_key_file else None
    try:
        pkg = load_and_verify(args.package, hmac_key=key)
    except IntegrityError as e:
        print(f"完整性检验失败: {e}", file=sys.stderr)
        return EXIT_INTEGRITY

    if args.cmd == "verify":
        _emit({
            "status": "ok",
            "warehouse_version": pkg.manifest.get("warehouse_version"),
            "cases": len(pkg.manifest["cases"]),
            "rows": {t: len(pkg.rows[t]) for t in model.TABLES},
        })
        return EXIT_OK

    if args.cmd == "requests":
        _emit({"requests": [
            {k: v for k, v in c.items() if k in ("request_id", "status", "restricted_categories")}
            for c in pkg.manifest["cases"]
        ]})
        return EXIT_OK

    # report / rows：越界查询一律拒绝
    try:
        case = pkg.case(args.request)
    except OutOfScopeError as e:
        print(f"越界查询被拒绝: {e}", file=sys.stderr)
        return EXIT_OUT_OF_SCOPE

    if case["status"] == "refused":
        _emit({
            "request_id": case["request_id"],
            "status": "refused",
            "restricted_categories": case["restricted_categories"],
        })
        return EXIT_REFUSED
    if case["status"] == "unknown_request":
        _emit({"request_id": case["request_id"], "status": "unknown_request"})
        return EXIT_UNKNOWN_REQUEST

    if args.cmd == "rows":
        _emit({
            "request_id": case["request_id"],
            "status": "delivered",
            "queries": case["queries"],
            "empty_queries": case["empty_queries"],
        })
        return EXIT_OK

    # report：由包内原行重算
    rows = pkg.case_rows(case)
    report = compute_report(
        case["doc_row_id"],
        rows["documents"], rows["items"], rows["arrivals"], rows["suspensions"],
    )
    report["request_id"] = case["request_id"]
    report["warehouse_version"] = pkg.manifest.get("warehouse_version")
    _emit(report)
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # 输出被管道截断（如 | head）：静默退出
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
