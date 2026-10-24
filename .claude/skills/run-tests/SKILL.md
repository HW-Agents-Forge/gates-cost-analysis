---
name: run-tests
description: Lance les tests de gatecost (unittest, stdlib) au bon niveau : un module pendant l'itération, la suite complète avant de conclure, plus benchmarks, smoke et mypy en option. À utiliser dès qu'il faut vérifier un changement de code.
---

# run-tests

Tout se lance depuis la racine du dépôt avec `PYTHONPATH=src`. Il n'y a rien à installer.

## Choisir le niveau

| Besoin | Commande |
|---|---|
| Un module (itération) | `PYTHONPATH=src python3 -m unittest tests.test_cost_formulas` |
| Un seul test | `PYTHONPATH=src python3 -m unittest tests.test_cli -k launcher` |
| Suite complète (avant de conclure) | `PYTHONPATH=src python3 -m unittest discover -s tests -t .` |
| Docs / SKILL.md / lint stdlib | `PYTHONPATH=src python3 -m unittest tests.test_docs_sync tests.test_agent_grammar` |
| Budgets de performance | `GATECOST_BENCH=1 PYTHONPATH=src python3 -m unittest tests.test_bench` |
| Type check (optionnel) | `mypy --strict src` |
| RTL réel (optionnel) | `python3 tests/smoke/run_smoke.py CORPUS_DIR --json` |

## Quel module pour quel sous-système

- front-end : `test_verilog_fuzz`, `test_udp`, `test_elab_vhdl_*`, corpus `tests/corpus/`
- élaboration : `test_elab_*` (`regressions`, `widths`, `hierarchy`, `params`, `speed`, `invariance`)
- IR : `test_ir_*`
- coût : `test_cost_*`
- diff / rapports / CLI : `test_diff_*`, `test_report`, `test_cli`, `test_e2e_*`, `test_snapshot`, `test_baseline`
- docs : `test_docs_sync`, `test_agent_grammar`

## Règles

- Le projet doit passer sur Python 3.11 (plus ancien supporté) et sur la dernière version ; si un seul interpréteur est disponible, dis-le dans le compte rendu.
- Certains modules (fuzz, speed, campaign) sont lents : ne les relance en entier qu'une fois en fin de travail.
- Si un test échoue, rapporte-le avec sa sortie ; n'ignore, ne désactive ni ne saute jamais un test pour obtenir du vert.
- Les golden files ne se régénèrent que par un script explicite, jamais automatiquement.
