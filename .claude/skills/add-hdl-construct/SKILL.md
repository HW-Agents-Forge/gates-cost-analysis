---
name: add-hdl-construct
description: Ajoute le support d'une construction Verilog/SystemVerilog/VHDL à gatecost, du lexer/parser jusqu'au coût : tokens, AST, élaboration, IR, tests Verilog/VHDL, table de couverture. À utiliser quand une construction produit UNSUPPORTED_CONSTRUCT ou est ignorée à tort.
---

# add-hdl-construct

Une construction traverse le pipeline `frontend → elab → ir → cost`. Procéder dans cet ordre et ne passer à l'étape suivante que lorsque la précédente est testée.

## 0. Cadrer

- Lire la ligne correspondante de la table de couverture dans `docs/FRONTEND.md` (état actuel, sémantique attendue).
- Décider du modèle matériel : que synthétise cette construction (logique combinatoire, registre, mémoire, rien) ? Si ce n'est pas synthétisable (testbench, assertions), elle doit être ignorée silencieusement, sans diagnostic bruyant.

## 1. Front-end (`src/gatecost/frontend/`)

- Lexer/préprocesseur si nouveau token ou directive.
- AST (`verilog/ast.py`, `vhdl/nodes.py`) : dataclasses figées/slotted.
- Parser avec récupération d'erreurs : un fragment mal formé ne doit pas empêcher d'analyser le code voisin.

## 2. Élaboration (`src/gatecost/elab/`)

- Règles de largeur et de signe de la construction, évaluation constante si besoin.
- Abaissement vers l'IR en utilisant les opérations existantes du catalogue (`docs/IR.md` §2–3) ; n'ajouter une opération IR qu'en dernier recours, avec son entrée dans `IR.md`, son coût dans `COST_MODEL.md`, et ses cas dans `tests/ir_sim.py`.
- Valeur non résolue : défaut documenté + diagnostic (`ASSUMED` / `UNRESOLVED_PARAM`).

## 3. Tests

- Parser/élaborateur pour le langage concerné (`tests/test_elab_*.py`, `tests/test_elab_vhdl_*.py`).
- Si la construction existe dans les deux langages, un test d'équivalence (`test_elab_vhdl_equiv.py`) : mêmes coûts pour le même matériel.
- Invariance : reformater ou renommer ne change pas le résultat.
- Fuzz/mutation du module concerné ; vitesse sur une grande instance (pas de quadratique).

## 4. Documentation

Passer la ligne de `docs/FRONTEND.md` à « supporté », mettre à jour `docs/IMPLEMENTATION.md` si une étape change d'état (skill `docs-sync`).

## 5. Clore

Suite complète (skill `run-tests`) puis `done-checklist`.
