---
name: docs-sync
description: Synchroniser SKILL.md, README.md, docs/ et exemples de gatecost avec le code, et passer les tests de cohérence documentaire (test_docs_sync, grammaire de sortie agent). À utiliser après tout changement de CLI, de format de sortie, de modèle de coût ou de couverture de langage.
---

# docs-sync

## Quoi vérifier selon le changement

| Changement | Documents |
|---|---|
| Option/commande CLI | `docs/CLI.md`, `SKILL.md` (table « Which command »), `README.md`, aide `gatecost help` |
| Format de sortie, verdict, code d'erreur | `docs/OUTPUT.md`, `schema/gatecost-result.schema.json`, `tests/agent_grammar.py`, `SKILL.md` |
| Formule de coût | `docs/COST_MODEL.md`, exemples chiffrés (`README.md`, `examples/mac/`) |
| Construction HDL supportée | table de couverture de `docs/FRONTEND.md` |
| Règle IR / passe | `docs/IR.md` |
| Différ / alignement | `docs/DIFF.md` |
| Étape terminée | table de statut de `docs/IMPLEMENTATION.md` |
| Conventions / process | `CONTRIBUTING.md` |

## Contraintes de `SKILL.md` (le skill produit, à la racine)

- Frontmatter = exactement `name` et `description` ; description bornée et au vocabulaire contrôlé.
- Moins de 120 lignes ; aucun chemin de montage en dur ; ne référence que des fichiers de `docs/` existants.
- Chaque commande citée doit être acceptée par le parseur ; chaque exemple `estimate` doit s'exécuter.

## Vérifier

```bash
PYTHONPATH=src python3 -m unittest tests.test_docs_sync tests.test_agent_grammar
python3 scripts/gatecost help
```

## Principe

Les documents sont la spécification. Si code et doc divergent, décider lequel a tort, et le dire explicitement dans le compte rendu : ne jamais aligner la doc sur un bug sans le signaler.

Ne pas confondre `SKILL.md` (skill produit pour les agents utilisateurs de l'outil) et `.claude/skills/` (skills de développement de ce dépôt).

Agent recommandé : `docs-keeper`.
