"""
The Normalized Deployment Plan (NDP).

This is the contract between "some spreadsheet a human wrote" and the
validation engine. Deployment plans arrive in arbitrary shapes; they are all
flattened into the structures below, and the validator only ever reads these.

The important idea: a plan is reduced to a flat list of :class:`Target` rows.
One target = one thing that must be true in the audit log, addressed as
(entity, group, button). Everything else on the plan is context.

An NDP round-trips through JSON, so a plan that auto-detection gets wrong can
be exported, hand-corrected, and re-uploaded as a ``.json`` plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

SCHEMA_VERSION = "1.0"


@dataclass
class Target:
    """One expected change: button ``button`` of ``entity`` must reach ``expect_state``."""

    entity: str                      # e.g. "Fairway - Portfolio A" (screen set / coop)
    group: str                       # e.g. "Breakfast" (daypart / column it came from)
    button: str                      # e.g. "12"
    expect_state: str = "Disable"    # target state of the button
    from_state: str = "Enable"       # expected previous state
    workflow: str = ""               # optional screen/workflow number for the group
    source: str = ""                 # where in the plan this came from, for traceability

    @property
    def key(self) -> tuple[str, str]:
        return (norm_entity(self.entity), self.button)


@dataclass
class Change:
    """One row of a plan written on the Audit Master template.

    A :class:`Target` can only say "this button ends up enabled or disabled".
    A change says the general thing — *this field* of *this thing* should end
    up reading *this value* — which covers captions, display orders, a store's
    time zone or a user's status alike.
    """

    tab: str                         # the template tab it came from: "Menu item plan"
    row: int = 0                     # 1-based row on that tab, for the reader
    kind: str = ""                   # channel / type: "Price", "User", "Package schedule"
    where: str = ""                  # screen set, menu item set, user ID, file name
    store: str = ""                  # restaurant number, for store-level work
    screen: str = ""                 # screen name
    screen_number: str = ""
    group: str = ""                  # daypart
    button: str = ""
    menu_item: str = ""
    name: str = ""                   # menu item or store name, for display only
    what: str = ""                   # the field, as the audit log names it
    language: str = ""               # blank or "All" means every language
    was: str = ""
    becomes: str = ""
    effective: str = ""
    notes: str = ""

    @property
    def label(self) -> str:
        """Where this change is, in the words a reader would use."""
        bits = [b for b in (self.where, f"store {self.store}" if self.store else "",
                            self.screen, f"button {self.button}" if self.button else "",
                            f"menu item {self.menu_item}" if self.menu_item else "",
                            self.name) if b]
        return " · ".join(bits) or self.kind or "(everywhere)"

    @property
    def field_label(self) -> str:
        lang = self.language if self.language and self.language.lower() != "all" else ""
        return f"{self.what} ({lang})" if lang else self.what


@dataclass
class Assignment:
    """Who was asked to do / check one entity."""

    entity: str
    assignee: str = ""
    assignee_status: str = ""
    validator: str = ""
    validator_status: str = ""


@dataclass
class PlanMeta:
    """Context that shapes the checks but is not itself a target."""

    screen_number: str = ""          # "61000"
    screen_name: str = ""            # "Left hand navigation"
    tile_label: str = ""             # "NEW McCafé Specialty Drinks"
    action: str = "disable"          # disable | enable
    environment: str = ""            # Prod | Pre-Prod, when the plan states it
    operation: str = ""              # expected audit "Operation", e.g. Manage Screen Set
    group_workflows: dict[str, str] = field(default_factory=dict)
    instructions: list[str] = field(default_factory=list)
    validation_rules: list[str] = field(default_factory=list)
    #: Everything the plan states about the deployment as a whole (ticket,
    #: owner, dates, rollback…), label -> value, in the plan's own order.
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class Plan:
    """A fully normalized deployment plan."""

    plan_name: str = ""
    profile: str = ""
    profile_label: str = ""
    schema_version: str = SCHEMA_VERSION
    source_filename: str = ""
    source_format: str = ""
    source_encoding: str | None = None
    sheet_names: list[str] = field(default_factory=list)
    sheet_roles: dict[str, str] = field(default_factory=dict)
    meta: PlanMeta = field(default_factory=PlanMeta)
    targets: list[Target] = field(default_factory=list)
    #: Row-level expectations from the plan template (see plan_template.py).
    changes: list[Change] = field(default_factory=list)
    #: Places the plan says must not be touched at all.
    do_not_touch: list[str] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Values harvested from the plan for operation-agnostic matching. Held as
    #: a plain list (of :class:`auditmaster.evidence.Claim`) to avoid an import
    #: cycle.
    claims: list = field(default_factory=list)

    # -- views ------------------------------------------------------------
    @property
    def entities(self) -> list[str]:
        seen: list[str] = []
        for t in self.targets:
            if t.entity not in seen:
                seen.append(t.entity)
        return seen

    def targets_for(self, entity: str) -> list[Target]:
        want = norm_entity(entity)
        return [t for t in self.targets if norm_entity(t.entity) == want]

    def find_entity(self, name: str) -> str | None:
        """Resolve an audit-log entity name to the plan's spelling of it."""
        want = norm_entity(name)
        for e in self.entities:
            if norm_entity(e) == want:
                return e
        # Tolerate the plan carrying a suffix the audit log omits, or vice versa
        # — "16 - GREAT PLAINS (362)" is the same co-op as "16 - GREAT PLAINS",
        # the 362 being a screen number. But an environment qualifier is not
        # decoration: matching a Pre-Prod plan against a Prod log would validate
        # the wrong system and say nothing about it.
        for e in self.entities:
            ne = norm_entity(e)
            if not (ne and want):
                continue
            if ne.startswith(want):
                extra = ne[len(want):]
            elif want.startswith(ne):
                extra = want[len(ne):]
            else:
                continue
            if _is_environment_suffix(extra):
                continue
            return e
        return None

    def mentions(self, name: str) -> bool:
        """True if a template change row names this place."""
        want = norm_entity(name)
        return bool(want) and any(norm_entity(c.where) == want for c in self.changes)

    # -- serialization ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Only what the plan asks for; a claim's match results belong to a
        # validation, not to the plan.
        d["claims"] = [
            {"value": c.value, "kind": c.kind, "label": c.label, "source": c.source}
            for c in self.claims
        ]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plan:
        meta_d = d.get("meta") or {}
        plan = cls(
            plan_name=d.get("plan_name", ""),
            profile=d.get("profile", ""),
            profile_label=d.get("profile_label", ""),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            source_filename=d.get("source_filename", ""),
            source_format=d.get("source_format", ""),
            source_encoding=d.get("source_encoding"),
            sheet_names=list(d.get("sheet_names") or []),
            sheet_roles=dict(d.get("sheet_roles") or {}),
            meta=PlanMeta(**{k: v for k, v in meta_d.items() if k in PlanMeta.__dataclass_fields__}),
            groups=list(d.get("groups") or []),
            do_not_touch=list(d.get("do_not_touch") or []),
            warnings=list(d.get("warnings") or []),
            notes=list(d.get("notes") or []),
        )
        for t in d.get("targets") or []:
            plan.targets.append(
                Target(**{k: v for k, v in t.items() if k in Target.__dataclass_fields__})
            )
        for c in d.get("changes") or []:
            plan.changes.append(
                Change(**{k: v for k, v in c.items() if k in Change.__dataclass_fields__})
            )
        if d.get("claims"):
            from .evidence import Claim   # evidence imports this module
            for c in d["claims"]:
                if isinstance(c, dict) and c.get("value") and c.get("kind"):
                    plan.claims.append(Claim(value=str(c["value"]), kind=str(c["kind"]),
                                             label=str(c.get("label", "")),
                                             source=str(c.get("source", ""))))
        for a in d.get("assignments") or []:
            plan.assignments.append(
                Assignment(**{k: v for k, v in a.items() if k in Assignment.__dataclass_fields__})
            )
        return plan


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

_PUNCT = str.maketrans({c: " " for c in "-–—_/\\.,:;()[]{}'\"*#&+"})


#: Words that make an entity a *different* target rather than the same one
#: written longer. Screen numbers and the like are safe to ignore; these are not.
ENVIRONMENT_WORDS = {
    "preprod", "pre", "prod", "production", "staging", "stage", "test",
    "testing", "uat", "qa", "dev", "development", "sandbox", "training",
    "pilot", "mirror", "dr",
}


def _is_environment_suffix(extra: str) -> bool:
    """True if the part that differs names an environment."""
    return any(tok in ENVIRONMENT_WORDS for tok in extra.split())


def norm_entity(name: str) -> str:
    """Loose key for matching entity names across the two documents.

    ``"29 - MOCNI"``, ``"29  MOCNI"`` and ``"29 mocni"`` all collapse to the
    same key, but ``"29 - MOCNI (Pre-Prod)"`` stays distinct because the
    parenthesised qualifier carries real meaning.
    """
    if not name:
        return ""
    s = name.replace("\xa0", " ").strip().lower()
    s = s.translate(_PUNCT)
    return " ".join(s.split())


def norm_label(text: str) -> str:
    """Aggressive key for comparing captions/labels (drops all non-alphanumerics)."""
    if not text:
        return ""
    s = text.replace("\xa0", " ").lower()
    # Plans sometimes carry literal escape text such as "\r" inside a label.
    s = s.replace("\\r", " ").replace("\\n", " ")
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return " ".join(s.split())


def fold(text: str) -> str:
    """Accent-insensitive key: ``"McCafé"`` and ``"McCafe"`` compare equal.

    The two documents are exported by different systems, and one may strip
    diacritics from a product name that the other keeps.
    """
    import unicodedata

    s = norm_label(text)
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))
-0