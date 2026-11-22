"""What the booth remembers saying: form mentions, asides, news and predictions.

Part of Store (commentator/store/core.py), which is these parts together."""
import time


class MemoryMixin:
    """What the booth remembers saying: form mentions, asides, news and predictions."""

    def record_form_mentions(self, players: list[str], topics: list[str]) -> None:
        """Round-sheet angles the booth used on these players, so later rounds can steer it elsewhere."""
        now = time.time()
        self.db.executemany("INSERT INTO form_mentions (session_id, ts, player, topic) VALUES (?, ?, ?, ?)",
                            [(self.session_id, now, p, t) for p in players for t in topics])
        self.db.commit()

    def record_aside(self, kind: str, topic: str, text: str) -> None:
        self.db.execute("INSERT INTO asides (session_id, ts, kind, topic, text) VALUES (?, ?, ?, ?, ?)",
                        (self.session_id, time.time(), kind, topic, text))
        self.db.commit()

    def record_news_heard(self, players: list[str], title: str) -> None:
        now = time.time()
        self.db.executemany("INSERT INTO news_heard (session_id, ts, player, title) VALUES (?, ?, ?, ?)",
                            [(self.session_id, now, p, title) for p in players])
        self.db.commit()

    def news_heard(self, players: list[str]) -> set[str]:
        """Titles of the stories any of these players has already heard from the booth."""
        if not players:
            return set()
        rows = self.db.execute(
            f"SELECT DISTINCT title FROM news_heard WHERE player IN ({','.join('?' * len(players))})", players)
        return {r["title"] for r in rows}

    def recent_asides(self, kind: str, topic: str, limit: int = 12) -> list[str]:
        """What the booth has already told on this topic (a course's trivia), most recent first."""
        rows = self.db.execute("SELECT text FROM asides WHERE kind = ? AND topic = ? ORDER BY ts DESC LIMIT ?",
                               (kind, topic, limit)).fetchall()
        return [r["text"] for r in rows]

    def recent_form_mentions(self, player: str, sessions: int = 3) -> list[str]:
        """Angles used on this player in their last few sessions before today, most used first."""
        rows = self.db.execute(
            f"""SELECT topic, COUNT(*) AS n FROM form_mentions
                WHERE player = ? AND {self._BEFORE_TODAY} AND session_id IN (
                    SELECT DISTINCT session_id FROM form_mentions WHERE player = ? AND {self._BEFORE_TODAY}
                    ORDER BY session_id DESC LIMIT ?)
                GROUP BY topic ORDER BY n DESC, topic""",
            (player, self.session_id, player, self.session_id, sessions),
        ).fetchall()
        return [r["topic"] for r in rows]

    def record_prediction(self, commentator: str, kind: str, predicted: str, actual: str,
                          correct: bool) -> None:
        """A resolved commentator prediction, for the season call-accuracy stat."""
        self.db.execute(
            "INSERT INTO predictions (session_id, ts, commentator, kind, predicted, actual, correct) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.session_id, time.time(), commentator, kind, predicted, actual, int(correct)),
        )
        self.db.commit()

    def prediction_record(self, commentator: str) -> dict[str, int]:
        """How many of a commentator's resolved calls were right, this season (across nights)."""
        r = self.db.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(correct), 0) AS hits FROM predictions "
            "WHERE commentator = ? AND correct IS NOT NULL",
            (commentator,),
        ).fetchone()
        return {"total": r["total"], "hits": r["hits"]}
