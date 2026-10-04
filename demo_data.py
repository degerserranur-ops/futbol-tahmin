"""
API anahtarı olmadan sistemi test etmek için örnek (sentetik) veriyle demo.

Gerçek sonuçlar değildir - sadece Elo + Poisson modellerinin doğru
çalıştığını göstermek için rastgele ama gerçekçi bir lig simülasyonu
üretir (6 takım, round-robin, güç farkı olacak şekilde ağırlıklandırılmış).
"""
import random

from models.elo import EloRatings
from models.poisson_model import MatchResult, PoissonGoalModel

random.seed(42)

TEAMS = {
    "Takim A (guclu)": 1.6,
    "Takim B (guclu)": 1.4,
    "Takim C (orta)": 1.0,
    "Takim D (orta)": 0.95,
    "Takim E (zayif)": 0.6,
    "Takim F (zayif)": 0.5,
}


def simulate_season(rounds: int = 3):
    matches = []
    names = list(TEAMS.keys())
    for _ in range(rounds):
        for i, home in enumerate(names):
            for j, away in enumerate(names):
                if home == away:
                    continue
                home_strength = TEAMS[home]
                away_strength = TEAMS[away]
                lam_home = max(0.3, 1.35 * home_strength / away_strength)
                lam_away = max(0.2, 1.1 * away_strength / home_strength)
                hg = min(8, int(random.gammavariate(lam_home, 1)))
                ag = min(8, int(random.gammavariate(lam_away, 1)))
                matches.append(MatchResult(home=home, away=away, home_goals=hg, away_goals=ag))
    return matches


def main():
    matches = simulate_season(rounds=4)
    print(f"Simüle edilen toplam maç: {len(matches)}")

    poisson_model = PoissonGoalModel()
    poisson_model.fit(matches)

    elo = EloRatings()
    for m in matches:
        elo.update_match(m.home, m.away, m.home_goals, m.away_goals)

    print("\nElo sıralaması (simülasyon sonrası):")
    for team, rating in elo.ranked_table():
        print(f"  {team:20s} {rating:.0f}")

    print("\n--- Örnek tahmin: güçlü vs zayıf ---")
    pred = poisson_model.predict("Takim A (guclu)", "Takim F (zayif)")
    _print(pred)

    print("\n--- Örnek tahmin: dengeli maç ---")
    pred2 = poisson_model.predict("Takim C (orta)", "Takim D (orta)")
    _print(pred2)


def _print(pred):
    print(f"Beklenen gol -> Ev: {pred['beklenen_gol_ev']}  Deplasman: {pred['beklenen_gol_deplasman']}")
    print(f"1X2 -> Ev: %{pred['1x2']['ev_kazanir']*100:.1f}  "
          f"Beraberlik: %{pred['1x2']['beraberlik']*100:.1f}  "
          f"Deplasman: %{pred['1x2']['deplasman_kazanir']*100:.1f}")
    print(f"2.5 Üst/Alt -> Üst: %{pred['2.5_gol']['ust']*100:.1f}  Alt: %{pred['2.5_gol']['alt']*100:.1f}")
    print(f"KG Var/Yok -> Var: %{pred['kg']['var']*100:.1f}  Yok: %{pred['kg']['yok']*100:.1f}")
    print("En olası skorlar:", ", ".join(f"{s['skor']} (%{s['olasilik']*100:.1f})" for s in pred["en_olasi_skorlar"]))


if __name__ == "__main__":
    main()
