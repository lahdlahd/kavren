"""Approved job templates.

A template is a fixed, versioned parameter set. The buyer agent may only use
templates listed in the owner's on-chain policy; it never edits a spec.
Prices are in yoctoNEAR (1 NEAR = 10**24 yocto) and are kept as ints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .spec import Column, DateRules, Spec

YOCTO_PER_NEAR = 10**24
_MILLI = 10**21  # 0.001 NEAR

DATE_INPUTS = ("DD/MM/YYYY", "YYYY-MM-DD", "D MMM YYYY")


@dataclass(frozen=True)
class Template:
    template_id: str
    template_version: int
    columns: tuple[Column, ...]
    dedupe_key: tuple[str, ...]
    max_reject_pct: int
    price_base_yocto: int
    price_per_row_yocto: int

    def build_spec(self) -> Spec:
        return Spec(
            template_id=self.template_id,
            template_version=self.template_version,
            columns=self.columns,
            dedupe_key=self.dedupe_key,
            date_rules=DateRules(input_formats=DATE_INPUTS, output_format="YYYY-MM-DD"),
            max_reject_pct=self.max_reject_pct,
        )

    def price_yocto(self, input_rows: int) -> int:
        if input_rows < 1:
            raise ValueError("input_rows must be >= 1")
        return self.price_base_yocto + self.price_per_row_yocto * input_rows


TEMPLATES: dict[str, Template] = {
    t.template_id: t
    for t in (
        Template(
            template_id="contacts-v1",
            template_version=1,
            columns=(
                Column(name="full_name", type="text", required=True),
                Column(name="email", type="email", required=True),
                Column(name="signup_date", type="date", required=False),
            ),
            dedupe_key=("email",),
            max_reject_pct=25,
            price_base_yocto=200 * _MILLI,   # 0.2 NEAR
            price_per_row_yocto=2 * _MILLI,  # 0.002 NEAR per input row
        ),
        Template(
            template_id="leads-v1",
            template_version=1,
            columns=(
                Column(name="company", type="text", required=True),
                Column(name="contact_email", type="email", required=True),
                Column(name="country", type="text", required=False),
                Column(name="created_at", type="date", required=False),
            ),
            dedupe_key=("company", "contact_email"),
            max_reject_pct=25,
            price_base_yocto=200 * _MILLI,
            price_per_row_yocto=2 * _MILLI,
        ),
    )
}


def get_template(template_id: str) -> Template:
    try:
        return TEMPLATES[template_id]
    except KeyError:
        raise KeyError(f"unknown template {template_id!r}") from None


def select_template(header: Iterable[str], allowed: Optional[Iterable[str]] = None) -> Optional[str]:
    """Return the single template whose columns exactly match `header` (as a set).

    Header cells are compared after trimming; order does not matter. Returns None
    if no template, or more than one, matches. `allowed` restricts the candidates
    to the owner's policy.
    """
    cells = [h.strip() for h in header]
    if len(set(cells)) != len(cells):
        return None
    allowed_set = set(allowed) if allowed is not None else None
    matches = [
        t.template_id
        for t in TEMPLATES.values()
        if (allowed_set is None or t.template_id in allowed_set)
        and set(cells) == {c.name for c in t.columns}
    ]
    return matches[0] if len(matches) == 1 else None
