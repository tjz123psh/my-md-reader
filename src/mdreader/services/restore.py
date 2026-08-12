from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RestoreRequest:
    """自动位置恢复请求，绑定到具体文档的 identity 与相对路径。"""

    relative_path: Path
    identity: tuple[int, int]
    slug: str


@dataclass(frozen=True, slots=True)
class PresentedAction:
    """document-presented 时应执行的动作。kind 取值 "fragment" | "restore" | "none"。"""

    kind: str
    slug: str = ""


def make_restore(
    relative_path: Path, identity: tuple[int, int], slug: str
) -> RestoreRequest | None:
    """slug 为空或仅空白时返回 None，否则返回 RestoreRequest。"""
    if not slug.strip():
        return None
    return RestoreRequest(relative_path=relative_path, identity=identity, slug=slug)


def restore_matches(
    request: RestoreRequest | None,
    relative_path: Path | None,
    identity: tuple[int, int] | None,
) -> bool:
    """request 非 None 且 request.relative_path == relative_path 且 request.identity == identity 时为 True；所有参数均需 None 安全。"""
    if request is None or relative_path is None or identity is None:
        return False
    return request.relative_path == relative_path and request.identity == identity


def resolve_presented_action(
    pending_fragment: tuple[Path, str] | None,
    pending_restore: RestoreRequest | None,
    current_relative_path: Path | None,
    current_identity: tuple[int, int] | None,
) -> PresentedAction:
    """优先级：
    1. pending_fragment 非 None 且 pending_fragment[0] == current_relative_path → PresentedAction("fragment", pending_fragment[1])
    2. 否则若 restore_matches(pending_restore, current_relative_path, current_identity) → PresentedAction("restore", pending_restore.slug)
    3. 否则 PresentedAction("none")
    """
    if pending_fragment is not None and pending_fragment[0] == current_relative_path:
        return PresentedAction("fragment", pending_fragment[1])
    if restore_matches(
        pending_restore, current_relative_path, current_identity
    ) and pending_restore is not None:
        return PresentedAction("restore", pending_restore.slug)
    return PresentedAction("none")
