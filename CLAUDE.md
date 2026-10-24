# gatecost: développement avec Claude Code

Conventions et définition de « done » : `CONTRIBUTING.md`. Spécifications : `docs/`. Skills de développement : `.claude/skills/` (le `SKILL.md` racine est le skill produit pour les utilisateurs de l'outil).

## Déléguer aux sous-agents (`.claude/agents/`)

**Règle impérative** : l'agent principal est un chef de projet. Dès qu'une demande touche un sous-système du tableau ci-dessous (lecture, analyse, revue, correction, test, doc, git), il lance l'agent correspondant avec l'outil Agent au lieu de lire ou modifier ce code lui-même. Il ne traite directement que : les questions sans lecture de code, un lookup d'une seule commande, et la synthèse finale. Les demandes d'analyse ou de proposition sans modification passent aussi par l'agent propriétaire (ex. « la formule de multiplication du coût est-elle correcte ? » → `cost-model-dev`).

Après une délégation : ne pas refaire le même travail en parallèle ; attendre le résultat de l'agent, puis relire ses références clés. Lancer en parallèle uniquement des tâches indépendantes (sous-systèmes ou fichiers distincts). Si la demande couvre plusieurs sous-systèmes, découper et déléguer chaque partie.

Chemin touché → agent : `frontend/` → `frontend-dev` · `elab/` → `elaborator-dev` · `ir/` → `ir-analysis-dev` · `cost/`, `profiles/`, `estimate*.py` → `cost-model-dev` · `diff/`, `report/`, `cli/`, `schema/` → `diff-report-dev` · `tests/` → `test-engineer` · `docs/`, `SKILL.md`, `README.md` → `docs-keeper`.


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
