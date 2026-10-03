from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunTelemetryCounter:
    """Optional helper. Use it or replace it with your own observability."""
    llm_calls: int = 0
    tool_calls: int = 0
    tool_names: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking tokens where the provider reports them

    def record_llm_call(self) -> None:
        self.llm_calls += 1

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0

    def record_tool_call(self, name: str) -> None:
        self.tool_calls += 1
        self.tool_names.append(name)
