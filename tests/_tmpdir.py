"""测试用临时目录工具。

背景：部分运行环境（受限沙箱 / 非交互式令牌）下，`tempfile.TemporaryDirectory()`
创建的目录无法被当前进程再次写入（Windows 上表现为 `PermissionError` 或
SQLite 的 `unable to open database file`），导致大量与本项目逻辑无关的测试报错。

这里提供两个可互换的工具：

- `make_temp_dir()`：返回 `tempfile.TemporaryDirectory` 兼容对象（有 `.name`、
  `.cleanup()`，也支持 `with`），无法写入时回退到仓库根目录 `.test-tmp/`；
- `temp_dir_path()`：直接返回路径字符串，用于 `with ... as tmp:` 场景。
"""
from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _usable(path: Path) -> bool:
    probe = path / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _fallback_dir(prefix: str) -> str:
    target = _workspace_root() / ".test-tmp" / f"{prefix}{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


class TempDir:
    """`tempfile.TemporaryDirectory` 的可写性感知替代品。"""

    def __init__(self, prefix: str = "ra-test-") -> None:
        self._prefix = prefix
        self.name = self._create()
        self._owned = True

    def _create(self) -> str:
        candidate = Path(tempfile.mkdtemp(prefix=self._prefix))
        if _usable(candidate):
            return str(candidate)
        shutil.rmtree(candidate, ignore_errors=True)
        return _fallback_dir(self._prefix)

    def __enter__(self) -> "TempDir":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()

    def __fspath__(self) -> str:
        return self.name

    def __str__(self) -> str:
        return self.name

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)


def make_temp_dir(prefix: str = "ra-test-") -> TempDir:
    """返回可直接使用 `.name` 的临时目录对象（支持 with 与显式 cleanup）。"""
    return TempDir(prefix=prefix)


def temp_dir_path(prefix: str = "ra-test-") -> str:
    """返回临时目录路径字符串（`with ... as tmp:` 场景）。"""
    candidate = Path(tempfile.mkdtemp(prefix=prefix))
    if _usable(candidate):
        return str(candidate)
    shutil.rmtree(candidate, ignore_errors=True)
    return _fallback_dir(prefix)


def cleanup_workspace_temp() -> None:
    """清理工作区回退目录；供测试收尾调用。"""
    shutil.rmtree(_workspace_root() / ".test-tmp", ignore_errors=True)
