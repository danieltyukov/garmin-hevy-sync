from __future__ import annotations

import json

import pytest

from gh_sync.exercise_map import (
    EQUIPMENT,
    ExerciseMapper,
    garmin_key,
    score,
    tokenize,
)

# A slice of real Hevy template shapes.
TEMPLATES = [
    # "Bench Press (Cable)" is deliberately the shortest of the bench titles:
    # it is the template a length-based tie-break wrongly prefers.
    {"id": "T_BENCH_CABLE", "title": "Bench Press (Cable)", "type": "weight_reps"},
    {"id": "T_BENCH_BB", "title": "Bench Press (Barbell)", "type": "weight_reps"},
    {"id": "T_BENCH_DB", "title": "Bench Press (Dumbbell)", "type": "weight_reps"},
    {"id": "T_BENCH_SMITH", "title": "Bench Press (Smith Machine)", "type": "weight_reps"},
    {"id": "T_SQUAT_BB", "title": "Squat (Barbell)", "type": "weight_reps"},
    {"id": "T_SQUAT_BAND", "title": "Squat (Band)", "type": "weight_reps"},
    {"id": "T_LAT_PULLDOWN", "title": "Lat Pulldown (Cable)", "type": "weight_reps"},
    {"id": "T_BICEP_CURL_DB", "title": "Bicep Curl (Dumbbell)", "type": "weight_reps"},
    {"id": "T_FRONT_RAISE_DB", "title": "Front Raise (Dumbbell)", "type": "weight_reps"},
    {"id": "T_FRONT_LEVER_RAISE", "title": "Front Lever Raise", "type": "reps_only"},
    {"id": "T_PLANK", "title": "Plank", "type": "duration"},
    {"id": "T_PULL_UP", "title": "Pull Up", "type": "reps_only"},
    {"id": "T_PULL_UP_WEIGHTED", "title": "Pull Up (Weighted)", "type": "weighted_bodyweight"},
]


@pytest.fixture()
def mapper(tmp_path):
    return ExerciseMapper(TEMPLATES, threshold=0.55, map_file=tmp_path / "map.json")


class TestTokenize:
    def test_garmin_constant_and_hevy_title_agree(self):
        assert tokenize("BARBELL_BENCH_PRESS") == tokenize("Bench Press (Barbell)")

    def test_plurals_are_stemmed_together(self):
        assert tokenize("BICEP_CURLS") == tokenize("Bicep Curl")

    def test_abbreviations_expand(self):
        assert "dumbbell" in tokenize("DB_ROW")
        assert "barbell" in tokenize("BB_ROW")

    def test_stopwords_dropped(self):
        assert tokenize("OTHER") == set()

    def test_empty_input(self):
        assert tokenize("") == set()
        assert tokenize(None) == set()


class TestScore:
    def test_identical_scores_high(self):
        value = score(tokenize("BARBELL_BENCH_PRESS"), tokenize("Bench Press (Barbell)"))
        assert value >= 0.95

    def test_equipment_disagreement_is_penalised(self):
        barbell = score(tokenize("BARBELL_BENCH_PRESS"), tokenize("Bench Press (Barbell)"))
        dumbbell = score(tokenize("BARBELL_BENCH_PRESS"), tokenize("Bench Press (Dumbbell)"))
        assert barbell > dumbbell

    def test_unrelated_scores_low(self):
        assert score(tokenize("PLANK"), tokenize("Squat (Barbell)")) < 0.2

    def test_empty_side_is_zero(self):
        assert score(set(), tokenize("Plank")) == 0.0

    def test_equipment_vocabulary_is_lowercase_stems(self):
        # Guards the table against drifting out of sync with tokenize().
        for word in EQUIPMENT:
            assert tokenize(word) == {word}, word


class TestResolve:
    def test_picks_the_right_equipment_variant(self, mapper):
        match = mapper.resolve("BENCH_PRESS", "BARBELL_BENCH_PRESS")
        assert match is not None
        assert match.hevy_id == "T_BENCH_BB"

    def test_dumbbell_variant(self, mapper):
        match = mapper.resolve("BENCH_PRESS", "DUMBBELL_BENCH_PRESS")
        assert match is not None
        assert match.hevy_id == "T_BENCH_DB"

    def test_category_only_still_resolves(self, mapper):
        match = mapper.resolve("LAT_PULLDOWN", None)
        assert match is not None
        assert match.hevy_id == "T_LAT_PULLDOWN"

    def test_unknown_exercise_returns_none_and_is_reported(self, mapper):
        assert mapper.resolve("UNDERWATER_BASKET_WEAVING", None) is None
        assert "UNDERWATER_BASKET_WEAVING" in mapper.unmapped

    def test_unmapped_counts_repeat_sightings(self, mapper):
        mapper.resolve("ZERCHER_CARRY_THING", None)
        mapper.resolve("ZERCHER_CARRY_THING", None)
        assert mapper.unmapped["ZERCHER_CARRY_THING"]["seen"] == 2

    def test_override_beats_the_scorer(self, mapper):
        mapper.overrides["BENCH_PRESS/BARBELL_BENCH_PRESS"] = "T_PLANK"
        match = mapper.resolve("BENCH_PRESS", "BARBELL_BENCH_PRESS")
        assert match.hevy_id == "T_PLANK"
        assert match.score == 1.0

    def test_resolution_is_cached(self, mapper):
        mapper.resolve("SQUAT", "BARBELL_SQUAT")
        assert "SQUAT/BARBELL_SQUAT" in mapper.resolved

    def test_save_and_reload_round_trips(self, mapper, tmp_path):
        mapper.resolve("SQUAT", "BARBELL_SQUAT")
        mapper.resolve("NONSENSE_MOVEMENT", None)
        mapper.save()

        reloaded = ExerciseMapper(TEMPLATES, threshold=0.55, map_file=mapper.map_file)
        assert "SQUAT/BARBELL_SQUAT" in reloaded.resolved
        assert "NONSENSE_MOVEMENT" in reloaded.unmapped

    def test_corrupt_map_file_does_not_crash(self, tmp_path):
        path = tmp_path / "map.json"
        path.write_text("{not json")
        loaded = ExerciseMapper(TEMPLATES, map_file=path)
        assert loaded.resolved == {}

    def test_cached_entry_for_deleted_template_is_rescored(self, tmp_path):
        path = tmp_path / "map.json"
        path.write_text(json.dumps({
            "resolved": {"SQUAT/BARBELL_SQUAT": {
                "hevy_id": "T_DELETED", "hevy_title": "Gone", "score": 0.9}}
        }))
        loaded = ExerciseMapper(TEMPLATES, map_file=path)
        match = loaded.resolve("SQUAT", "BARBELL_SQUAT")
        assert match is not None and match.hevy_id == "T_SQUAT_BB"


class TestEquipmentDisambiguation:
    """Regressions found by scoring the real 1527-exercise Garmin catalogue."""

    def test_bare_category_prefers_barbell_over_cable(self, mapper):
        # Every single-equipment bench template ties on score. Title length
        # would hand this to the cable variant; gym convention says barbell.
        match = mapper.resolve("BENCH_PRESS", "BENCH_PRESS")
        assert match.hevy_id == "T_BENCH_BB"

    def test_equipment_in_the_name_survives_the_category_fallback(self, mapper):
        # The name is too specific to score well, so resolution falls back to
        # the bare category. That fallback must not discard "dumbbell".
        match = mapper.resolve("BENCH_PRESS", "NEUTRAL_GRIP_DUMBBELL_BENCH_PRESS")
        assert match.hevy_id == "T_BENCH_DB"

    def test_equipment_in_the_category_is_honoured(self, mapper):
        # BANDED_EXERCISES carries the equipment; the name alone would lose it.
        match = mapper.resolve("BANDED_EXERCISES", "SQUAT")
        assert match.hevy_id == "T_SQUAT_BAND"

    def test_contradicting_equipment_is_excluded_entirely(self, mapper):
        # No kettlebell bench template exists; better to report nothing than to
        # silently log a cable press.
        assert mapper.resolve("BENCH_PRESS", "KETTLEBELL_CHEST_PRESS") is None

    def test_extra_movement_word_loses_to_extra_equipment_word(self, mapper):
        # "Front Raise (Dumbbell)" and "Front Lever Raise" tie under Jaccard.
        match = mapper.resolve("LATERAL_RAISE", "FRONT_RAISE")
        assert match.hevy_id == "T_FRONT_RAISE_DB"

    def test_unqualified_template_beats_a_qualified_one(self, mapper):
        match = mapper.resolve("PULL_UP", "PULL_UP")
        assert match.hevy_id == "T_PULL_UP"

    def test_a_specific_query_can_beat_the_coarse_category(self, mapper):
        match = mapper.resolve("BENCH_PRESS", "DUMBBELL_BENCH_PRESS")
        assert match.hevy_id == "T_BENCH_DB"
        assert match.score == 1.0


class TestGarminKey:
    def test_category_and_name_combined(self):
        assert garmin_key("BENCH_PRESS", "BARBELL_BENCH_PRESS") == "BENCH_PRESS/BARBELL_BENCH_PRESS"

    def test_name_equal_to_category_collapses(self):
        assert garmin_key("PLANK", "PLANK") == "PLANK"

    def test_missing_name(self):
        assert garmin_key("PLANK", None) == "PLANK"

    def test_missing_everything(self):
        assert garmin_key(None, None) == "UNKNOWN"
