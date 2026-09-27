from typing import Literal, Optional
from pydantic import Field
from .base import ApiModel

ImpactLevel = Literal["Low", "Medium", "High"]
BenefitLevel = Literal["Small", "Medium", "High"]
RatingKey = Literal["business_impact", "business_benefit", "technical_impact"]
RatingValue = Literal["Low", "Small", "Medium", "High"]

class RatingConfirmation(ApiModel):
    key: RatingKey
    value: RatingValue
    source_hash: str
    actor_id: str
    actor: str
    at: str

class RatingView(ApiModel):
    proposed: Optional[RatingValue] = None
    confirmed: Optional[RatingValue] = None
    status: Literal["not_assessed", "proposed", "confirmed", "stale"] = "not_assessed"
    source_hash: str
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None

class StoryRatings(ApiModel):
    business_impact: RatingView
    business_benefit: RatingView
    technical_impact: RatingView
    revision: int = 0
    can_confirm_business: bool = False
    can_confirm_technical: bool = False

class ConfirmRatingInput(ApiModel):
    key: RatingKey
    value: RatingValue
    source_hash: str
    expected_revision: int = Field(ge=0)
