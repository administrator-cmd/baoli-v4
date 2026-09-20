"""检查 app 包内是否存在模块级导入环。

用法（在 backend 下）：

    python script/check_module_cycles.py            # 只检查
    python script/check_module_cycles.py --self-check   # 先用已知环验证检查器本身

为什么需要它：判断「有没有循环依赖」靠肉眼看 import 极易出错。本文件写错过三次
（漏 `from . import x`、漏 `from .pkg import sub`、把 `__init__.py` 记成 `包.__init__`
从而丢掉整条指向包的边），每次都给出过错误的「0 环」。因此：

- 判定用 Tarjan 强连通分量（分量内节点数 > 1 即为环），不依赖 DFS 的 visited 剪枝；
- `--self-check` 会在临时目录里造四类已知环（含经 `__init__.py` 转发这种最容易漏的），
  检查器必须先把它们全部找出来，否则不允许采信检查结果；
- 退出码：0 = 无环；1 = 有环或自检失败。

本脚本是纯标准库，只读源码，不连接数据库、不导入被测代码。
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
import tempfile


def _module_name(path: pathlib.Path, root: pathlib.Path) -> tuple[str, str]:
    """返回 (模块名, 相对导入的基准包)。

    `__init__.py` 代表的是**包本身**：它的模块名去掉 `.__init__`，
    基准包就是它自己（因为包的相对导入是相对该包，不是相对「包 + __init__」）。
    """
    relative = path.relative_to(root.parent)
    dotted = str(relative.with_suffix("")).replace("\\", ".").replace("/", ".")
    if dotted.endswith(".__init__"):
        return dotted[: -len(".__init__")], dotted[: -len(".__init__")]
    return dotted, dotted.rsplit(".", 1)[0]


def build_edges(root: pathlib.Path) -> dict[str, set[str]]:
    """模块级导入图（含函数内导入）。

    三条必须遵守的解析规则：
      1. `from . import x` 的 module 是 None，要展开成 `包路径.x`；
      2. `from .pkg import name` 可能指向子模块 `包路径.pkg.name`，不只是符号；
      3. `__init__.py` 的层级基准是包本身（见 `_module_name`）。
    """
    edges: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        mod, package = _module_name(path, root)
        deps: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom):
                continue
            base = package.split(".")
            if node.level:
                pkg = base[: len(base) - node.level + 1]
                if node.module:
                    deps.add(".".join(pkg + [node.module]))
                    deps.update(".".join(pkg + [node.module, alias.name]) for alias in node.names)
                else:
                    deps.update(".".join(pkg + [alias.name]) for alias in node.names)
            elif node.module and node.module.startswith("app."):
                deps.add(node.module)
        edges[mod] = deps
    return edges


def find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan 强连通分量；分量内节点数 > 1 即为环。"""
    known = set(edges)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = [0]

    def strongconnect(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for neighbour in sorted(edges.get(node, ())):
            if neighbour not in known:
                continue
            if neighbour not in index:
                strongconnect(neighbour)
                low[node] = min(low[node], low[neighbour])
            elif neighbour in on_stack:
                low[node] = min(low[node], index[neighbour])
        if low[node] == index[node]:
            component = []
            while True:
                popped = stack.pop()
                on_stack.discard(popped)
                component.append(popped)
                if popped == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for module in sorted(edges):
        if module not in index:
            strongconnect(module)
    return result


def self_check() -> bool:
    """用四类已知环验证检查器本身，避免「检查器坏了却报 0 环」。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "app"
        root.mkdir()
        (root / "__init__.py").write_text("", encoding="utf-8")
        # 环一：from . import 形式
        (root / "a.py").write_text("from . import b\n", encoding="utf-8")
        (root / "b.py").write_text("from . import a\n", encoding="utf-8")
        # 环二：from .d import f 形式
        (root / "c.py").write_text("from .d import f\n", encoding="utf-8")
        (root / "d.py").write_text("from . import c\n\n\ndef f():\n    return 1\n", encoding="utf-8")
        # 环三：经子包 __init__ 转发（最容易漏）
        sub = root / "sub"
        sub.mkdir()
        (sub / "__init__.py").write_text("from .impl import g\n", encoding="utf-8")
        (sub / "impl.py").write_text("from ..x import h\n\n\ndef g():\n    return 1\n", encoding="utf-8")
        (root / "x.py").write_text("from .sub import g\n\n\ndef h():\n    return 1\n", encoding="utf-8")
        # 无环对照
        (root / "y.py").write_text("import os\n", encoding="utf-8")

        found = find_cycles(build_edges(root))

    expected = 3
    if len(found) != expected:
        print(f"[自检失败] 预期发现 {expected} 个环，实际 {len(found)} 个：检查器不可信，结果作废")
        return False
    print(f"[自检通过] 已知环全部检出（{expected}/{expected}）")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 app 包内的模块级导入环")
    parser.add_argument("--self-check", action="store_true", help="先用已知环验证检查器本身")
    parser.add_argument("--app", default="app", help="被测包目录（默认 app）")
    args = parser.parse_args()

    if args.self_check and not self_check():
        return 1

    root = pathlib.Path(args.app).resolve()
    if not root.is_dir():
        print(f"[错误] 目录不存在：{root}")
        return 1

    edges = build_edges(root)
    cycles = find_cycles(edges)
    if not cycles:
        print("无模块级导入环。")
        return 0

    print(f"发现 {len(cycles)} 个模块级导入环：")
    for component in cycles:
        print("  分量：")
        for module in component:
            print(f"    {module}")
            for dep in sorted(d for d in edges.get(module, ()) if d in component):
                print(f"        -> {dep}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
