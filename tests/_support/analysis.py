"""Small source project shared by analysis contract tests."""

from pathlib import Path


# 共享样例保留服务到仓储的调用链，便于跨阶段验证同一组源码证据。
def sample_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "orders.py").write_text(
        """
class OrderRepository:
    def save(self, order):
        return order

class OrderService:
    def __init__(self):
        self.repository = OrderRepository()

    def place(self, order):
        return self.repository.save(order)
""".lstrip(),
        encoding="utf-8",
    )
    return root


# 测试通过内容摘要定位具体包，避免误读其他分析批次的产物。
def bundle_path(root: Path, digest: str) -> Path:
    return root / "modus/.cache/init-analysis/bundles" / digest
