"""Tests for country scores, red dots and the published file."""

import pytest

import scoring
from scoring import Story, build_output, country_scores, red_dots, world_mean

DAY = 86400.0
NOW = 100 * DAY


def story(age_days=0.0, stress=1.0, country="XX", from_title=True, outlets=1, id=None, title="A headline"):
    story.next_id += 1
    return Story(id=id or story.next_id, first_seen=NOW - age_days * DAY, stress=stress, title=title,
                 link="https://example.com/a", source="Outlet", outlets={f"o{i}" for i in range(outlets)},
                 countries={country: from_title}, positions={country: (1.0, 2.0)})


story.next_id = 0


def history(per_block, days=14, country="XX"):
    """per_block quiet stories in each 6-hour block before the current one."""
    return [story(age_days=(b + 0.5) / 4, country=country) for b in range(1, days * 4 + 1) for _ in range(per_block)]


def spike(count, country="XX", outlets=3, stress=2.5, from_title=True):
    return [story(age_days=0.1, country=country, outlets=outlets, stress=stress, from_title=from_title) for _ in range(count)]


def test_thin_country_stays_near_the_world_mean():
    assert country_scores([story(stress=3.0)], 1.0, NOW)["XX"]["score"] < 1.2


def test_busy_country_keeps_its_own_score():
    assert country_scores([story(stress=2.0) for _ in range(200)], 1.0, NOW)["XX"]["score"] > 1.9


def test_old_stories_count_half_after_two_days():
    assert country_scores([story(age_days=2.0)], 1.0, NOW)["XX"]["evidence"] == pytest.approx(0.5)


def test_colour_is_clamped_at_the_calm_end():
    assert country_scores([story(stress=0.1) for _ in range(1000)], 0.1, NOW)["XX"]["colour"] == 0


def test_colour_is_clamped_at_the_stressed_end():
    assert country_scores([story(stress=2.5) for _ in range(1000)], 2.5, NOW)["XX"]["colour"] == 1


def test_no_signal_has_no_colour():
    assert country_scores([story(age_days=2.0) for _ in range(5)], 1.0, NOW)["XX"]["colour"] is None


def test_brightness_is_confidence():
    assert country_scores([story() for _ in range(10)], 1.0, NOW)["XX"]["brightness"] == pytest.approx(0.5)


def test_each_country_of_a_story_counts_it():
    both = story()
    both.countries = {"XX": True, "YY": True}
    assert set(country_scores([both], 1.0, NOW)) == {"XX", "YY"}


def test_world_mean_uses_the_last_thirty_days():
    assert world_mean([story(stress=2.0), story(age_days=31, stress=0.0)], NOW) == 2.0


def test_world_mean_of_nothing_is_neutral():
    assert world_mean([], NOW) == 1.0


def test_dot_fires_on_a_spike():
    assert "XX" in red_dots(history(2) + spike(8), {}, NOW)


def test_quiet_country_can_fire():
    assert "XX" in red_dots([story(age_days=13)] + spike(4), {}, NOW)


def test_big_country_needs_a_big_jump():
    assert red_dots(history(50) + spike(55), {}, NOW) == {}


def test_a_calm_spike_stays_silent():
    assert red_dots(history(2) + spike(8, stress=1.0), {}, NOW) == {}


def test_one_outlet_is_not_enough():
    assert red_dots(history(2) + spike(8, outlets=1), {}, NOW) == {}


def test_outlet_fallback_never_fires():
    assert red_dots(history(2) + spike(8, from_title=False), {}, NOW) == {}


def test_short_history_cannot_fire():
    assert red_dots(history(2, days=5) + spike(8), {}, NOW) == {}


def test_two_outlets_are_enough():
    assert "XX" in red_dots(history(2) + spike(8, outlets=2), {}, NOW)


def test_a_mild_spike_fires():
    assert "XX" in red_dots(history(2) + spike(5, stress=1.8), {}, NOW)


def test_a_hot_story_outside_the_window_does_not_fire():
    quiet_now = [story(age_days=0.1, country="XX") for _ in range(8)]
    assert red_dots(history(2) + quiet_now + [story(age_days=0.5, outlets=5, stress=2.5)], {}, NOW) == {}


def test_a_past_burst_has_cleared():
    burst = [story(age_days=0.5, outlets=3, stress=2.5) for _ in range(8)]
    assert red_dots(history(2) + burst, {}, NOW) == {}


def test_dot_names_its_story():
    dot = red_dots(history(2) + spike(8), {}, NOW)["XX"]
    assert (dot["lat"], dot["lon"], dot["outlets"], dot["since"]) == (1.0, 2.0, 3, NOW)


def test_each_country_dot_sits_in_its_own_country():
    both = spike(8)
    for s in both:
        s.countries, s.positions = {"XX": True, "YY": True}, {"XX": (1.0, 2.0), "YY": (3.0, 4.0)}
    dots = red_dots(history(2) + history(2, country="YY") + both, {}, NOW)
    assert (dots["XX"]["lat"], dots["YY"]["lat"]) == (1.0, 3.0)


def test_dot_keeps_its_start_time():
    first = red_dots(history(2) + spike(8), {}, NOW)
    assert red_dots(history(2) + spike(8), first, NOW + 60)["XX"]["since"] == NOW


def test_dot_holds_for_a_day():
    first = red_dots(history(2) + spike(8), {}, NOW)
    assert "XX" in red_dots([], first, NOW + 23 * 3600)


def test_dot_clears_after_a_day():
    first = red_dots(history(2) + spike(8), {}, NOW)
    assert red_dots([], first, NOW + 25 * 3600) == {}


def titles(output):
    return [s["title"] for s in output["countries"]["XX"]["top"]]


def test_top_has_the_most_stressful_recent_stories_then_a_calm_one():
    stories = [story(stress=2.0 + i / 10, title=f"worst {i}") for i in range(6)] + [story(stress=0.2, title="calm")]
    assert titles(build_output(stories, {}, NOW)) == ["worst 5", "worst 4", "worst 3", "worst 2", "worst 1", "calm"]


def test_top_needs_no_calm_story():
    assert titles(build_output([story(stress=2.0, title="only")], {}, NOW)) == ["only"]


def test_output_carries_no_stress_numbers():
    assert "stress" not in build_output([story()], {}, NOW)["countries"]["XX"]["top"][0]


def test_output_counts_the_last_day():
    assert build_output([story(), story(age_days=2)], {}, NOW)["countries"]["XX"]["stories_24h"] == 1


def test_output_lists_dots():
    held = red_dots(history(2) + spike(8), {}, NOW)
    dot = build_output([], held, NOW)["dots"][0]
    assert dot["country"] == "XX" and "held" not in dot


def test_empty_input():
    output = build_output([], {}, NOW)
    assert (output["countries"], output["dots"], output["generated_at"]) == ({}, [], NOW)


def test_constants_are_the_documented_ones():
    assert (scoring.HALF_LIFE_HOURS, scoring.PRIOR, scoring.NO_SIGNAL) == (48, 10, 3)
    assert (scoring.BLUE, scoring.ORANGE) == (0.87, 1.39)
    assert (scoring.DOT_WINDOW_HOURS, scoring.DOT_Z, scoring.DOT_MIN_OUTLETS, scoring.DOT_STRESS) == (6, 2, 2, 1.8)


def test_baseline_covers_all_fourteen_days():
    assert scoring.blocks_known(NOW - 14.3 * DAY, NOW) == 56


def test_baseline_stops_at_fourteen_days():
    assert scoring.blocks_known(NOW - 40 * DAY, NOW) == 56


def test_baseline_counts_only_whole_blocks():
    assert scoring.blocks_known(NOW - 7 * DAY, NOW) == 27
