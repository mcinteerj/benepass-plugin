"""Eligibility policy: what each benefit accepts, straight from Benepass.

This is the data behind the app's "Policy overview" screen, and it comes from
three per-benefit endpoints:

    /v2/me/benefits/{id}/eligibility-categories/   what you may buy
    /v2/me/benefits/{id}/allowed-merchants/        buy ANYTHING here
    /v2/me/benefits/{id}/disallowed-merchants/     buy nothing here

None of these are discoverable from the generic `/v2/me/merchants/` catalog. An
earlier version of this module derived categories by unioning the categories of
each benefit's merchants, which silently under-reported them by roughly half:
only categories with at least one curated merchant survive that derivation, so
whole eligible categories vanished. Always use the policy endpoints; never
re-derive.
"""

from __future__ import annotations

from typing import Any

from . import api


def _clean(text: Any) -> str:
    """Benepass mixes \\r\\n and \\n inside the specification and example blocks."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def merchants(
    client: api.Client, benefit_id: str | None = None
) -> list[dict[str, Any]]:
    """The general merchant catalog, optionally narrowed to one benefit."""
    params: dict[str, Any] = {"page_size": 300}
    if benefit_id:
        params["benefit"] = benefit_id
    return api.rows(client.get("/v2/me/merchants/", params))


def _benefit_rows(
    client: api.Client, benefit_id: str, leaf: str
) -> list[dict[str, Any]]:
    return api.rows(
        client.get(f"/v2/me/benefits/{benefit_id}/{leaf}/", {"page_size": 300})
    )


def categories_for(client: api.Client, benefit_id: str) -> list[dict[str, Any]]:
    """Every eligible category for a benefit, with its specification and examples."""
    out = []
    for cat in _benefit_rows(client, benefit_id, "eligibility-categories"):
        out.append(
            {
                "id": cat.get("id"),
                "name": cat.get("name"),
                "benefit_type": cat.get("benefit_type"),
                "specification": _clean(cat.get("specification")),
                "examples": _clean(cat.get("examples")),
                "metadata": cat.get("eligibility_metadata") or {},
            }
        )
    return sorted(out, key=lambda c: str(c.get("name") or ""))


def _merchant_names(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(m.get("display_name") or m.get("name") or m.get("main_name") or "")
            for m in rows
        }
        - {""}
    )


def policy_for(client: api.Client, benefit_id: str) -> dict[str, Any]:
    """A benefit's full policy: categories, plus merchant allow/deny overrides."""
    return {
        "categories": categories_for(client, benefit_id),
        "allowed_merchants": _merchant_names(
            _benefit_rows(client, benefit_id, "allowed-merchants")
        ),
        "disallowed_merchants": _merchant_names(
            _benefit_rows(client, benefit_id, "disallowed-merchants")
        ),
    }


def catalog(client: api.Client, benefits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-benefit policy, narrowest first.

    Ordering is the point: the claiming rule is "use the narrowest benefit that
    legitimately covers the purchase", and category count is the only objective
    narrowness signal Benepass gives us.
    """
    out = []
    for benefit in benefits:
        policy = policy_for(client, benefit["id"])
        out.append(
            {
                "benefit_id": benefit["id"],
                "benefit": benefit.get("name"),
                "available_usd": benefit.get("available_usd"),
                "available_local": benefit.get("available"),
                "category_count": len(policy["categories"]),
                **policy,
            }
        )
    return sorted(out, key=lambda b: (b["category_count"], str(b["benefit"] or "")))


def to_markdown(rows: list[dict[str, Any]], generated_note: str) -> str:
    """Render the policy as the skill's reference doc. Generated, never hand-edited."""
    lines = [
        "# Benepass eligibility policy — GENERATED, do not hand-edit",
        "",
        # No date stamp: this file is regenerated periodically and diffed to decide
        # whether the employer changed the policy. A generation date would change on
        # every run, producing a phantom "policy changed" every time.
        generated_note,
        "",
        "Regenerate with `benepass categories --markdown > ~/.config/benepass/categories.md`.",
        "Anything hand-edited here is silently overwritten on the next regeneration.",
        "",
        "This is the same data as the Benepass app's **Policy overview** screen, read from the",
        "per-benefit policy endpoints — it is authoritative, not inferred. A category absent from a",
        "benefit is genuinely not eligible for it.",
        "",
        "Benefits are listed **narrowest first**: category count is the objective narrowness signal, and",
        "the rule is to claim against the narrowest benefit that legitimately covers the purchase.",
        "",
        "**Allowed merchants** are an override — any purchase there qualifies regardless of category.",
        "**Disallowed merchants** are the reverse: nothing bought there qualifies, whatever the category.",
        "",
    ]
    for row in rows:
        lines += [
            f"## {row['benefit']} — {row['category_count']} categories",
            "",
            f"`{row['benefit_id']}`",
            "",
        ]
        if row.get("allowed_merchants"):
            lines += [
                "**Allowed merchants** (anything bought here qualifies): "
                + ", ".join(row["allowed_merchants"]),
                "",
            ]
        if row.get("disallowed_merchants"):
            lines += [
                "**Disallowed merchants** (nothing bought here qualifies): "
                + ", ".join(row["disallowed_merchants"]),
                "",
            ]
        if not row["categories"]:
            lines += ["_No eligibility categories exposed for this benefit._", ""]
            continue
        for cat in row["categories"]:
            lines += [f"### {cat['name']}", ""]
            if cat["specification"]:
                lines += [cat["specification"], ""]
            if cat["examples"]:
                lines += ["Examples:", ""]
                for line in cat["examples"].split("\n"):
                    line = line.strip()
                    if line:
                        lines.append(line if line.startswith("-") else f"- {line}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"
