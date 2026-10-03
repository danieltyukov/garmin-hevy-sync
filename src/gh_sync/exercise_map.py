"""Bind Garmin exercise identifiers to Hevy exercise templates.

Garmin reports lifts as a ``category``/``name`` pair of SCREAMING_SNAKE
constants (``BENCH_PRESS`` / ``BARBELL_BENCH_PRESS``). Hevy uses human titles
with the equipment in parentheses ("Bench Press (Barbell)"). Neither side
publishes a crosswalk, so we normalise both into token sets and score them.

The resolved map is cached to ``exercise_map.json`` in the home folder. Hand-written entries
under ``overrides`` always win, which is the escape hatch when the scorer picks
the wrong variant.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import paths

logger = logging.getLogger("gh_sync.exercise_map")

# Tokens that carry no discriminating meaning on either side.
STOPWORDS = {"the", "a", "with", "and", "or", "exercise", "unknown", "other"}

# How much an unmatched token costs, relative to a matched one. Garmin routinely
# omits the equipment that Hevy puts in its title, so an extra equipment word on
# the Hevy side is nearly free; an extra movement word means a different lift.
EXTRA_EQUIPMENT_COST = 0.25
EXTRA_TOKEN_COST = 0.75

# Garmin abbreviations and spelling variants folded onto the Hevy vocabulary.
SYNONYMS = {
    "db": "dumbbell",
    "bb": "barbell",
    "kb": "kettlebell",
    "ez": "ezbar",
    "banded": "band",
    "bands": "band",
    "resistance": "band",
    "exercises": "exercise",
    "cables": "cable",
    "machine": "machine",
    "smith": "smith",
    "bodyweight": "bodyweight",
    "weighted": "weighted",
    "assisted": "assisted",
    "flies": "fly",
    "flyes": "fly",
    "flye": "fly",
    "pulldowns": "pulldown",
    "situp": "situps",
    "sit": "situps",
    "ups": "up",
    "pullup": "pull",
    "chinup": "chin",
    "lateral": "lateral",
    "lat": "lat",
    "tricep": "triceps",
    "bicep": "biceps",
    "abdominal": "abs",
    "abdominals": "abs",
    "glute": "glutes",
    "hamstring": "hamstrings",
    "quadricep": "quads",
    "quadriceps": "quads",
}

# Equipment words. A match here is worth a bonus because it is usually what
# distinguishes two otherwise identical Hevy templates.
EQUIPMENT = {
    "barbell",
    "dumbbell",
    "kettlebell",
    "cable",
    "machine",
    "smith",
    "bodyweight",
    "band",
    "ezbar",
    "plate",
    "assisted",
    "weighted",
}

# When a Garmin exercise names no equipment, several Hevy templates tie on
# score and something has to break it. Hevy's catalogue lists a cable, barbell,
# dumbbell and Smith variant of most lifts; picking by title length awards the
# tie to whichever spelling happens to be shortest, which is how a plain
# BENCH_PRESS ends up as a cable bench press. Rank by what the unqualified name
# conventionally means in a gym instead.
EQUIPMENT_PRIORITY = {
    "barbell": 0,
    "dumbbell": 1,
    "bodyweight": 2,
    "machine": 3,
    "cable": 4,
    "kettlebell": 5,
    "smith": 6,
    "band": 7,
    "plate": 8,
    "ezbar": 9,
    "weighted": 10,
    "assisted": 11,
}
# A template naming no equipment at all ("Pull Up") beats every qualified one.
NO_EQUIPMENT_RANK = -1

_SUFFIXES = (("ies", "y"), ("ses", "s"), ("es", ""), ("s", ""))


def _stem(token: str) -> str:
    """Crude singulariser: enough to fold press/presses, curl/curls, row/rows."""
    if len(token) <= 3:
        return token
    for suffix, replacement in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + replacement
    return token


def tokenize(text: str) -> set[str]:
    """Normalise a Garmin constant or a Hevy title into a comparable token set."""
    if not text:
        return set()
    lowered = re.sub(r"[^a-z0-9]+", " ", text.lower())
    tokens = set()
    for raw in lowered.split():
        token = SYNONYMS.get(raw, raw)
        token = _stem(token)
        if token and token not in STOPWORDS:
            tokens.add(token)
    return tokens


def score(garmin_tokens: set[str], hevy_tokens: set[str]) -> float:
    """Asymmetric token similarity in [0, 1].

    Plain Jaccard is wrong here because it treats every unmatched token alike.
    Given Garmin's FRONT_RAISE, "Front Raise (Dumbbell)" and "Front Lever Raise"
    have identical Jaccard scores, yet only the first is the same exercise. The
    difference is what the extra token *is*: equipment that Garmin simply did
    not report, or a movement qualifier that makes it a different lift.

    Weighting extra equipment at a quarter of a token and extra movement words
    at three quarters separates those two cases. An outright equipment
    contradiction (barbell against dumbbell) takes a further flat penalty.
    """
    if not garmin_tokens or not hevy_tokens:
        return 0.0
    intersection = garmin_tokens & hevy_tokens
    if not intersection:
        return 0.0

    missing = garmin_tokens - hevy_tokens  # Garmin said it, Hevy's title lacks it
    extra = hevy_tokens - garmin_tokens
    extra_equipment = extra & EQUIPMENT

    denominator = (
        len(intersection)
        + len(missing)
        + EXTRA_EQUIPMENT_COST * len(extra_equipment)
        + EXTRA_TOKEN_COST * len(extra - extra_equipment)
    )
    value = len(intersection) / denominator

    garmin_equipment = garmin_tokens & EQUIPMENT
    hevy_equipment = hevy_tokens & EQUIPMENT
    if garmin_equipment and hevy_equipment and not (garmin_equipment & hevy_equipment):
        value -= 0.20

    return max(0.0, min(1.0, value))


def garmin_key(category: str | None, name: str | None) -> str:
    """Stable identifier for a Garmin exercise, used as the map key."""
    category = (category or "").strip().upper() or "UNKNOWN"
    name = (name or "").strip().upper()
    return f"{category}/{name}" if name and name != category else category


@dataclass(frozen=True)
class Match:
    hevy_id: str
    hevy_title: str
    score: float


class ExerciseMapper:
    def __init__(
        self,
        templates: Iterable[dict[str, Any]],
        threshold: float = 0.55,
        map_file: Path | None = None,
    ) -> None:
        self.threshold = threshold
        self.map_file = map_file or paths().exercise_map
        self.templates = [
            {"id": t["id"], "title": t.get("title", ""), "tokens": tokenize(t.get("title", ""))}
            for t in templates
            if t.get("id")
        ]
        self._disk = self._load()
        self.overrides: dict[str, str] = self._disk.get("overrides", {})
        self.resolved: dict[str, dict] = self._disk.get("resolved", {})
        self.unmapped: dict[str, dict] = self._disk.get("unmapped", {})
        self._by_id = {t["id"]: t for t in self.templates}

    def _load(self) -> dict:
        if self.map_file.exists():
            try:
                return json.loads(self.map_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("%s is not valid JSON; starting fresh", self.map_file)
        return {}

    def save(self) -> None:
        self.map_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": (
                "Edit 'overrides' to force a Garmin exercise onto a specific Hevy "
                "exercise_template_id. Overrides always win over 'resolved'. "
                "Entries in 'unmapped' scored below the threshold and were skipped."
            ),
            "overrides": self.overrides,
            "resolved": dict(sorted(self.resolved.items())),
            "unmapped": dict(sorted(self.unmapped.items())),
        }
        self.map_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _equipment_rank(template: dict) -> int:
        equipment = template["tokens"] & EQUIPMENT
        if not equipment:
            return NO_EQUIPMENT_RANK
        return min(EQUIPMENT_PRIORITY.get(word, 99) for word in equipment)

    def _best_template(
        self, tokens: set[str], stated_equipment: set[str] | None = None
    ) -> Match | None:
        """Highest-scoring template for a token set.

        ``stated_equipment`` is the equipment mentioned anywhere in the Garmin
        category or name. When present it is a hard filter, not a preference:
        the coarse category-only query would otherwise be free to answer
        NEUTRAL_GRIP_DUMBBELL_BENCH_PRESS with a cable bench press, because
        "bench press" alone matches the cable template perfectly and outscores
        the dumbbell template that the full name actually asked for.

        Ties break on equipment convention first, then title length.
        """
        best: Match | None = None
        best_rank = 99
        for template in self.templates:
            if stated_equipment:
                template_equipment = template["tokens"] & EQUIPMENT
                if template_equipment and not (template_equipment & stated_equipment):
                    continue
            value = score(tokens, template["tokens"])
            if value <= 0.0:
                continue
            rank = self._equipment_rank(template)
            if (
                best is None
                or value > best.score
                or (
                    value == best.score
                    and (
                        rank < best_rank
                        or (rank == best_rank and len(template["title"]) < len(best.hevy_title))
                    )
                )
            ):
                best, best_rank = Match(template["id"], template["title"], value), rank
        return best

    def resolve(self, category: str | None, name: str | None) -> Match | None:
        """Best Hevy template for a Garmin exercise, or None if below threshold."""
        key = garmin_key(category, name)

        forced = self.overrides.get(key)
        if forced:
            template = self._by_id.get(forced)
            title = template["title"] if template else forced
            return Match(hevy_id=forced, hevy_title=title, score=1.0)

        cached = self.resolved.get(key)
        if cached and cached.get("hevy_id") in self._by_id:
            return Match(cached["hevy_id"], cached["hevy_title"], cached["score"])

        # Three ways to phrase the query, scored independently, best score wins.
        #
        # Combined matters because Garmin sometimes parks the equipment in the
        # category: BANDED_EXERCISES/FLY is a band chest fly, and the name alone
        # throws the band away. But the category is just as often a taxonomy
        # bucket rather than a descriptor, and then it injects a token that is
        # actively wrong: LATERAL_RAISE/FRONT_RAISE is a front raise, and the
        # "lateral" only drags the match off target.
        #
        # Taking the maximum rather than the first result over the threshold is
        # what lets each case pick the phrasing that suits it.
        queries = [
            tokenize(f"{category} {name}") if category and name else None,
            tokenize(name) if name else None,
            tokenize(category) if category else None,
        ]

        # Equipment stated anywhere in the pair constrains every query variant,
        # including the category-only fallback.
        stated_equipment = tokenize(f"{category or ''} {name or ''}") & EQUIPMENT

        best: Match | None = None
        for tokens in queries:
            if not tokens:
                continue
            candidate = self._best_template(tokens, stated_equipment)
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate

        if best and best.score >= self.threshold:
            self.resolved[key] = {
                "hevy_id": best.hevy_id,
                "hevy_title": best.hevy_title,
                "score": round(best.score, 3),
            }
            self.unmapped.pop(key, None)
            return best

        entry = self.unmapped.setdefault(key, {"seen": 0})
        entry["seen"] = entry.get("seen", 0) + 1
        if best:
            entry["closest"] = best.hevy_title
            entry["closest_id"] = best.hevy_id
            entry["score"] = round(best.score, 3)
        return None
