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
import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import poisson


def _dixon_coles_tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles düzeltme fonksiyonu (tau). Bağımsız Poisson modelinin
    düşük skorlarda (özellikle beraberliklerde) yaptığı sistematik sapmayı
    düzeltir. rho=0 -> tau=1 (tüm skorlarda), yani düzeltme tamamen etkisiz
    olur ve matris eski (düzeltmesiz) haliyle aynı kalır."""
    if x == 0 and y == 0:
        return 1 - lam * mu * rho
    if x == 0 and y == 1:
        return 1 + lam * rho
    if x == 1 and y == 0:
        return 1 + mu * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


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
    # Maçın tarihi (ISO string, örn. "2026-09-01T18:00:00+00:00") -- SADECE
    # zaman ağırlıklandırma (bkz. fit()) için kullanılır. None ise o maç
    # normal (ağırlıksız) kabul edilir, model davranışı önceki gibi kalır.
    date: Optional[str] = None


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
        # Dixon-Coles düzeltmesi: bağımsız Poisson modeli düşük skorlu
        # beraberlikleri (özellikle 1-1) hafife alma eğiliminde -- rho bunu
        # düzeltir (bkz. score_matrix). 0.0 = düzeltme yok (eski davranış).
        # fit() içinde geçmiş maçlardan tahmin edilir; tahmin başarısız olursa
        # ya da yeterli veri yoksa 0.0'da kalır.
        self.rho = 0.0
        # Zaman ağırlıklandırma: eski maçlar katlanarak azalan ağırlık alır
        # (exp(-xi * gün)), güncel form geçmişten daha belirleyici olsun diye.
        # Literatürde (Dixon-Coles zaman-ağırlıklı varyantları) yaygın kullanılan
        # değer araliği ~0.0018-0.0033/gün; burada 0.0018 (daha yumuşak azalma)
        # kullanılıyor. Maçlarda tarih yoksa (date=None) ağırlık 1.0 kalır,
        # yani davranış öncekiyle aynı olur.
        self.time_decay_xi = 0.0018
        # Elo harmanlama ağırlığı (bkz. predict()): elo=None verilirse (çoğu
        # çağrı) bu hiç kullanılmaz, davranış öncekiyle tamamen aynı kalır.
        self.elo_weight = 0.15

    def _match_weights(self, matches: List["MatchResult"]) -> List[float]:
        """Her maça exp(-xi * yaş_gün) ağırlığı verir; referans nokta en güncel
        maçın tarihidir (gerçek "bugün" fit() içine ayrıca verilmiyor, en
        yakın maç ona en yakın yaklaşıklamadır). Tarihsiz maç varsa (date=None)
        o maça 1.0 (tam ağırlık) verilir -- bu, tarih bilgisi hiç yoksa TÜM
        maçların 1.0 alıp eski (ağırlıksız) davranışa dönmesini sağlar.
        """
        parsed = []
        for m in matches:
            if not m.date:
                parsed.append(None)
                continue
            try:
                parsed.append(dt.datetime.fromisoformat(m.date.replace("Z", "+00:00")))
            except (ValueError, TypeError):
                parsed.append(None)

        valid_dates = [d for d in parsed if d is not None]
        if not valid_dates:
            return [1.0] * len(matches)

        ref = max(valid_dates)
        weights = []
        for d in parsed:
            if d is None:
                weights.append(1.0)
                continue
            age_days = max((ref - d).total_seconds() / 86400.0, 0.0)
            weights.append(float(np.exp(-self.time_decay_xi * age_days)))
        return weights

    def fit(self, matches: List[MatchResult]) -> None:
        if not matches:
            raise ValueError("Model eğitmek için en az bir maç sonucu gerekli.")

        teams = set()
        for m in matches:
            teams.add(m.home)
            teams.add(m.away)

        weights = self._match_weights(matches)
        w_sum = sum(weights)
        total_home_goals_w = sum(m.home_goals * w for m, w in zip(matches, weights))
        total_away_goals_w = sum(m.away_goals * w for m, w in zip(matches, weights))
        self.league_avg_home_goals = total_home_goals_w / w_sum
        self.league_avg_away_goals = total_away_goals_w / w_sum

        goals_for: Dict[str, List[float]] = {t: [] for t in teams}
        goals_against: Dict[str, List[float]] = {t: [] for t in teams}
        goals_for_w: Dict[str, List[float]] = {t: [] for t in teams}
        goals_against_w: Dict[str, List[float]] = {t: [] for t in teams}

        for m, w in zip(matches, weights):
            # ev sahibinin attığı gol, lig ev ortalamasına göre normalize
            goals_for[m.home].append(m.home_goals / self.league_avg_home_goals)
            goals_against[m.home].append(m.away_goals / self.league_avg_away_goals)
            goals_for_w[m.home].append(w)
            goals_against_w[m.home].append(w)
            # deplasman takımının attığı gol, lig deplasman ortalamasına göre
            goals_for[m.away].append(m.away_goals / self.league_avg_away_goals)
            goals_against[m.away].append(m.home_goals / self.league_avg_home_goals)
            goals_for_w[m.away].append(w)
            goals_against_w[m.away].append(w)

        for t in teams:
            gf, gf_w = goals_for[t], goals_for_w[t]
            ga, ga_w = goals_against[t], goals_against_w[t]
            attack = float(np.average(gf, weights=gf_w)) if gf else 1.0
            defense = float(np.average(ga, weights=ga_w)) if ga else 1.0
            self.strengths[t] = TeamStrength(attack=attack, defense=defense)

        # --- Dixon-Coles rho düzeltmesi: bağımsız Poisson'un düşük skorlu
        # beraberlikleri (özellikle 1-1) hafife alma eğilimini düzeltmek için,
        # zaten hesaplanmış attack/defense'i sabit tutup rho'yu 1-boyutlu
        # maksimum olabilirlik ile tahmin ediyoruz. Sadece 0-0/1-0/0-1/1-1
        # skorlarını etkiler (bkz. score_matrix) -- diğer skorlarda etkisiz. ---
        try:
            import scipy.optimize as _opt

            low_score_matches = []
            for m in matches:
                if (m.home_goals, m.away_goals) in ((0, 0), (1, 0), (0, 1), (1, 1)):
                    lam_h, lam_a = self.expected_goals(m.home, m.away)
                    low_score_matches.append((m.home_goals, m.away_goals, lam_h, lam_a))

            if len(low_score_matches) >= 20:
                def neg_log_lik(rho):
                    total = 0.0
                    for x, y, lam, mu in low_score_matches:
                        tau = _dixon_coles_tau(x, y, lam, mu, rho)
                        if tau <= 1e-6:
                            return 1e6
                        total += np.log(tau)
                    return -total

                res = _opt.minimize_scalar(neg_log_lik, bounds=(-0.25, 0.25), method="bounded")
                if res.success:
                    self.rho = float(np.clip(res.x, -0.25, 0.25))
            # yetersiz veri varsa __init__'teki varsayılan (0.0, düzeltme yok) kalır
        except Exception:
            # rho tahmini başarısız olursa modeli bozmak yerine düzeltmesiz (0.0) bırak
            self.rho = 0.0

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

        # Dixon-Coles düzeltmesi (rho=0 ise tau=1, matris değişmez -- eski davranış)
        if self.rho != 0.0:
            for x, y in ((0, 0), (0, 1), (1, 0), (1, 1)):
                tau = _dixon_coles_tau(x, y, lam_h, lam_a, self.rho)
                matrix[x, y] *= max(tau, 0.0)

        matrix /= matrix.sum()  # kesilen kuyruğu + tau düzeltmesini normalize et
        return matrix

    def predict(
        self, home: str, away: str,
        home_attack_mult: float = 1.0, away_attack_mult: float = 1.0,
        elo: Optional[object] = None, elo_weight: Optional[float] = None,
    ) -> Dict:
        """home_attack_mult / away_attack_mult: kadroda kilit bir oyuncu eksikse
        (bkz. export_excel.enrich_upcoming_matches) o tarafın hücum gücünü
        hafifçe düşürmek için kullanılır (1.0 = etkisiz).

        elo: verilirse (bir EloRatings örneği, bkz. models/elo.py) 1X2
        olasılığı Poisson'un yanına Elo'nun win_draw_loss_prob() çıktısıyla
        hafifçe harmanlanır (ağırlık elo_weight, verilmezse self.elo_weight).
        elo=None (varsayılan) ise HİÇBİR ŞEY değişmez -- önceki davranış
        (sadece Poisson) tamamen korunur. Sadece 1X2 (ve ondan türeyen çifte
        şans) etkilenir; skor matrisine dayanan diğer tüm pazarlar (2.5/1.5
        gol, KG, en olası skorlar, ilk yarı) saf Poisson'dan gelmeye devam
        eder -- Elo zaten gol sayısını değil sadece galibiyet/mağlubiyet/
        beraberlik eğilimini modelliyor."""
        matrix = self.score_matrix(home, away, home_attack_mult, away_attack_mult)
        lam_h, lam_a = self.expected_goals(home, away, home_attack_mult, away_attack_mult)

        home_win = float(np.tril(matrix, -1).sum())
        draw = float(np.trace(matrix))
        away_win = float(np.triu(matrix, 1).sum())

        if elo is not None:
            w = self.elo_weight if elo_weight is None else elo_weight
            w = float(np.clip(w, 0.0, 1.0))
            try:
                elo_home, elo_draw, elo_away = elo.win_draw_loss_prob(home, away)
                home_win = (1 - w) * home_win + w * elo_home
                draw = (1 - w) * draw + w * elo_draw
                away_win = (1 - w) * away_win + w * elo_away
                total = home_win + draw + away_win
                if total > 0:
                    home_win, draw, away_win = home_win / total, draw / total, away_win / total
            except Exception:
                pass  # Elo harmanlama başarısız olursa sessizce atla, saf Poisson sonucu kalır

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
