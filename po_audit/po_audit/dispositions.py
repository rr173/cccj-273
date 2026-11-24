"""差异处置清单：从完整仓核账结果形成跨快照待处理项，并跟踪处置状态。

复核员原先只能逐单生成核账报告；本模块在完整仓（可信侧）之上维护一份持久化
处置清单，复用与仓侧/离线相同的 ``compute_report``，不改变原有逐单报告与离线
复算行为。

标识与生命周期
==============

* 自然键：单据级差异为 ``(supplier, doc_no, kind)``；项目级差异为
  ``(supplier, doc_no, item_row_id, kind)``。项目级以**物理行ID**定位，重号
  项目行各自独立，一个项目的事实变化不会重置同单其他项目的处置。
* ``discrepancy_id`` 由自然键规范哈希确定性生成，跨仓快照稳定，因此同一差异
  在后续快照中可被识别为同一项，重复刷新不会重复生成处置项。
* 每项带 ``fact_fingerprint``（相关核账事实的规范 SHA-256）：
    - 指纹不变  → 处置状态/说明原样保留；
    - 指纹变化  → 重新置为 open，旧状态/说明/事实进入 events 历史（仍可查）；
    - 差异消失  → active=false、移出当前清单，ledger 与历史保留；
    - 差异再现  → 同ID复活为 open，全部历史事件保留。

差异种类（与报告事实对应，各项互不掩盖、可并存）
================================================

* 单据级：duplicate_doc_number / empty_document / document_suspended
* 项目级：short / over（按 fulfillment）/ item_suspended

状态文件（JSON）格式 ``po-audit-dispositions/1``，写入采用临时文件原子替换。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass

from . import DISPOSITION_FORMAT, model
from .report import compute_report
from .warehouse import Warehouse

# 差异种类
DOCUMENT_LEVEL_KINDS = ("duplicate_doc_number", "empty_document", "document_suspended")
ITEM_FULFILLMENT_KINDS = ("short", "over")
ITEM_SUSPENDED_KIND = "item_suspended"
ALL_KINDS = DOCUMENT_LEVEL_KINDS + ITEM_FULFILLMENT_KINDS + (ITEM_SUSPENDED_KIND,)

# 处置状态词汇
STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"
ALL_STATUSES = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_DISMISSED)

# 退出码（沿用 offline 的 0/10..13 区间之外的新码段，不改动既有约定）
EXIT_OK = 0
EXIT_STATE_ERROR = 20
EXIT_UNKNOWN_DISCREPANCY = 21
EXIT_INVALID_STATUS = 22
EXIT_UNKNOWN_DOC = 23


class DispositionError(ValueError):
    """处置清单状态或参数非法。"""


class UnknownDiscrepancyError(DispositionError):
    """引用的差异ID在 ledger 中不存在。"""


class InactiveDiscrepancyError(DispositionError):
    """差异已移出当前清单，不能再登记处置。"""


class InvalidStatusError(DispositionError):
    """处置状态不在受控词汇内。"""


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(obj) -> str:
    return hashlib.sha256(model.canonical_bytes(obj)).hexdigest()


def _natural_key(kind: str, doc_key: tuple, item_row_id: str | None) -> dict:
    return {
        "kind": kind,
        "supplier": doc_key[0],
        "doc_no": doc_key[1],
        "item_row_id": item_row_id,  # null 表示单据级差异
    }


def discrepancy_id_for(kind: str, doc_key: tuple, item_row_id: str | None) -> str:
    """自然键 → 确定性差异ID（跨快照稳定）。"""
    return "DSP-" + _sha256_hex(_natural_key(kind, doc_key, item_row_id))[:16]


def _fingerprint(kind: str, facts: dict) -> str:
    return _sha256_hex({"kind": kind, "facts": facts})


@dataclass
class ExtractedDiscrepancy:
    kind: str
    item_row_id: str | None
    item_no: str | None
    facts: dict

    @property
    def discrepancy_id(self):
        return discrepancy_id_for(self.kind, self._doc_key, self.item_row_id)

    def bind(self, doc_key: tuple):
        self._doc_key = doc_key
        return self


def extract_discrepancies(report: dict) -> list:
    """从一份 compute_report 结果抽取全部差异（互不掩盖，可并存）。"""
    doc_key = (report["doc_key"]["supplier"], report["doc_key"]["doc_no"])
    out: list = []

    dup = report["duplicate_doc_number"]
    if dup["is_duplicate"]:
        out.append(ExtractedDiscrepancy(
            "duplicate_doc_number", None, None,
            {"doc_row_ids": list(dup["doc_row_ids"])},
        ).bind(doc_key))
    if report["empty_document"]:
        out.append(ExtractedDiscrepancy(
            "empty_document", None, None, {"empty": True},
        ).bind(doc_key))
    if report["document_suspended"]:
        whole_ids = sorted(
            s["susp_row_id"] for s in report["suspensions"] if s["scope"] == "document"
        )
        out.append(ExtractedDiscrepancy(
            "document_suspended", None, None,
            {"suspension_row_ids": whole_ids},
        ).bind(doc_key))

    for it in report["items"]:
        if it["fulfillment"] in ITEM_FULFILLMENT_KINDS:
            out.append(ExtractedDiscrepancy(
                it["fulfillment"], it["item_row_id"], it["item_no"],
                {
                    "expected_qty": it["expected_qty"],
                    "arrived_qty": it["arrived_qty"],
                    "arrival_row_ids": list(it["arrival_row_ids"]),
                },
            ).bind(doc_key))
        if it["suspended"]:
            out.append(ExtractedDiscrepancy(
                ITEM_SUSPENDED_KIND, it["item_row_id"], it["item_no"],
                {"suspension_row_ids": list(it["suspension_row_ids"])},
            ).bind(doc_key))
    return out


class DispositionStore:
    """处置清单持久化状态。"""

    def __init__(self):
        self.ledger: dict = {}          # discrepancy_id -> entry
        self.scanned_doc_row_ids: set = set()
        self.snapshots: dict = {}       # warehouse_version -> 快照刷新记录

    # ---------- 持久化 ----------

    @classmethod
    def load(cls, path: str) -> "DispositionStore":
        if not os.path.isfile(path):
            raise DispositionError(f"处置清单状态文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as e:
            raise DispositionError(f"状态文件解析失败: {e}") from e
        cls._validate(data)
        store = cls()
        store.ledger = {e["discrepancy_id"]: e for e in data["ledger"]}
        store.scanned_doc_row_ids = set(data["scanned_doc_row_ids"])
        store.snapshots = {s["warehouse_version"]: s for s in data["snapshots"]}
        return store

    @classmethod
    def new(cls) -> "DispositionStore":
        return cls()

    @staticmethod
    def _validate(data: dict) -> None:
        if not isinstance(data, dict) or data.get("format") != DISPOSITION_FORMAT:
            raise DispositionError(f"不支持的处置清单格式: {data.get('format')!r}")
        if not isinstance(data.get("ledger"), list):
            raise DispositionError("状态文件缺 ledger")
        if not isinstance(data.get("scanned_doc_row_ids"), list):
            raise DispositionError("状态文件缺 scanned_doc_row_ids")
        if not isinstance(data.get("snapshots"), list):
            raise DispositionError("状态文件缺 snapshots")
        seen = set()
        for e in data["ledger"]:
            did = e.get("discrepancy_id")
            if not isinstance(did, str) or not did:
                raise DispositionError("ledger 条目缺 discrepancy_id")
            if did in seen:
                raise DispositionError(f"ledger 中 discrepancy_id 重复: {did}")
            seen.add(did)
            if e.get("kind") not in ALL_KINDS:
                raise DispositionError(f"{did}: 未知差异种类 {e.get('kind')!r}")
            if e.get("status") not in ALL_STATUSES:
                raise DispositionError(f"{did}: 未知处置状态 {e.get('status')!r}")
            if not isinstance(e.get("events"), list):
                raise DispositionError(f"{did}: 缺 events 历史")

    def save(self, path: str) -> None:
        data = {
            "format": DISPOSITION_FORMAT,
            "scanned_doc_row_ids": sorted(self.scanned_doc_row_ids),
            "snapshots": [self.snapshots[v] for v in sorted(self.snapshots)],
            "ledger": [self.ledger[k] for k in sorted(self.ledger)],
        }
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{os.path.basename(path)}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)

    # ---------- 视图 ----------

    def active_entries(self, status: str | None = None,
                       doc_row_id: str | None = None) -> list:
        """当前清单：active 条目（差异消失项不在此视图）。"""
        out = []
        for e in self.ledger.values():
            if not e["active"]:
                continue
            if status is not None and e["status"] != status:
                continue
            if doc_row_id is not None and doc_row_id not in e["doc_row_ids"]:
                continue
            out.append(e)
        return sorted(out, key=_entry_sort_key)

    def all_entries(self) -> list:
        """完整 ledger（含已移除/再开的历史项）。"""
        return sorted(self.ledger.values(), key=_entry_sort_key)

    def get(self, discrepancy_id: str) -> dict:
        try:
            return self.ledger[discrepancy_id]
        except KeyError:
            raise UnknownDiscrepancyError(
                f"差异项不存在: {discrepancy_id!r}") from None

    # ---------- 变更 ----------

    def _append_event(self, entry: dict, etype: str, snapshot: str, now: str,
                      from_status: str | None = None, from_note: str | None = None) -> None:
        ev = {
            "type": etype,
            "at": now,
            "snapshot": snapshot,
            "status": entry["status"],
            "note": entry["note"],
            "fact_fingerprint": entry["fact_fingerprint"],
            "facts": json.loads(json.dumps(entry["facts"], ensure_ascii=False)),
        }
        if from_status is not None:
            # 仅处置变更事件携带"变更前"状态，便于审计处置轨迹
            ev["from_status"] = from_status
            ev["from_note"] = from_note
        entry["events"].append(ev)

    def record_disposition(self, discrepancy_id: str, status: str, note: str,
                           now: str | None = None) -> dict:
        if status not in ALL_STATUSES:
            raise InvalidStatusError(
                f"非法处置状态 {status!r}；可选: {', '.join(ALL_STATUSES)}")
        if not isinstance(note, str):
            raise InvalidStatusError("处置说明必须是字符串")
        entry = self.get(discrepancy_id)
        if not entry["active"]:
            raise InactiveDiscrepancyError(
                f"{discrepancy_id}: 差异已不在当前清单（参见 history），不可登记处置")
        now = now or _now_iso()
        if entry["status"] != status or entry["note"] != note:
            old_status, old_note = entry["status"], entry["note"]
            entry["status"] = status
            entry["note"] = note
            entry["updated_at"] = now
            # 事件记录新登记的处置（历史可查的"旧处置"），并保留变更前状态
            self._append_event(entry, "disposition", entry["last_seen_snapshot"], now,
                               from_status=old_status, from_note=old_note)
        return entry


def _entry_sort_key(e: dict):
    return (e["doc_key"]["supplier"], e["doc_key"]["doc_no"],
            e.get("item_row_id") or "", e["kind"])


def refresh(store: DispositionStore, warehouse: Warehouse, doc_row_ids,
            now: str | None = None) -> dict:
    """用一个完整仓快照刷新处置清单。

    对每个点名单据物理ID重算核账报告（共用 compute_report），按自然键匹配
    既有处置项；同一快照重复刷新幂等（不新增项、不重置处置、不追加事件）。

    返回刷新摘要：created / reopened / removed / reappeared / unchanged /
    unknown_doc_row_ids / missing_doc_row_ids。
    """
    now = now or _now_iso()
    version = warehouse.version
    summary = {
        "warehouse_version": version,
        "created": [], "reopened": [], "removed": [], "reappeared": [],
        "unchanged": [], "unknown_doc_row_ids": [], "missing_doc_row_ids": [],
    }

    # 同单键只重算一次（多个点名物理ID可能共享单键）
    keys_seen: dict = {}          # doc_key -> 任一 doc_row_id
    unknown = []
    for did in doc_row_ids:
        if not isinstance(did, str) or not did:
            raise DispositionError("doc_row_id 必须是非空字符串")
        doc = warehouse.docs_by_id.get(did)
        if doc is None:
            # 历史点名过、本快照消失的单据归入 missing（见下），不算未知请求；
            # 只有从未在任何已刷新快照中出现过的ID才算 unknown
            if did not in store.scanned_doc_row_ids:
                unknown.append(did)
            continue
        keys_seen.setdefault(model.doc_key_of(doc), did)
        store.scanned_doc_row_ids.add(did)
    summary["unknown_doc_row_ids"] = sorted(unknown)

    for doc_key, named_id in keys_seen.items():
        scope = warehouse.compute_scope("__dispositions__", named_id)
        arrivals = [r for _k, rows in scope.arrival_keys for r in rows]
        report = compute_report(named_id, scope.documents, scope.items,
                                arrivals, scope.suspensions)
        extracted = {d.discrepancy_id: d for d in extract_discrepancies(report)}
        key_doc_row_ids = sorted(d["doc_row_id"] for d in scope.documents)

        existing_for_key = [
            e for e in store.ledger.values()
            if (e["doc_key"]["supplier"], e["doc_key"]["doc_no"]) == doc_key
        ]
        seen_ids = set()

        for did_x, ex in extracted.items():
            seen_ids.add(did_x)
            fp = _fingerprint(ex.kind, ex.facts)
            old = store.ledger.get(did_x)
            if old is None:
                entry = _new_entry(did_x, ex, doc_key, key_doc_row_ids, version, fp, now)
                store.ledger[did_x] = entry
                summary["created"].append(did_x)
                continue

            old["last_seen_snapshot"] = version
            old["doc_row_ids"] = key_doc_row_ids
            if not old["active"]:
                # 差异在消失后再次出现：同ID复活，历史全部保留
                old["active"] = True
                old["removed_snapshot"] = None
                old["fact_fingerprint"] = fp
                old["facts"] = json.loads(json.dumps(ex.facts, ensure_ascii=False))
                old["status"] = STATUS_OPEN
                old["note"] = ""
                old["updated_at"] = now
                store._append_event(old, "reappeared", version, now)
                summary["reappeared"].append(did_x)
            elif old["fact_fingerprint"] != fp:
                # 相关核账事实变化：旧处置归档，重新待处理（同单其他项不受影响）
                store._append_event(old, "reopened", version, now)
                old["fact_fingerprint"] = fp
                old["facts"] = json.loads(json.dumps(ex.facts, ensure_ascii=False))
                old["status"] = STATUS_OPEN
                old["note"] = ""
                old["updated_at"] = now
                summary["reopened"].append(did_x)
            else:
                # 事实未变：处置原样保留，幂等刷新无任何写入性变化
                summary["unchanged"].append(did_x)

        # 该单键下本次未出现的 active 差异 → 差异不再存在，移出当前清单
        for old in existing_for_key:
            if old["active"] and old["discrepancy_id"] not in seen_ids:
                old["active"] = False
                old["removed_snapshot"] = version
                old["updated_at"] = now
                store._append_event(old, "removed", version, now)
                summary["removed"].append(old["discrepancy_id"])

    # 点名ID在本快照中已不存在（单据被删除）：保留原处置，摘要提示
    present_ids = set(warehouse.docs_by_id)
    for did in sorted(store.scanned_doc_row_ids):
        if did not in present_ids:
            summary["missing_doc_row_ids"].append(did)

    snap_rec = store.snapshots.get(version)
    if snap_rec is None:
        store.snapshots[version] = {
            "warehouse_version": version,
            "first_refreshed_at": now,
            "last_refreshed_at": now,
            "refresh_count": 1,
            "scanned_doc_row_ids": sorted(store.scanned_doc_row_ids),
        }
    else:
        snap_rec["last_refreshed_at"] = now
        snap_rec["refresh_count"] += 1
        snap_rec["scanned_doc_row_ids"] = sorted(store.scanned_doc_row_ids)

    for k in ("created", "reopened", "removed", "reappeared", "unchanged"):
        summary[k] = sorted(summary[k])
    summary["counts"] = {k: len(summary[k]) for k in
                         ("created", "reopened", "removed", "reappeared", "unchanged")}
    return summary


def _new_entry(did_x: str, ex: "ExtractedDiscrepancy", doc_key: tuple,
               key_doc_row_ids, version: str, fp: str, now: str) -> dict:
    entry = {
        "discrepancy_id": did_x,
        "kind": ex.kind,
        "doc_key": {"supplier": doc_key[0], "doc_no": doc_key[1]},
        "doc_row_ids": list(key_doc_row_ids),
        "item_row_id": ex.item_row_id,
        "item_no": ex.item_no,
        "first_seen_snapshot": version,
        "last_seen_snapshot": version,
        "removed_snapshot": None,
        "active": True,
        "status": STATUS_OPEN,
        "note": "",
        "fact_fingerprint": fp,
        "facts": json.loads(json.dumps(ex.facts, ensure_ascii=False)),
        "created_at": now,
        "updated_at": now,
        "events": [],
    }
    entry["events"].append({
        "type": "created",
        "at": now,
        "snapshot": version,
        "status": STATUS_OPEN,
        "note": "",
        "fact_fingerprint": fp,
        "facts": json.loads(json.dumps(ex.facts, ensure_ascii=False)),
    })
    return entry


# ---------------- CLI ----------------

def _emit(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _load_doc_row_ids(args) -> list:
    ids = list(args.doc_row_id or [])
    if args.requests:
        with open(args.requests, "r", encoding="utf-8") as fh:
            reqs = json.load(fh)
        if not isinstance(reqs, list):
            raise DispositionError("请求文件必须是 JSON 数组")
        for r in reqs:
            if not isinstance(r, dict) or not isinstance(r.get("doc_row_id"), str):
                raise DispositionError("请求条目须含 doc_row_id 字符串")
            ids.append(r["doc_row_id"])
    if not ids:
        raise DispositionError("须用 --doc-row-id 或 --requests 指定至少一个单据物理ID")
    # 去重保序
    seen = set()
    uniq = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _entry_view(e: dict) -> dict:
    return {k: e[k] for k in (
        "discrepancy_id", "kind", "doc_key", "doc_row_ids", "item_row_id",
        "item_no", "active", "status", "note", "facts", "fact_fingerprint",
        "first_seen_snapshot", "last_seen_snapshot", "removed_snapshot",
        "created_at", "updated_at",
    )}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m po_audit.dispositions",
        description="差异处置清单：跨仓快照跟踪核账差异的处置状态与历史",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ref = sub.add_parser("refresh", help="用完整仓快照刷新处置清单")
    p_ref.add_argument("--warehouse", required=True)
    p_ref.add_argument("--state", required=True, help="处置清单状态文件路径")
    p_ref.add_argument("--doc-row-id", action="append", help="可重复")
    p_ref.add_argument("--requests", help="请求文件 JSON（同 slicer 格式）")

    p_list = sub.add_parser("list", help="列出当前清单（默认仅 active）")
    p_list.add_argument("--state", required=True)
    p_list.add_argument("--all", action="store_true", help="含已移除的历史项")
    p_list.add_argument("--status", choices=ALL_STATUSES)
    p_list.add_argument("--doc-row-id")

    p_note = sub.add_parser("note", help="登记处置状态与说明")
    p_note.add_argument("--state", required=True)
    p_note.add_argument("--discrepancy-id", required=True)
    p_note.add_argument("--status", required=True,
                        help=f"处置状态: {', '.join(ALL_STATUSES)}")
    p_note.add_argument("--note", default="")

    p_hist = sub.add_parser("history", help="查看历史（含旧处置、再开与移除记录）")
    p_hist.add_argument("--state", required=True)
    p_hist.add_argument("--discrepancy-id")

    p_show = sub.add_parser("show", help="查看单个差异项完整记录")
    p_show.add_argument("--state", required=True)
    p_show.add_argument("--discrepancy-id", required=True)

    args = ap.parse_args(argv)

    try:
        if args.cmd == "refresh":
            wh = Warehouse.load(args.warehouse)
            ids = _load_doc_row_ids(args)
            if os.path.isfile(args.state):
                store = DispositionStore.load(args.state)
            else:
                store = DispositionStore.new()
            result = refresh(store, wh, ids)
            store.save(args.state)
            _emit(result)
            if result["unknown_doc_row_ids"]:
                return EXIT_UNKNOWN_DOC
            return EXIT_OK

        store = DispositionStore.load(args.state)

        if args.cmd == "list":
            if args.all:
                entries = store.all_entries()
                if args.status is not None:
                    entries = [e for e in entries if e["status"] == args.status]
                if args.doc_row_id is not None:
                    entries = [e for e in entries
                               if args.doc_row_id in e["doc_row_ids"]]
            else:
                entries = store.active_entries(args.status, args.doc_row_id)
            _emit({
                "format": DISPOSITION_FORMAT,
                "total": len(entries),
                "discrepancies": [_entry_view(e) for e in entries],
            })
            return EXIT_OK

        if args.cmd == "note":
            e = store.record_disposition(args.discrepancy_id, args.status, args.note)
            store.save(args.state)
            _emit(_entry_view(e))
            return EXIT_OK

        if args.cmd == "show":
            _emit(store.get(args.discrepancy_id))
            return EXIT_OK

        if args.cmd == "history":
            if args.discrepancy_id:
                e = store.get(args.discrepancy_id)
                _emit({"discrepancy_id": e["discrepancy_id"], "events": e["events"]})
            else:
                _emit({
                    "discrepancies": [
                        {"discrepancy_id": e["discrepancy_id"],
                         "kind": e["kind"], "active": e["active"],
                         "events": e["events"]}
                        for e in store.all_entries()
                    ]
                })
            return EXIT_OK
    except InactiveDiscrepancyError as e:
        print(f"处置清单错误: {e}", file=sys.stderr)
        return EXIT_UNKNOWN_DISCREPANCY
    except UnknownDiscrepancyError as e:
        print(f"处置清单错误: {e}", file=sys.stderr)
        return EXIT_UNKNOWN_DISCREPANCY
    except InvalidStatusError as e:
        print(f"处置清单错误: {e}", file=sys.stderr)
        return EXIT_INVALID_STATUS
    except DispositionError as e:
        print(f"处置清单错误: {e}", file=sys.stderr)
        return EXIT_STATE_ERROR
    except Exception as e:
        # WarehouseError 等仓装载错误归入状态/输入错误码段
        print(f"错误: {e}", file=sys.stderr)
        return EXIT_STATE_ERROR

    return EXIT_STATE_ERROR


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
