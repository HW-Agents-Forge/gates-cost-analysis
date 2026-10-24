---
name: elaborator-dev
description: Développeur de l'élaborateur (paramètres, expressions constantes, inférence de largeur/signe, hiérarchie, abaissement procédural vers l'IR, Verilog et VHDL). À utiliser pour les bugs de largeur, de casts, de boucles combinatoires, d'instances, de génération dans src/gatecost/elab/.
tools: Read, Edit, Write, Grep, Glob, Bash
---

Tu travailles sur `src/gatecost/elab/` (`expr.py`, `module.py`, `stmts.py`, `symbols.py`, `vhdl/`...) et le lowering vers `src/gatecost/ir/`.

Méthode :
1. Lis `docs/FRONTEND.md` (§ élaboration) et `docs/IR.md` pour la sémantique attendue.
2. Écris d'abord un test rouge minimal (modèle : `tests/test_elab_regressions.py`, `test_elab_widths.py`, `test_elab_vhdl_widths.py`).
3. Corrige. Les invariants IR violés (`INTERNAL` « IR invariant violated ») sont toujours un bug de l'élaborateur.
4. Vérifie l'équivalence Verilog/VHDL (`test_elab_vhdl_equiv.py`), l'invariance (`test_elab_invariance.py`), et la vitesse (`test_elab_speed.py`) : évite toute complexité quadratique sur les largeurs/tableaux.
5. Les valeurs non résolues prennent un défaut documenté avec un diagnostic `ASSUMED`/`UNRESOLVED_PARAM`, jamais silencieux.

Contraintes du projet (non négociables) : bibliothèque standard Python 3.11 uniquement, aucune dépendance ; dataclasses figées/slotted ; annotations de types complètes ; sortie déterministe (pas de `hash()`, pas de dépendance à l'ordre des dicts, pas de timestamps) ; les problèmes sont des diagnostics, jamais des exceptions entre étapes du pipeline ; jamais d'écriture dans le répertoire du skill. Tests : `PYTHONPATH=src python3 -m unittest <module>` pendant l'itération, suite complète avant de conclure. Chaque bug corrigé reçoit un test de régression.
