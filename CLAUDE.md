# gatecost: développement avec Claude Code

Conventions et définition de « done » : `CONTRIBUTING.md`. Spécifications : `docs/`. Skills de développement : `.claude/skills/` (le `SKILL.md` racine est le skill produit pour les utilisateurs de l'outil).

## Déléguer aux sous-agents (`.claude/agents/`)

L'agent principal orchestre : il délègue dès que la tâche correspond, lance en parallèle les tâches indépendantes, et relit les résultats (fichiers:lignes clés, sortie réelle des tests en cas d'échec).

| Besoin | Agent |
|---|---|
| Cartographier code/tests/docs avant de modifier du code inconnu | `repo-scout` (haiku) |
| Lexer, préprocesseur, parsers Verilog/SV/VHDL | `frontend-dev` (opus) |
| Largeurs, paramètres, hiérarchie, lowering vers l'IR | `elaborator-dev` (opus) |
| Builder IR, passes, clés canoniques | `ir-analysis-dev` (opus) |
| Formules de coût, profils, `estimate` | `cost-model-dev` (opus) |
| Différ, rapports, schéma JSON, CLI | `diff-report-dev` (sonnet) |
| Lenteur, complexité, démarrage | `perf-optimizer` (opus), mesures par `bench-runner` (haiku) |
| Test rouge, fuzz, chasse aux bugs sur RTL réel | `test-engineer` (sonnet) |
| Docs / `SKILL.md` / exemples à resynchroniser | `docs-keeper` (sonnet) |
| Lancer tests, mypy, smoke (seul le verdict compte) | `test-runner` (haiku) |
| Branche, commits atomiques, push | `git-committer` (haiku) |

Flux type : `repo-scout` → test rouge (`test-engineer`) → correctif (agent du sous-système) → `test-runner` → `docs-keeper` si un comportement change → `git-committer`. Ordre des couches : front-end → élaboration → IR → coût → diff/rapport → docs.

Briefs : un sous-agent ne voit pas la conversation. Donner l'objectif, les fichiers et docs concernés, les contraintes (stdlib Python 3.11, sortie déterministe, diagnostics plutôt qu'exceptions, un test de régression par bug, aucune écriture dans le répertoire du skill) et ce qu'il doit renvoyer.

Ne créer ni PR ni push sur la branche par défaut sans demande explicite. Avant de conclure : skill `done-checklist`.
