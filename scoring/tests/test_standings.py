from shotcast_scoring.board import Board, Entry, LeagueData, place, place_points

POINTS = [70.0, 60.0, 50.0]


def test_places_ties_and_points():
    assert place(-3, [-5, -3, -3, 0]) == (2, True)
    assert place(-5, [-5, -3, -3, 0]) == (1, False)
    # A tie splits the places it covers; places past the table score nothing.
    assert place_points(1, 2, POINTS) == 65.0 and place_points(3, 2, POINTS) == 25.0


def test_a_board_over_plain_league_data():
    league = LeagueData(team_players={"Aces": ["Mike"], "Duffers": ["Tony"], "Hackers": ["Lou"]},
                        schedule=[{"week": 6, "course": "Links", "end_date": "2026-12-06"}],
                        team_names={"Aces": "The Aces"}, flights={"Aces": ("g", "Gold"), "Duffers": ("g", "Gold")})
    b = Board({6: [Entry("Aces", -2), Entry("Duffers", -4), Entry("Hackers", None)]}, league, POINTS).with_flights()
    page = b.page("2026-12-07")
    assert page["week"]["course"] == "Links" and page["week"]["final"]
    assert [(r["team"], r["flight"]) for r in page["week"]["rows"]] == [("Duffers", "Gold"), ("The Aces", "Gold")]
    assert page["week"]["waiting"] == ["Hackers"]
    # Each flight places and stands on its own; Hackers, in none, never posted.
    assert b.season("2026-12-07") == ([("g", [("Duffers", 70.0, 1), ("Aces", 60.0, 2)]), (None, [("Hackers", 0.0, 1)])], 1)

