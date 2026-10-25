---
name: perf-check
description: Mesurer et diagnostiquer les performances de gatecost (démarrage, grands designs, complexité quadratique) avec les benchmarks du dépôt et cProfile. À utiliser quand une analyse est lente ou avant/après un changement de boucle sur les largeurs, tableaux ou netlists.
---

# perf-check

Budgets (`docs/DISTRIBUTION.md` §3, `docs/SPEC.md` §12) : démarrage ≤ 300 ms à froid, ≤ 100 ms à chaud ; analyse linéaire en la taille du design.

## Mesurer

```bash
GATECOST_BENCH=1 PYTHONPATH=src python3 -m unittest tests.test_bench   # applique les budgets
PYTHONPATH=src python3 -m tests.bench.run_bench                        # tableau complet
PYTHONPATH=src python3 -X importtime scripts/gatecost --version 2>&1 | sort -t'|' -k2 -n | tail   # démarrage
```

Générateurs de designs synthétiques : `tests/bench/gen_design.py`, `tests/bench/ir_gen.py`.

## Profiler

```bash
PYTHONPATH=src python3 -c "import cProfile,pstats,sys; sys.argv=['gatecost','cost','--no-cache']; \
from gatecost.cli import main; cProfile.run('main(sys.argv[1:])','/tmp/p.out')"
python3 -c "import pstats; pstats.Stats('/tmp/p.out').sort_stats('cumtime').print_stats(25)"
```

(Écrire la sortie de profil dans le scratchpad de la session plutôt que dans le dépôt.)

## Interpréter

- Le temps croît-il plus vite que linéairement quand on double la taille ? Chercher des boucles imbriquées sur des bits d'un net ou des éléments d'un tableau, des concaténations de listes répétées, des recherches linéaires dans des listes.
- Démarrage lent : un import coûteux hors du chemin critique ; utiliser les imports paresseux (`_lazy.py`).
- Précédents : `LowerBlock.finish` rendu linéaire en largeur de net (44 s → 2,5 s), écriture dynamique de vecteur packed quadratique.

## Règles

- Mesurer avant et après ; rapporter les chiffres.
- La sortie doit rester identique octet pour octet (déterminisme) : suite complète + `test_perf_structure`, `test_elab_speed`.
- Pas de cache qui modifie les résultats, pas de dépendance.

Agent recommandé : `perf-optimizer`.
