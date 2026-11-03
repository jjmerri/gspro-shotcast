import json
from datetime import date

from commentator import recall

TODAY = date(2026, 11, 3)


def _row(day="2026-11-02", course="Pebble", gross=40, par=36, played=9, to_par9=None, scores=None, pars=None,
         **kw):
    pars = pars or {str(h): 4 for h in range(1, 10)}
    scores = scores or {h: 4 for h in pars}
    return {"date": day, "course": course, "gross": gross, "par": par, "played": played, "is_league": 0,
            "holes": played, "complete": 1,
            "to_par9": gross - par if to_par9 is None else to_par9, "scores": json.dumps(scores),
            "hole_pars": json.dumps(pars), "putts": 16, "putt_holes": 9, "fir_hit": 3, "fir_tries": 7,
            "gir_hit": 4, "gir_tries": 9, **kw}


def test_the_last_round_says_when_where_and_the_score():
    assert recall.last_round_fact("Dan", [_row()], TODAY) == \
        "Dan's last round: yesterday, 40 (4 over par) for 9 holes at Pebble."
    assert recall.last_round_fact("Dan", [_row(day="2026-10-06", is_league=1)], TODAY) == \
        "Dan's last round in the league: on October 6, 4 weeks ago, 40 (4 over par) for 9 holes at Pebble."
    assert recall.last_round_fact("Dan", [], TODAY) is None


def test_a_standout_is_measured_against_the_sides_own_usual():
    usual = [_row(gross=g) for g in (40, 41, 42)]
    assert recall.standout("Dan", [_row(gross=39), *usual]).text == "That was Dan's best round of the season."
    # Within the norm on every count: nothing stands out.
    assert recall.standout("Dan", [_row(gross=41), *usual]) is None
    # 22 putts against 16 a nine is a putting story, told on a putt.
    st = recall.standout("Dan", [_row(gross=41, putts=22), *usual])
    assert st.moment == "putt" and "22 putts over 9 holes" in st.text and "1.8 usually" in st.text
    # Too few earlier rounds for a usual: only the round's own blow-up hole can stand out.
    blew = _row(gross=43, scores={**{str(h): 4 for h in range(1, 10)}, "7": 7})
    assert recall.standout("Dan", [blew, usual[0]]) == recall.Standout(
        "score", "In that round Dan made a 7 on the par-4 hole 7, a triple bogey.")


def test_a_hole_fact_needs_a_clear_pattern_on_that_hole():
    def here(**scores):
        return _row(scores={**{str(h): 4 for h in range(1, 10)}, **scores})
    assert recall.hole_fact("Dan", [here(**{"3": 5}), here(**{"3": 6})], "pebble", 3) == \
        "Dan has made bogey or worse on this hole both times they've played it before."
    assert recall.hole_fact("Dan", [here(**{"3": 5}), here(**{"3": 4})], "Pebble", 3) is None   # mixed
    assert recall.hole_fact("Dan", [here(), here(), here()], "Pebble", 3) == \
        "Dan has never dropped a shot on this hole in 3 rounds here."
    assert recall.hole_fact("Dan", [here(**{"3": 8}), here(**{"3": 3})], "Pebble", 3) == \
        "Last time here Dan made an 8 on this hole, a quadruple bogey."
    assert recall.hole_fact("Dan", [here(**{"3": 5}), here(**{"3": 5})], "Augusta", 3) is None   # another course


def test_the_course_fact_tells_a_first_visit_only_against_a_real_record():
    rows = [_row(course="Augusta"), _row(course="Augusta"), _row(course="Pebble", day="2026-10-20", gross=38),
            _row(course="Pebble", day="2026-10-10", gross=41)]
    assert recall.course_fact("Dan", rows, "pebble", TODAY) == \
        "Dan has played Pebble 2 times before, most recently on October 20, 2 weeks ago; best: 38 (2 over par) for 9 holes."
    assert recall.course_fact("Dan", rows, "Bethpage", TODAY) == "This is Dan's first recorded round at Bethpage."
    assert recall.course_fact("Dan", rows[:2], "Bethpage", TODAY) is None


def test_missing_counts_and_partial_pars_say_nothing_rather_than_something_wrong():
    usual = [_row(gross=g) for g in (40, 41, 42)]
    assert recall.standout("Dan", [_row(gross=41, putts=None, fir_hit=None, gir_hit=None), *usual]) is None
    # A round quit after a -2 nine and two triples isn't the season's best.
    quit = _row(gross=48, par=44, played=11, holes=18, complete=0, to_par9=-2, pars={str(h): 4 for h in range(1, 12)},
                scores={**{str(h): 4 for h in range(1, 10)}, "1": 3, "2": 3, "10": 7, "11": 7})
    assert recall.standout("Dan", [quit, *usual]).text == "In that round Dan made a 7 on the par-4 hole 10, a triple bogey."
    # A ten-hole round whose tenth par is unknown has no score to par to be a best with.
    partial = _row(gross=41, played=10, to_par9=0, scores={**{str(h): 4 for h in range(1, 10)}, "10": 5})
    rows = [_row(gross=42), partial]
    assert recall.course_fact("Dan", rows, "Pebble", TODAY).endswith("; best: 42 (6 over par) for 9 holes.")
    # An abandoned eleven ranks by its finished nine; it isn't shown as the course best.
    quit = _row(gross=52, played=11, holes=18, complete=0, to_par9=-2,
                pars={str(h): 4 for h in range(1, 12)}, scores={**{str(h): 4 for h in range(1, 11)}, "11": 12})
    assert recall.course_fact("Dan", [_row(gross=37), quit], "Pebble", TODAY).endswith(
        "; best: 37 (1 over par) for 9 holes.")
