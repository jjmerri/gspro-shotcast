from commentator.models import Line
from commentator.prompts import closer_prompt
from commentator.state import SessionState
from commentator.wordwatch import WordWatch


def test_a_pet_word_is_flagged_on_its_third_line_and_not_before():
    w = WordWatch()
    w.add("That's a tidy finish from Josh.")
    w.add("A tidier strike this time, Nigel.")
    assert w.overused() == []
    w.add("He keeps it tidy and walks off with par.")
    assert w.overused() == [("tidy", 3)]


def test_names_golf_terms_and_plain_facts_never_count():
    w = WordWatch()
    for _ in range(4):
        w.add("Derek's lob wedge from the deep rough finishes short and left of the green.")
    assert w.overused() == []


def test_the_booth_hears_its_leaned_on_words_in_the_prompt_and_a_new_round_forgets_them():
    state = SessionState()
    state.record_lines([Line("Nigel", "A gentle touch from Josh."), Line("Duke", "Gently does it, Nigel."),
                        Line("Nigel", "Another gentle roll."), Line("Duke", "That's a lovely touch."),
                        Line("Nigel", "Such a gentle stroke."), Line("Duke", "Proper touch, that.")])
    user, _ = closer_prompt(facts=["Josh shot 37."], recent_lines=state.recent_lines)
    assert "leaned on this round: gentle (3 times), touch (3)." in user
    state.recent_lines.words.reset()
    user, _ = closer_prompt(facts=["Josh shot 37."], recent_lines=state.recent_lines)
    assert "leaned on" not in user
