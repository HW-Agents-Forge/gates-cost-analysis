---
name: fix-hdl-bug
description: Workflow pour corriger un bug de gatecost révélé par du RTL réel (crash INTERNAL, mauvaise largeur, construction non supportée, estimation aberrante, lenteur) : réduire, test rouge, correction ciblée, régression, suite complète. À utiliser pour tout rapport de bug sur une entrée Verilog/SystemVerilog/VHDL.
---

# fix-hdl-bug

## 1. Reproduire

```bash
python3 scripts/gatecost cost --no-cache --format json   # ou diff, depuis le répertoire du RTL fautif
```

Repérer le symptôme : `ERROR INTERNAL` / traceback (toujours un bug), `UNSUPPORTED_CONSTRUCT` fréquent, résultat absurde, délai anormal.

## 2. Réduire

Réduire le RTL au plus petit fragment qui déclenche le symptôme (supprimer modules, signaux, branches tant que le symptôme persiste). Un fragment inline convient souvent : `estimate --rtl "assign y = a*b + c;" --widths a=16,b=16,c=32`.

## 3. Localiser la couche responsable

| Symptôme | Couche | Agent |
|---|---|---|
| Erreur de syntaxe, token, préprocesseur | `frontend/` | `frontend-dev` |
| Largeur/signe, paramètres, hiérarchie, boucle combinatoire fausse, invariant IR violé | `elab/` | `elaborator-dev` |
| Réduction fausse, known-bits, partage | `ir/` | `ir-analysis-dev` |
| Chiffre absurde pour une opération correcte | `cost/` | `cost-model-dev` |
| Delta ou verdict faux, format | `diff/`, `report/` | `diff-report-dev` |
| Temps de calcul | tout le pipeline | `perf-optimizer` |

## 4. Test rouge d'abord

Ajouter le fragment réduit comme test dans le module du sous-système (modèle : `tests/test_elab_regressions.py` : helper `run(text)` + assertion sur les codes de diagnostic ou le chiffre). Le lancer et constater qu'il échoue pour la bonne raison.

## 5. Corriger

- Cause racine, pas de rustine sur le symptôme.
- Pas de levée d'exception à travers les étapes : produire un diagnostic.
- Sortie déterministe ; pas de dépendance ajoutée.

## 6. Vérifier

1. Le test de régression passe.
2. Le module du sous-système passe, puis la suite complète (skill `run-tests`).
3. Si la correction change un comportement documenté, mettre à jour `docs/` dans le même changement (skill `docs-sync`).
4. Compte rendu : cause racine, fichier:ligne, test ajouté, ce qui n'a pas pu être vérifié.
