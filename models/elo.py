"""
Basit Elo derecelendirme sistemi (futbol maçları için).

Mantık:
- Her takım 1500 puanla başlar.
- Ev sahibine sanal bir avantaj puanı eklenir (ELO_HOME_ADVANTAGE).
- Beklenen sonuç = 1 / (1 + 10^((rakip_puan - kendi_puan)/400))
- Gerçek sonuç: galibiyet=1, berabere=0.5, mağlubiyet=0
- Gol farkı büyükse K katsayısı biraz artırılır (çok farklı galibiyetler
  daha fazla puan taşır) - basit bir "margin of victory" düzeltmesi.
"""
import os
import sys
from typing import Dict, Tuple

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


class EloRatings:
    def __init__(self, start: float = None, k: float = None, home_advantage: float = None):
        self.start = start if start is not None else config.ELO_START
        self.k = k if k is not None else config.ELO_K
        self.home_advantage = home_advantage if home_advantage is not None else config.ELO_HOME_ADVANTAGE
        self.ratings: Dict[str, float] = {}

    def _get(self, team: str) -> float:
        return self.ratings.setdefault(team, self.start)

    def expected_score(self, team: str, opponent: str, is_home: bool) -> float:
        team_r = self._get(team) + (self.home_advantage if is_home else 0)
        opp_r = self._get(opponent) + (0 if is_home else self.home_advantage)
        return 1 / (1 + 10 ** ((opp_r - team_r) / 400))

    def _margin_multiplier(self, goal_diff: int) -> float:
        # Büyük skor farkı = biraz daha büyük puan transferi (üst sınırla)
        if goal_diff <= 1:
            return 1.0
        return min(1.75, 1.0 + 0.15 * (goal_diff - 1))

    def update_match(self, home: str, away: str, home_goals: int, away_goals: int) -> None:
        exp_home = self.expected_score(home, away, is_home=True)
        exp_away = 1 - exp_home

        if home_goals > away_goals:
            actual_home, actual_away = 1.0, 0.0
        elif home_goals < away_goals:
            actual_home, actual_away = 0.0, 1.0
        else:
            actual_home, actual_away = 0.5, 0.5

        margin = self._margin_multiplier(abs(home_goals - away_goals))

        home_r = self._get(home)
        away_r = self._get(away)

        self.ratings[home] = home_r + self.k * margin * (actual_home - exp_home)
        self.ratings[away] = away_r + self.k * margin * (actual_away - exp_away)

    def win_draw_loss_prob(self, home: str, away: str) -> Tuple[float, float, float]:
        """
        Elo'dan kabaca 1-X-2 olasılığı üretir. Elo doğası gereği beraberliği
        doğrudan modellemez; burada basit bir yaklaşım kullanılır: puan
        farkına göre bir "beraberlik payı" ayrılır, kalan galibiyet olasılığı
        Elo beklenen skoruna göre home/away'e bölüştürülür.
        Not: Asıl skor/1-X-2 tahmini bu projede poisson_model ile yapılır;
        bu fonksiyon sadece Elo'yu tek başına yorumlamak içindir.
        """
        exp_home = self.expected_score(home, away, is_home=True)
        rating_gap = abs(self._get(home) + self.home_advantage - self._get(away))

        # Puan farkı küçükse beraberlik olasılığı daha yüksek
        draw_prob = max(0.18, 0.32 - rating_gap / 1600)

        home_win = exp_home * (1 - draw_prob)
        away_win = (1 - exp_home) * (1 - draw_prob)

        return home_win, draw_prob, away_win

    def get_rating(self, team: str) -> float:
        return self._get(team)

    def ranked_table(self):
        return sorted(self.ratings.items(), key=lambda kv: kv[1], reverse=True)
