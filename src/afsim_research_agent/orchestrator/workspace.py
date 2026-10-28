"""Path policy for the agent: everything happens under the data directory.

Rules (security constraints of the project):

- the agent addresses files by paths relative to the workspace root (``data/``); absolute
  local paths never appear in tool results;
- anything under a ``_local`` directory is off limits (native names, bindings);
- stages write only under ``runs/`` and only into directories that do not exist yet,
  so existing model packages and AFSIM files are never overwritten.

The same rules are given to Deep Agents' ``FilesystemBackend`` as permissions
(``graph.filesystem_permissions``) for the built-in file tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROTECTED_PART = "_local"
WRITABLE_TOP = "runs"


class WorkspaceError(ValueError):
    """A path the agent may not use; the message is shown to the model."""


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def from_settings(cls, settings=None) -> Workspace:
        if settings is None:
            from afsim_research_agent.config import Settings

            settings = Settings.from_environment()
        return cls(Path(settings.data_dir).resolve())

    # ------------------------------------------------------------------ resolving
    def resolve(self, path: str | Path) -> Path:
        """A workspace-relative path (``/runs/x`` or ``runs/x``) → absolute path."""
        text = str(path).replace("\\", "/").strip()
        if not text:
            raise WorkspaceError("路径为空")
        candidate = Path(text)
        if candidate.is_absolute():
            try:  # tolerate absolute paths that already point inside the root
                candidate = candidate.resolve().relative_to(self.root)
            except ValueError:
                candidate = Path(text.lstrip("/"))
        if ".." in candidate.parts:
            raise WorkspaceError(f"路径不能包含 '..'：{text}")
        if PROTECTED_PART in candidate.parts:
            raise WorkspaceError(f"{PROTECTED_PART}/ 下的内容只在本地使用，agent 不能读写：{text}")
        full = (self.root / candidate).resolve()
        if full != self.root and self.root not in full.parents:
            raise WorkspaceError(f"路径超出工作区：{text}")
        return full

    def existing(self, path: str | Path, *, kind: str = "file or directory") -> Path:
        full = self.resolve(path)
        if not full.exists():
            raise WorkspaceError(f"找不到 {kind}：{self.show(full)}")
        return full

    def new_dir(self, path: str | Path) -> Path:
        """A directory the stage will create; must be under ``runs/`` and not exist yet."""
        full = self.resolve(path)
        top = full.relative_to(self.root).parts[:1]
        if top != (WRITABLE_TOP,):
            raise WorkspaceError(f"只能在 {WRITABLE_TOP}/ 下创建输出目录：{self.show(full)}")
        if full.exists():
            raise WorkspaceError(f"输出目录已存在，不能覆盖：{self.show(full)}")
        return full

    def writable_file(self, path: str | Path) -> Path:
        """A new file under ``runs/`` (existing files are not overwritten)."""
        full = self.resolve(path)
        if full.relative_to(self.root).parts[:1] != (WRITABLE_TOP,):
            raise WorkspaceError(f"只能写 {WRITABLE_TOP}/ 下的文件：{self.show(full)}")
        if full.exists():
            raise WorkspaceError(f"文件已存在，不能覆盖：{self.show(full)}")
        return full

    def show(self, path: str | Path) -> str:
        try:
            return "/" + Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).name
