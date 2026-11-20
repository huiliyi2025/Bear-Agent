"""Read back content archived by context compaction.

Compaction replaces large tool output with a preview and an artifact id. This
tool is the model's only way to retrieve the full text. Access is scoped to the
current session: an artifact id from another session is reported as unknown,
never read.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from bear_agent.artifacts.store import ArtifactStore
from bear_agent.core.errors import ArtifactError
from bear_agent.core.modes import ToolSideEffect
from bear_agent.tools.base import Tool, ToolContext, ToolResult


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(description="Artifact id from a compaction marker (art_...).")
    offset: int = Field(default=0, ge=0, description="0-based character offset.")
    limit: int | None = Field(
        default=None,
        ge=1,
        le=200_000,
        description="Maximum characters to return; defaults to the tool output limit.",
    )


class ReadArtifactTool(Tool):
    name = "read_artifact"
    description = (
        "Read content that context compaction archived from an earlier tool "
        "result. Use the artifact id shown in a '[full output archived: ...]' "
        "marker. Only artifacts belonging to the current session are readable."
    )
    args_model = ReadArtifactArgs
    side_effect = ToolSideEffect.READ
    idempotent = True
    concurrency_safe = True

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ReadArtifactArgs)
        record = await self.artifacts.repository.get(args.artifact_id)
        if record is None or record.session_id != ctx.session_id:
            return ToolResult(
                content=f"unknown artifact: {args.artifact_id}",
                is_error=True,
                error_kind="unknown_artifact",
            )
        try:
            text = await self.artifacts.get_text(args.artifact_id)
        except ArtifactError as exc:
            return ToolResult(
                content=f"artifact {args.artifact_id} is unavailable: {exc}",
                is_error=True,
                error_kind="artifact_unavailable",
            )
        limit = min(args.limit or ctx.max_output_chars, ctx.max_output_chars)
        chunk = text[args.offset : args.offset + limit]
        truncated = args.offset + len(chunk) < len(text)
        suffix = ""
        if truncated:
            suffix = (
                f"\n... [artifact truncated: showing chars {args.offset}-"
                f"{args.offset + len(chunk)} of {len(text)}] ..."
            )
        return ToolResult(
            content=chunk + suffix,
            truncated=truncated,
            meta={
                "artifact_id": record.id,
                "sha256": record.sha256,
                "kind": record.kind,
                "size": record.size,
                "offset": args.offset,
                "returned_chars": len(chunk),
                "truncated": truncated,
            },
        )


__all__ = ["ReadArtifactArgs", "ReadArtifactTool"]
