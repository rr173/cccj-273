"""po_audit — 采购单复核切片与离线复算。

从完整仓（同版四表：单据/项目/到货/挂起）按"点名单据物理ID"切出
复核所必需的原行，生成交付包（含仓主范围清单），并提供离线复算入口。
"""

__version__ = "1.1.0"

PACKAGE_FORMAT = "po-audit-package/1"
DISPOSITION_FORMAT = "po-audit-dispositions/1"
