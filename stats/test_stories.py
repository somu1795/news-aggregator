"""Tests for joining headlines into stories."""

import numpy as np

from stories import StoryIndex

HOUR = 3600.0


def vector(cosine):
    return np.array([cosine, (1 - cosine ** 2) ** 0.5] + [0.0] * 382, dtype=np.float32)


def index_with_story(first_seen=0.0):
    index = StoryIndex()
    index.insert(7, vector(1.0), first_seen)
    return index


def test_close_vectors_join_a_story():
    assert index_with_story().match(vector(0.95), 1.0) == 7


def test_distant_vectors_do_not():
    assert index_with_story().match(vector(0.5), 1.0) is None


def test_threshold_is_inclusive():
    assert index_with_story().match(vector(0.92), 1.0) == 7


def test_the_closest_story_wins():
    index = index_with_story()
    index.insert(8, vector(0.99), 0.0)
    assert index.match(vector(0.99), 1.0) == 8


def test_empty_index_matches_nothing():
    assert StoryIndex().match(vector(1.0), 0.0) is None


def test_old_stories_expire():
    index = index_with_story()
    index.expire(73 * HOUR)
    assert index.match(vector(1.0), 73 * HOUR) is None


def test_recent_stories_stay():
    index = index_with_story()
    index.expire(71 * HOUR)
    assert index.match(vector(1.0), 71 * HOUR) == 7


def test_many_stories_fit():
    index = StoryIndex()
    for i in range(5000):
        index.insert(i, vector(0.0), 0.0)
    index.insert(9999, vector(1.0), 0.0)
    assert index.match(vector(1.0), 1.0) == 9999


def test_an_old_story_does_not_hide_a_recent_one():
    index = StoryIndex()
    index.insert(1, vector(1.0), 0.0)
    index.insert(2, vector(0.95), 71 * HOUR)
    assert index.match(vector(1.0), 73 * HOUR) == 2


def test_an_expired_index_can_grow_again():
    index = index_with_story()
    index.expire(73 * HOUR)
    index.insert(8, vector(1.0), 73 * HOUR)
    assert index.match(vector(1.0), 73 * HOUR) == 8
