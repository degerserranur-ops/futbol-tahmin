"""
Maç tahminleme sistemi - komut satırı arayüzü.

Kullanım örnekleri:

  # Bir ligdeki geçmiş sonuçlarla modeli eğit, iki takım arasındaki
  # bir maçı tahmin et:
  python predict.py --league super_lig --home "Galatasaray" --away "Fenerbahce"

  # Bir ligdeki önümüzdeki N maçın tahminlerini listele:
  python predict.py --league super_lig --upcoming 5

Not: Gerçek veriyle çalışmak için .env dosyasına API_FOOTBALL_KEY eklemen
gerekir (bkz. config.py). Anahtar yoksa test/demo verisiyle
demo_data.py üzerinden deneme yapabilirsin.
"""
import argparse
import sys
from typing import List

import config
from data.api_client import ApiFootballClient, ApiFootballError
from models.elo import EloRatings
from models.poisson_model import MatchResult, PoissonGoalModel


def fixtures_to_results(fixtures: list) -> List[MatchResult]:
    results = []
    for fx in fixtures:
        status = fx.get("fixture", {}).get("status", {}).get("short")
        if status != "FT":  # sadece tamamlanmış maçlar modele girer
            continue
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        hg = fx["goals"]["home"]
        ag = fx["goals"]["away"]
        if hg is None or ag is None:
            continue
        results.append(MatchResult(home=home, away=away, home_goals=hg, away_goals=ag))
    return results


def build_models(matches: List[MatchResult]):
    poisson_model = PoissonGoalModel()
    poisson_model.fit(matches)

    elo = EloRatings()
    for m in matches:
        elo.update_match(m.home, m.away, m.home_goals, m.away_goals)

    return poisson_model, elo


def print_prediction(poisson_model: PoissonGoalModel, elo: EloRatings, home: str, away: str) -> None:
    pred = poisson_model.predict(home, away)
    elo_h, elo_d, elo_a = elo.win_draw_loss_prob(home, away)

    print(f"\n=== {pred['ev']} vs {pred['deplasman']} ===")
    print(f"Beklenen gol  -> Ev: {pred['beklenen_gol_ev']}  |  Deplasman: {pred['beklenen_gol_deplasman']}")
    print(f"Elo puanları  -> {home}: {elo.get_rating(home):.0f}   {away}: {elo.get_rating(away):.0f}")
    print("\n1X2 olasılıkları (Poisson modeli):")
    print(f"  Ev kazanır     : %{pred['1x2']['ev_kazanir']*100:.1f}")
    print(f"  Beraberlik     : %{pred['1x2']['beraberlik']*100:.1f}")
    print(f"  Deplasman kazanır: %{pred['1x2']['deplasman_kazanir']*100:.1f}")
    print("\n1X2 olasılıkları (Elo modeli, referans amaçlı):")
    print(f"  Ev kazanır     : %{elo_h*100:.1f}")
    print(f"  Beraberlik     : %{elo_d*100:.1f}")
    print(f"  Deplasman kazanır: %{elo_a*100:.1f}")
    print(f"\n2.5 Üst/Alt     -> Üst: %{pred['2.5_gol']['ust']*100:.1f}   Alt: %{pred['2.5_gol']['alt']*100:.1f}")
    print(f"KG Var/Yok      -> Var: %{pred['kg']['var']*100:.1f}   Yok: %{pred['kg']['yok']*100:.1f}")
    print("\nEn olası skorlar:")
    for s in pred["en_olasi_skorlar"]:
        print(f"  {s['skor']}  (%{s['olasilik']*100:.1f})")
    print()


def main():
    parser = argparse.ArgumentParser(description="Futbol maç tahminleme sistemi")
    parser.add_argument("--league", default="super_lig", choices=list(config.LEAGUES.keys()))
    parser.add_argument("--season", type=int, default=config.CURRENT_SEASON)
    parser.add_argument("--home", type=str, help="Ev sahibi takım adı")
    parser.add_argument("--away", type=str, help="Deplasman takım adı")
    parser.add_argument("--upcoming", type=int, help="Önümüzdeki N maçı tahmin et")
    args = parser.parse_args()

    league_id = config.LEAGUES[args.league]
    client = ApiFootballClient()

    try:
        fixtures = client.get_fixtures(league_id, args.season)
    except ApiFootballError as e:
        print(f"HATA: {e}")
        print("İpucu: Gerçek API anahtarı olmadan demo_data.py ile örnek veriyle test edebilirsin:")
        print("       python demo_data.py")
        sys.exit(1)

    matches = fixtures_to_results(fixtures)
    if len(matches) < 10:
        print(f"UYARI: Modeli eğitmek için yeterli tamamlanmış maç bulunamadı ({len(matches)} maç).")
        sys.exit(1)

    poisson_model, elo = build_models(matches)

    if args.home and args.away:
        print_prediction(poisson_model, elo, args.home, args.away)
    elif args.upcoming:
        upcoming = client.get_upcoming_fixtures(league_id, args.season, next_n=args.upcoming)
        if not upcoming:
            print("Önümüzdeki maç bulunamadı.")
            return
        for fx in upcoming:
            home = fx["teams"]["home"]["name"]
            away = fx["teams"]["away"]["name"]
            print_prediction(poisson_model, elo, home, away)
    else:
        print("Lütfen --home/--away veya --upcoming belirt. (python predict.py -h)")


if __name__ == "__main__":
    main()
