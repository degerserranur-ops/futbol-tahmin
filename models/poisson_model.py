"""
Dixon-Coles tarzı basitleştirilmiş Poisson gol modeli.

Fikir:
- Her takım için hücum gücü (attack) ve savunma zayıflığı (defense) katsayısı
  hesaplanır: lig ortalamasına göre normalize edilmiş, attığı/yediği gol oranı.
- Bir maçta:
    ev_beklenen_gol = lig_ort_ev_gol * ev_attack * rakip_defense * ev_avantaji
    dep_beklenen_gol = lig_ort_dep_gol * dep_attack * rakip_defense
- Bu iki beklenen gol (lambda) değeriyle Poisson dağılımından 0-9 gol için
  olasılık matrisi kurulur -> 1-X-2, KG var/yok, 2.5 üst/alt olasılıkları
  bu matristen toplanarak çıkarılır.

Bu, gerçek bahis şirketlerinin kullandığı modellere göre basitleştirilmiş bir
yaklaşımdır; amaç istatistiksel bir tahmin/analiz aracı sunmaktır, kesin sonuç
garantisi değildir.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import poisson


@dataclass
class MatchResult:
    home: str
    away: str
    home_goals: int
    away_goals: int
    # İlk yarı (devre arası) gol sayıları -- SADECE football-data.org kaynaklı
    # maçlarda gerçek veri var (FlashScore'un tournaments/results ucu devre arası
    # skorunu vermiyor). None ise bu maç ilk-yarı oranı hesabında kullanılmaz,
    # ama tam-maç (attack/defense) eğitimine normal şekilde dahil olur.
    home_goals_ht: Optional[int] = None
    away_goals_ht: Optional[int] = None


@dataclass
class TeamStrength:
    attack: float = 1.0
    defense: float = 1.0
    home_games: int = 0
    away_games: int = 0


class PoissonGoalModel:
    def __init__(self, home_advantage: float = 1.25, max_goals: int = 9):
        self.home_advantage = home_advantage
        self.max_goals = max_goals
        self.strengths: Dict[str, TeamStrength] = {}
        self.league_avg_home_goals = 1.4
        self.league_avg_away_goals = 1.1
        # İlk yarıda oynanan golün tüm maç golüne oranı -- "İlk Yarı Tahmini"
        # (bkz. predict()) için kullanılır. Gerçek devre-arası verisi varsa
        # fit() içinde bu oran hesaba göre güncellenir; yoksa (örn. sadece
        # FlashScore'dan beslenen ligler) futbol istatistiklerinde yaygın
        # kullanılan ~%45 varsayımında kalır.
        self.first_half_ratio = 0.45

    def fit(self, matches: List[MatchResult]) -> None:
        if not matches:
            raise ValueError("Model eğitmek için en az bir maç sonucu gerekli.")

        teams = set()
        for m in matches:
            teams.add(m.home)
            teams.add(m.away)

        total_home_goals = sum(m.home_goals for m in matches)
        total_away_goals = sum(m.away_goals for m in matches)
        n = len(matches)
        self.league_avg_home_goals = total_home_goals / n
        self.league_avg_away_goals = total_away_goals / n

        goals_for: Dict[str, List[float]] = {t: [] for t in teams}
        goals_against: Dict[str, List[float]] = {t: [] for t in teams}

        for m in matches:
            # ev sahibinin attığı gol, lig ev ortalamasına göre normalize
            goals_for[m.home].append(m.home_goals / self.league_avg_home_goals)
            goals_against[m.home].append(m.away_goals / self.league_avg_away_goals)
            # deplasman takımının attığı gol, lig deplasman ortalamasına göre
            goals_for[m.away].append(m.away_goals / self.league_avg_away_goals)
            goals_against[m.away].append(m.home_goals / self.league_avg_home_goals)

        for t in teams:
            gf = goals_for[t]
            ga = goals_against[t]
            attack = float(np.mean(gf)) if gf else 1.0
            defense = float(np.mean(ga)) if ga else 1.0
            self.strengths[t] = TeamStrength(attack=attack, defense=defense)

        # --- İlk yarı oranı: gerçek devre-arası verisi olan maçlardan (varsa) ---
        ht_total = 0
        ft_total = 0
        for m in matches:
            if m.home_goals_ht is not None and m.away_goals_ht is not None:
                ht_total += m.home_goals_ht + m.away_goals_ht
                ft_total += m.home_goals + m.away_goals
        if ft_total > 0 and ht_total > 0:
            ratio = ht_total / ft_total
            # makul aralıkta tut (çok az maç varsa aşırı sapmasın)
            self.first_half_ratio = float(np.clip(ratio, 0.30, 0.60))
        # yoksa __init__'teki varsayılan (0.45) olduğu gibi kalır

    def _get_strength(self, team: str) -> TeamStrength:
        if team not in self.strengths:
            # veri yoksa lig ortalaması (nötr) kabul edilir
            return TeamStrength(attack=1.0, defense=1.0)
        return self.strengths[team]

    def expected_goals(
        self, home: str, away: str,
        home_attack_mult: float = 1.0, away_attack_mult: float = 1.0,
    ) -> Tuple[float, float]:
        home_s = self._get_strength(home)
        away_s = self._get_strength(away)

        lambda_home = (
            self.league_avg_home_goals
            * home_s.attack
            * home_attack_mult
            * away_s.defense
            * self.home_advantage
            / 1.0
        )
        lambda_away = self.league_avg_away_goals * away_s.attack * away_attack_mult * home_s.defense

        # aşırı uç değerleri makul aralıkta tut
        lambda_home = float(np.clip(lambda_home, 0.15, 5.5))
        lambda_away = float(np.clip(lambda_away, 0.15, 5.5))
        return lambda_home, lambda_away

    def score_matrix(
        self, home: str, away: str,
        home_attack_mult: float = 1.0, away_attack_mult: float = 1.0,
    ) -> np.ndarray:
        lam_h, lam_a = self.expected_goals(home, away, home_attack_mult, away_attack_mult)
        goals = np.arange(0, self.max_goals + 1)
        home_probs = poisson.pmf(goals, lam_h)
        away_probs = poisson.pmf(goals, lam_a)
        matrix = np.outer(home_probs, away_probs)
        matrix /= matrix.sum()  # kesilen kuyruğu normalize et
        return matrix

    def predict(
        self, home: str, away: str,
        home_attack_mult: float = 1.0, away_attack_mult: float = 1.0,
    ) -> Dict:
        """home_attack_mult / away_attack_mult: kadroda kilit bir oyuncu eksikse
        (bkz. export_excel.enrich_upcoming_matches) o tarafın hücum gücünü
        hafifçe düşürmek için kullanılır (1.0 = etkisiz)."""
        matrix = self.score_matrix(home, away, home_attack_mult, away_attack_mult)
        lam_h, lam_a = self.expected_goals(home, away, home_attack_mult, away_attack_mult)

        home_win = float(np.tril(matrix, -1).sum())
        draw = float(np.trace(matrix))
        away_win = float(np.triu(matrix, 1).sum())

        goals_idx = np.arange(matrix.shape[0])
        total_goals_grid = goals_idx[:, None] + goals_idx[None, :]
        over_2_5 = float(matrix[total_goals_grid > 2].sum())
        under_2_5 = 1 - over_2_5

        btts_yes = float(matrix[1:, 1:].sum())
        btts_no = 1 - btts_yes

        # 1.5 gol üst/alt (toplamda 2 veya daha fazla gol olma olasılığı)
        over_1_5 = float(matrix[total_goals_grid > 1].sum())
        under_1_5 = 1 - over_1_5

        # en olası skorlar (ilk 3)
        flat_idx = np.argsort(matrix, axis=None)[::-1][:3]
        top_scores = []
        for idx in flat_idx:
            h, a = np.unravel_index(idx, matrix.shape)
            top_scores.append({"skor": f"{h}-{a}", "olasilik": float(matrix[h, a])})

        # çifte şans (double chance) -- 1X2'den direkt türetiliyor, ek veri gerekmiyor
        cifte_sans = {
            "1x": round(min(home_win + draw, 1.0), 3),
            "x2": round(min(draw + away_win, 1.0), 3),
            "12": round(min(home_win + away_win, 1.0), 3),
        }

        # ilk yarı (devre arası) tahmini -- tam maç lambda'sı self.first_half_ratio
        # ile ölçeklenip AYNI Poisson yöntemiyle ayrı bir 1-X-2 çıkarılıyor.
        # Not: takım bazlı değil, lig/genel bir oranla yaklaşıklanıyor --
        # gerçek devre-arası verisi olmayan liglerde (çoğu FlashScore ligi)
        # tek makul seçenek bu (bkz. fit()).
        lam_h_ht = lam_h * self.first_half_ratio
        lam_a_ht = lam_a * self.first_half_ratio
        home_probs_ht = poisson.pmf(goals_idx, lam_h_ht)
        away_probs_ht = poisson.pmf(goals_idx, lam_a_ht)
        matrix_ht = np.outer(home_probs_ht, away_probs_ht)
        matrix_ht /= matrix_ht.sum()
        ilk_yari = {
            "ev": round(float(np.tril(matrix_ht, -1).sum()), 3),
            "beraberlik": round(float(np.trace(matrix_ht)), 3),
            "deplasman": round(float(np.triu(matrix_ht, 1).sum()), 3),
        }

        return {
            "ev": home,
            "deplasman": away,
            "beklenen_gol_ev": round(lam_h, 2),
            "beklenen_gol_deplasman": round(lam_a, 2),
            "1x2": {
                "ev_kazanir": round(home_win, 3),
                "beraberlik": round(draw, 3),
                "deplasman_kazanir": round(away_win, 3),
            },
            "2.5_gol": {
                "ust": round(over_2_5, 3),
                "alt": round(under_2_5, 3),
            },
            "kg": {
                "var": round(btts_yes, 3),
                "yok": round(btts_no, 3),
            },
            "1.5_gol": {
                "ust": round(over_1_5, 3),
                "alt": round(under_1_5, 3),
            },
            "en_olasi_skorlar": top_scores,
            "cifte_sans": cifte_sans,
            "ilk_yari": ilk_yari,
        }
