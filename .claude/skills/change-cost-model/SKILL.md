---
name: change-cost-model
description: Modifier ou ajouter une formule de coût, un primitif ou un profil dans gatecost (GE, scénarios min/typ/max, milli-GE, confiance) en gardant COST_MODEL.md, les tests de propriétés et les exemples cohérents. À utiliser pour tout changement de chiffres produits par l'outil.
---

# change-cost-model

Le coût est défini par `docs/COST_MODEL.md` ; le code l'implémente. Toute modification touche les deux.

## Fichiers

- Formules : `src/gatecost/cost/{arith,logic,seq,sharing}.py`
- Unités et arrondis : `cost/units.py` (entiers en milli-GE)
- Arbre, delta, provenance : `cost/{tree,delta,provenance}.py`
- Profils : `src/gatecost/profiles/*.toml` (`generic-asic`, héritage via `extends`)
- Notation `estimate` : `estimate.py`, `rtl_estimate.py`

## Procédure

1. **Spécifier** : écrire/modifier la formule dans `docs/COST_MODEL.md` §3 (les trois scénarios min/typ/max) avec la justification (architecture supposée : additionneur ripple vs carry-lookahead, multiplicateur Booth, etc.).
2. **Implémenter** en entiers milli-GE, sans flottant dans les résultats, avec min ≤ typ ≤ max.
3. **Tests** :
   - valeurs exactes pour quelques largeurs (`tests/test_cost_formulas.py`) ;
   - propriétés : monotonie en la largeur, constantes moins chères que variables, bornes (`test_cost_properties.py`) ;
   - profil (`test_cost_profile.py`), arbre (`test_cost_tree.py`).
4. **Qualité/confiance** : une opération mal modélisée (diviseurs, mémoires, ROM) garde une classe de qualité basse ; ne pas annoncer une confiance supérieure à la réalité.
5. **Propager** : les chiffres cités dans `SKILL.md`, `README.md`, `docs/CLI.md`, `examples/mac/REPORT.md` doivent rester vrais. Relancer `tests.test_docs_sync` et les tests e2e, puis régénérer l'exemple s'il existe un script explicite pour cela.
6. **Ne pas** présenter le profil comme calibré : il ne l'est pas (`scripts/validate_reference.py` compare avec des surfaces de référence, usage mainteneur).

## Vérifier

Agent recommandé : `cost-model-dev`. Puis skill `run-tests` (suite complète) et `done-checklist`.
