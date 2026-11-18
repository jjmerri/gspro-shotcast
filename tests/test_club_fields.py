from commentator import club_fields
from commentator.hud import Hud
from commentator.models import Shot
from commentator.store import Store


def club(name: str | None, **kw) -> Shot:
    return Shot(club=name, club_measured=True, ball_speed=120.0, **kw)


def hit(st: Store, player: str | None, shot: Shot, attributed: bool = True) -> Shot:
    """The shot as the booth keeps it, recorded as the engine records it."""
    shot = club_fields.apply(player, shot, st.club_fields_seen)
    st.record_shot(player=player, hole="Hole 1", shot=shot, before=Hud(), after=Hud(), tags=[],
                   carry_game_yards=None, total_yards=None, offline_yards=None, attributed=attributed)
    return shot


def test_a_field_counts_once_it_comes_through_non_zero():
    st = Store(None)
    first = hit(st, "Mike", club("7I", club_speed=85.0, path=-1.5, face_to_target=0.0))
    assert (first.club_speed, first.path, first.face_to_target) == (85.0, -1.5, None)
    hit(st, "Mike", club("7I", club_speed=86.0, path=0.0, face_to_target=2.0))
    square = hit(st, "Mike", club("7I", club_speed=84.0, path=0.0, face_to_target=0.0))
    # Both are measured now: their zeros are a square face and a straight path.
    assert (square.club_speed, square.path, square.face_to_target) == (84.0, 0.0, 0.0)


def test_each_club_and_player_is_learned_on_its_own():
    st = Store(None)
    hit(st, "Mike", club("DR", club_speed=105.0, face_to_target=1.2))   # stickers on the driver only
    iron = hit(st, "Mike", club("7I", club_speed=85.0, face_to_target=0.0))
    assert iron.club_speed == 85.0 and iron.face_to_target is None
    assert hit(st, "Steve", club("DR", club_speed=100.0, face_to_target=0.0)).face_to_target is None
    # Nor from a shared ball, which a partner may have hit.
    hit(st, "Ann", club("DR", club_speed=99.0, face_to_target=-1.0), attributed=False)
    assert hit(st, "Ann", club("DR", club_speed=98.0, face_to_target=0.0)).face_to_target is None


def test_a_shot_reading_zero_on_every_known_field_is_a_misread():
    st = Store(None)
    hit(st, "Mike", club("DR", club_speed=105.0, path=2.0, angle_of_attack=3.0))
    blank = hit(st, "Mike", club("DR", club_speed=0.0, path=0.0, angle_of_attack=0.0, loft=0.0))
    assert (blank.club_speed, blank.path, blank.angle_of_attack, blank.loft) == (None, None, None, None)
    # A misread teaches nothing: the loft's later zero is still unmeasured, its path zero a real one.
    later = hit(st, "Mike", club("DR", club_speed=104.0, path=0.0, loft=0.0))
    assert (later.club_speed, later.path, later.loft) == (104.0, 0.0, None)
    # A shot with none of the club data known for the club is missing it, whatever else it says.
    hit(st, "Ann", club("7I", club_speed=0.0, path=1.0))
    assert hit(st, "Ann", Shot(club="7I", club_speed=85.0)).club_speed is None


def test_a_shot_with_no_club_or_player_is_left_alone():
    st = Store(None)
    shot = club(None, club_speed=0.0, path=0.0)
    assert club_fields.apply("Mike", shot, st.club_fields_seen) is shot
    assert club_fields.apply(None, club("7I", club_speed=0.0), st.club_fields_seen).club_speed == 0.0


def test_what_each_club_measures_survives_a_restart(tmp_path):
    path = str(tmp_path / "season.db")
    st = Store(path)
    hit(st, "Mike", club("DR", club_speed=105.0, face_to_target=1.2, horizontal_face_impact=0.0))
    hit(st, "Mike", club("7I", club_speed=85.0, face_to_target=0.0))
    st.db.close()

    st = Store(path)
    drive = hit(st, "Mike", club("DR", club_speed=103.0, face_to_target=0.0, horizontal_face_impact=0.0))
    assert (drive.club_speed, drive.face_to_target, drive.horizontal_face_impact) == (103.0, 0.0, None)
    assert hit(st, "Mike", club("7I", club_speed=84.0, face_to_target=0.0)).face_to_target is None
