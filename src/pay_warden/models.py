"""Typed request/decision models shared by the policy engine, audit store, and server."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Product(BaseModel):
    description: str
    unit_price: Decimal
    quantity: int = 1


class PurchaseRequest(BaseModel):
    agent: str
    merchant_name: str
    merchant_url: str
    merchant_country: str = Field(pattern=r"^[A-Z]{2}$")
    products: list[Product] = Field(min_length=1)
    total_amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("total_amount")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("total_amount must be positive")
        return v

    def products_total(self) -> Decimal:
        return sum((p.unit_price * p.quantity for p in self.products), Decimal("0"))


class Verdict(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


class Decision(BaseModel):
    verdict: Verdict
    rule_id: str
    reason: str
