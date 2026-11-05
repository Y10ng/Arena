"""项目读取复用统一消费账本的已知小计，不把未知分量转换为零。"""
from pydantic import BaseModel, Field


class ConsumptionUsage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None


class ConsumptionAmount(BaseModel):
    currency: str
    amount: str


class ConsumptionSummary(BaseModel):
    tokens: int
    responses: int
    partial_responses: int
    unpriced_responses: int
    amounts: list[ConsumptionAmount] = Field(default_factory=list)
    usage: ConsumptionUsage
    unobserved_bindings: int = 0
