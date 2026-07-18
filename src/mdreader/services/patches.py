from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PatchProposal:
    path: Path
    base_hash: str
    start_line: int
    end_line: int
    replacement: str
    old_content: str
    new_content: str
    diff: str


class PatchService:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.expanduser().resolve(strict=True)
        self._undo: PatchProposal | None = None

    @staticmethod
    def parse_replacement(payload: str, expected_start: int, expected_end: int) -> tuple[int, int, str]:
        try:
            value = json.loads(payload.strip())
        except json.JSONDecodeError as error:
            raise PatchError("模型没有返回有效的修改建议") from error
        if not isinstance(value, dict) or set(value) != {"startLine", "endLine", "replacement"}:
            raise PatchError("修改建议的结构不正确")
        start = value.get("startLine")
        end = value.get("endLine")
        replacement = value.get("replacement")
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(replacement, str):
            raise PatchError("修改建议中包含无效字段")
        if start != expected_start or end != expected_end:
            raise PatchError("模型尝试修改选中范围之外的行")
        return start, end, replacement

    def propose(
        self,
        path: Path,
        *,
        expected_start: int,
        expected_end: int,
        expected_base_hash: str,
        payload: str,
    ) -> PatchProposal:
        start, end, replacement = self.parse_replacement(payload, expected_start, expected_end)
        path = self._validate_target(path)
        old_content = self._read_text(path)
        base_hash = self._hash(old_content)
        if base_hash != expected_base_hash:
            raise PatchError("OpenCode 生成建议期间，文件已发生变化")
        lines = old_content.splitlines(keepends=True)
        if start < 1 or end < start or end > len(lines):
            raise PatchError("选中的源码行已不存在")
        line_ending = "\r\n" if "\r\n" in old_content else "\r" if "\r" in old_content else "\n"
        normalized_replacement = replacement.replace("\r\n", "\n").replace("\r", "\n")
        if line_ending != "\n":
            normalized_replacement = normalized_replacement.replace("\n", line_ending)
        if (
            lines[end - 1].endswith(("\n", "\r"))
            and normalized_replacement
            and not normalized_replacement.endswith(("\n", "\r"))
        ):
            normalized_replacement += line_ending
        replacement_lines = normalized_replacement.splitlines(keepends=True)
        new_content = "".join(lines[: start - 1] + replacement_lines + lines[end:])
        if new_content == old_content:
            raise PatchError("此建议没有改变文档内容")
        relative_name = path.name
        diff = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{relative_name}",
                tofile=f"b/{relative_name}",
            )
        )
        return PatchProposal(
            path=path,
            base_hash=base_hash,
            start_line=start,
            end_line=end,
            replacement=replacement,
            old_content=old_content,
            new_content=new_content,
            diff=diff,
        )

    def apply(self, proposal: PatchProposal) -> None:
        path = self._validate_target(proposal.path)
        current = self._read_text(path)
        if self._hash(current) != proposal.base_hash:
            raise PatchError("建议生成后，文件已发生变化")
        self._atomic_write(path, proposal.new_content)
        self._undo = proposal

    def undo(self) -> bool:
        proposal = self._undo
        if proposal is None:
            return False
        path = self._validate_target(proposal.path)
        current = self._read_text(path)
        if current != proposal.new_content:
            raise PatchError("AI 修改后文件又发生了变化，未执行撤销")
        self._atomic_write(path, proposal.old_content)
        self._undo = None
        return True

    @property
    def can_undo(self) -> bool:
        return self._undo is not None

    def _validate_target(self, path: Path) -> Path:
        try:
            target = path.expanduser().resolve(strict=True)
        except OSError as error:
            raise PatchError(f"建议修改的文件已不可用：{path}") from error
        if not target.is_relative_to(self.workspace_root):
            raise PatchError("建议修改的文件位于当前工作区之外")
        if not target.is_file():
            raise PatchError("建议修改的路径不是文件")
        return target

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_text(path: Path) -> str:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(path.stat().st_mode)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
