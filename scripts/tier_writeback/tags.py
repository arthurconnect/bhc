"""The BHC tag scheme, and the add/remove diff for a single customer.

Pure functions - no network, no database - so the part of this job that is
easiest to get quietly wrong is the part that is fully unit-testable without
credentials. See test_tags.py.

The rule the whole scheme rests on: exactly one tier tag and one engagement
tag per customer at all times. Shopify tags are additive, so every add of a
tier tag must be paired with the removal of the stale one, or a customer who
climbs from Betty to Caroline ends up wearing both.
"""

# Supabase customer_tiers.tier -> Shopify tag.
TIER_TAGS = {
    "SVIP Country Club Caroline": "bhc-svip-caroline",
    "VIP Country Club Caroline": "bhc-vip-caroline",
    "Country Club Caroline": "bhc-caroline",
    "VIP Backyard Betty": "bhc-vip-betty",
    "Backyard Betty": "bhc-betty",
    "Not yet tracked": "bhc-not-tracked",
}

# Supabase customer_tiers.engagement_state -> Shopify tag. Note the view stores
# "at_risk" with an underscore while the tag is "bhc-at-risk" with a hyphen, so
# this has to be an explicit table: "bhc-" + engagement_state would write the
# wrong tag for a quarter of the customer base.
ENGAGEMENT_TAGS = {
    "active": "bhc-active",
    "at_risk": "bhc-at-risk",
    "winback": "bhc-winback",
    "lapsed": "bhc-lapsed",
}

STAR_TAG = "bhc-star"

# The tags this job writes.
MANAGED_TAGS = frozenset(
    set(TIER_TAGS.values()) | set(ENGAGEMENT_TAGS.values()) | {STAR_TAG}
)

# Tags from the previous, hand-maintained scheme. Stripped wherever they turn
# up and never written, so the run that installs the new scheme also retires
# the old one instead of leaving two contradictory stories on the same record.
# Removing a tag is irreversible, so nothing belongs here that anything still
# depends on - as of 2026-08-24 no Klaviyo segment references either of these.
RETIRED_TAGS = frozenset({"VIP Betty", "VIP Caroline"})

# The complete set this job may remove. Everything else on a customer - a
# `Wholesale` tag, a hand-added `bhc-` one - is left strictly alone.
REMOVABLE_TAGS = frozenset(MANAGED_TAGS | RETIRED_TAGS)

_REMOVABLE_NORM = frozenset(tag.casefold() for tag in REMOVABLE_TAGS)


class UnmappedValue(Exception):
    """A tier or engagement_state the tag scheme has no entry for."""


def desired_tags(tier, is_repeat_customer, engagement_state):
    """The complete set of managed tags a customer should be carrying."""
    try:
        tags = {TIER_TAGS[tier]}
    except KeyError:
        raise UnmappedValue(f"tier {tier!r}") from None
    try:
        tags.add(ENGAGEMENT_TAGS[engagement_state])
    except KeyError:
        raise UnmappedValue(f"engagement_state {engagement_state!r}") from None
    if is_repeat_customer:
        tags.add(STAR_TAG)
    return frozenset(tags)


def diff(current, desired):
    """(tags_to_add, tags_to_remove) to move `current` to `desired`.

    Shopify treats tags case-insensitively for deduplication but stores them as
    entered, so comparison is done on casefolded values while removals carry the
    exact string Shopify holds - otherwise tagsRemove("bhc-betty") would leave a
    stray "BHC-Betty" behind.

    Removals are intersected with REMOVABLE_TAGS - the tags this job writes,
    plus the retired ones it is cleaning up. A customer's other tags, bhc-
    prefixed or not, are never touched.
    """
    by_norm = {}
    for tag in current:
        tag = (tag or "").strip()
        if tag:
            by_norm.setdefault(tag.casefold(), tag)

    desired_norm = {tag.casefold() for tag in desired}

    add = sorted(tag for tag in desired if tag.casefold() not in by_norm)
    remove = sorted(
        original
        for norm, original in by_norm.items()
        if norm in _REMOVABLE_NORM and norm not in desired_norm
    )
    return add, remove


def gid(customer_id):
    """customer_tiers stores a bare numeric; the Admin API wants the full GID."""
    return f"gid://shopify/Customer/{int(customer_id)}"


def customer_id_from_gid(value):
    """Inverse of gid(). Returns None for anything that isn't a customer GID."""
    if not isinstance(value, str) or "/" not in value:
        return None
    try:
        return int(value.rsplit("/", 1)[1])
    except ValueError:
        return None
