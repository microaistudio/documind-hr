# Path: services/llm_types.py
from pydantic import BaseModel, Field
from typing import Optional

class LLMOverrides(BaseModel):
    max_tokens: Optional[int] = Field(None, ge=64, le=4096)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    presence_penalty: Optional[float] = Field(None, ge=0.0, le=2.0)
    frequency_penalty: Optional[float] = Field(None, ge=0.0, le=2.0)
    # retrieval knobs
    topk: Optional[int] = Field(None, ge=4, le=64)
    percent_cap: Optional[int] = Field(None, ge=10, le=100)  # % of doc text allowed
    style: Optional[str] = Field(None, pattern="^(bullet|abstract|table)$")
    timeout_ms: Optional[int] = Field(None, ge=1000, le=600000)  # 1s .. 10m
