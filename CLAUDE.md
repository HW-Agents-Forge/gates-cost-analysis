# gatecost: développement avec Claude Code

L'agent par défaut de ce dépôt est `claude` (`.claude/settings.json`) : il orchestre les sous-agents de `.claude/agents/`. Les autres agents sont internes et ne se lancent pas directement. Pour forcer cet agent : `claude --agent claude`.

Conventions et définition de « done » : `CONTRIBUTING.md`. Spécifications : `docs/`. Skills de développement : `.claude/skills/` (le `SKILL.md` racine est le skill produit pour les utilisateurs de l'outil).
