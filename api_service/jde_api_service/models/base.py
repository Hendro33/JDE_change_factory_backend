"""
Shared pydantic base: every model on this API's wire format is
camelCase, matching src/types/domain.ts exactly, while the Python code
underneath stays snake_case. This is what lets httpApi.ts consume
these responses with zero translation layer on the frontend side.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
