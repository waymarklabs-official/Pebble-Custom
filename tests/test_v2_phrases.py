"""Voice phrase patterns: slots, filler words, whole-utterance matching, the raw-regex escape."""

from __future__ import annotations

import pytest

from ringhub.phrases import PhraseIndex, compile_phrase


def match(pattern: str, said: str) -> dict[str, str] | None:
    m = PhraseIndex([("s", pattern)]).match(said)
    return m.slots if m else None


@pytest.mark.parametrize("said", [
    "warm up the car", "Warm up the car.", "WARM UP THE CAR!", "warm  up   the car",
    "hey warm up the car", "ok please warm up the car", "can you warm up the car please",
    "could you warm up the car now", "would you warm up the car thanks", "go ahead and warm up the car",
    "i'd like to warm up the car thank you", "Okay, can you warm up the car, please?",
])
def test_literal_phrase_with_polite_filler(said: str) -> None:
    assert match("warm up the car", said) == {}


@pytest.mark.parametrize("said", [
    "remind me to warm up the car at 7",   # a reminder that mentions the command
    "warm up the car at 7",
    "don't warm up the car",
    "warm up the",
    "warm up the cart",
    "please warm the car",
])
def test_whole_utterance_must_match(said: str) -> None:
    assert match("warm up the car", said) is None


def test_text_slot_takes_the_rest() -> None:
    assert match("tell {who} i'm running late", "Tell Sarah and Bob I’m running late") == {"who": "sarah and bob"}
    assert match("tell {who} i'm running late", "tell i'm running late") is None  # slot can't be empty


@pytest.mark.parametrize("said,minutes", [
    ("set focus timer for 25", "25"),
    ("set focus timer for twenty five", "25"),
    ("set focus timer for twenty-five", "25"),
    ("set focus timer for 1.5", "1.5"),
    ("set focus timer for seven", "7"),
])
def test_number_slot_accepts_spoken_numbers(said: str, minutes: str) -> None:
    assert match("set focus timer for {minutes:number}", said) == {"minutes": minutes}


def test_number_slot_rejects_words() -> None:
    assert match("set focus timer for {minutes:number}", "set focus timer for later") is None


def test_word_slot_is_one_word() -> None:
    assert match("call {who:word}", "call mom") == {"who": "mom"}
    assert match("call {who:word}", "call mom's") == {"who": "mom's"}
    assert match("call {who:word}", "call my mom") is None


def test_several_slots_and_literal_numbers() -> None:
    p = "focus for {minutes:number} minutes on {task}"
    assert match(p, "focus for thirty minutes on the report") == {"minutes": "30", "task": "the report"}
    # literal words are normalized like transcripts: "one" in a pattern equals "1" spoken
    assert match("play episode one", "play episode 1") == {}


def test_regex_escape_hatch() -> None:
    p = "re:^(?:open|pop) the (?P<which>trunk|frunk)$"
    assert match(p, "Pop the frunk!") == {"which": "frunk"}
    assert match(p, "open the trunk") == {"which": "trunk"}
    assert match(p, "open the door") is None


def test_regex_metacharacters_in_literals_are_escaped() -> None:
    # A literal pattern never behaves like a regex, whatever characters it contains.
    assert match("lights to 50.5", "lights to 50.5") == {}
    assert match("lights to 50.5", "lights to 5005") is None
    assert match("a/b test", "a/b test") == {}


@pytest.mark.parametrize("bad,message", [
    ("", "empty"),
    ("   ", "empty"),
    ("?!", "empty"),
    ("tell {who", "malformed"),
    ("tell who}", "malformed"),
    ("tell {Who} hi", "malformed"),      # slot names are lowercase
    ("tell {1who} hi", "malformed"),
    ("set {x:float}", "malformed"),      # unknown slot kind
    ("tell {} hi", "malformed"),
    ("{who} and {who}", "twice"),
    ("re:(unclosed", "bad regex"),
])
def test_malformed_patterns_are_rejected(bad: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        compile_phrase(bad)


def test_index_respects_priority_order() -> None:
    idx = PhraseIndex([("first", "open the {what}"), ("second", "open the trunk")])
    m = idx.match("open the trunk")
    assert m is not None and (m.skill_id, m.slots, m.phrase) == ("first", {"what": "trunk"}, "open the {what}")
    idx2 = PhraseIndex([("second", "open the trunk"), ("first", "open the {what}")])
    assert idx2.match("open the trunk").skill_id == "second"  # type: ignore[union-attr]
    assert idx2.match("close the trunk") is None


def test_transcript_cannot_inject_regex() -> None:
    idx = PhraseIndex([("s", "tell {who} hi")])
    m = idx.match("tell .* (?P<x>y) hi")
    assert m is not None and m.slots == {"who": "p x y"}  # punctuation normalized away, data stays data
