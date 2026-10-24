---
name: done-checklist
description: Liste de contrôle « définition de done » de gatecost à passer avant de conclure un changement ou d'ouvrir une PR (spec, tests, deux interpréteurs, benchmark, docs, pas de dépendance, pas d'écriture hors zones autorisées). À utiliser en fin de tâche.
---

# done-checklist

Source : `CONTRIBUTING.md` § Definition of done et `docs/IMPLEMENTATION.md` §5. Dérouler chaque point et répondre honnêtement ; un point non vérifié est signalé comme tel.

1. **Spec** : le comportement suit les sections de `docs/` citées, ou la spec est mise à jour dans le même changement avec la raison.
2. **Tests** : tests des sections concernées + un test de régression pour chaque défaut corrigé.
3. **Suite complète** : `PYTHONPATH=src python3 -m unittest discover -s tests -t .` verte. Python 3.11 et dernière version si disponibles (CI : Linux + macOS × 3.11 / 3.x). Benchmark de démarrage dans son budget (`GATECOST_BENCH=1 … tests.test_bench`) si le chemin de démarrage ou des boucles chaudes ont changé.
4. **Docs** : `SKILL.md`, `README.md`, `docs/` conformes à l'outil ; `tests.test_docs_sync` passe.
5. **Hygiène** :
   - aucune nouvelle dépendance, stdlib seulement, syntaxe ≤ 3.11 ;
   - aucune écriture dans le répertoire du skill (seulement `<projet>/.gatecost/` ou le cache utilisateur) ;
   - sortie déterministe (pas de `hash()`, d'ordre de dict, de timestamp) ;
   - erreurs en diagnostics, pas d'exception entre étapes ;
   - golden files non régénérés automatiquement.
6. **Statut** : table de `docs/IMPLEMENTATION.md` mise à jour si une étape a changé d'état.
7. **Revue de diff** : relire `git diff` en cherchant ce que la CI rejetterait (types, lint stdlib, fichiers temporaires, `__pycache__`).

Optionnel : `mypy --strict src`, smoke sur corpus RTL.

Compte rendu final : ce qui est fait et vérifié, ce qui ne l'est pas, avec la sortie des tests en cas d'échec.
