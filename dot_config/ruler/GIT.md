# Git Rules for AI Coding Agents

## Commit Message Standards

### Always Sign Commits
- **ALWAYS** use `git commit -s` to sign commits
- This adds a "Signed-off-by" line indicating you have the right to submit the code
- Essential for legal compliance and accountability in professional environments

### Never add co-authors / attributions for AI agents

Examples of what NOT to add:
- 🤖 Generated with [Claude Code](https://claude.ai/code)
- Co-Authored-By: Claude <noreply@anthropic.com>

### Commit Message Format (Conventional Commits)
Follow the Conventional Commits specification for all commit messages:

```
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```
#### Required Commit Types
- `feat`: A new feature for the user
- `fix`: A bug fix for the user
- `docs`: Documentation changes only
- `style`: Code style changes (formatting, missing semicolons, etc.)
- `refactor`: Code changes that neither fix bugs nor add features
- `perf`: Performance improvements
- `test`: Adding or updating tests
- `build`: Changes to build system or dependencies
- `ci`: Changes to CI/CD configuration files and scripts
- `chore`: Other changes that don't modify src or test files

#### Breaking Changes
- Use `!` after the type/scope to indicate breaking changes: `feat!: remove deprecated API`
- Add `BREAKING CHANGE:` in the footer with description

### Message Content Guidelines
- **Summary line**: Keep to 50 characters or less
- **Body**: Wrap at 72 characters per line
- **Length**: 1-8 lines for typical commits, but larger changesets can grow as needed
- **Clarity**: Distill down to relevant points - explain WHAT and WHY, not HOW
- **Imperative mood**: Use imperative mood ("Add feature" not "Added feature")
- **No trailing periods**: Don't end the summary line with a period

#### Examples of Good Commit Messages
```bash
feat(auth): add password reset functionality

- Implement email-based password reset flow
- Add rate limiting to prevent abuse
- Include email templates for reset notifications

Closes #123
```

```bash
fix(api): resolve timeout issue in user endpoint

The user endpoint was timing out for requests with large datasets.
Optimized the database query and added proper indexing.

fix #456
```

```bash
docs: update installation instructions

Add missing prerequisites and clarify environment setup steps.
```

## Push and Branch Management

### Explicit Push Commands
- **ALWAYS** be explicit when pushing: `git push origin <branch-name>`
- **NEVER** use `git push` without specifying remote and branch
- This prevents accidental pushes to wrong branches or remotes
- Use `git push -u origin <branch-name>` for first push to set upstream

### Branch Naming Conventions
Use descriptive, categorized branch names following this pattern:
```
<type>/<issue-number>-<short-description>
```

#### Branch Types
- `feature/` - New features
- `fix/` or `bugfix/` - Bug fixes  
- `hotfix/` - Critical fixes for production
- `release/` - Release preparation
- `experiment/` - Experimental work
- `docs/` - Documentation updates
- `refactor/` - Code refactoring

#### Examples
```bash
feature/123-user-authentication
fix/456-login-timeout-error
hotfix/789-critical-security-patch
docs/update-api-documentation
```

### Branch Protection Rules
- **NEVER** commit directly to `main`, `master`, or `production` branches
- Use pull requests for all changes to protected branches
- Ensure branch names follow the naming convention before creating

## File Management and Staging

### Selective File Addition
- **ONLY** add files that are directly related to the current work context
- **ASK FOR FEEDBACK** if unsure whether a file should be included in a commit
- Use `git add <specific-files>` instead of `git add .` or `git add -A`
- Review staged files with `git status` before committing

### Files to Never Commit
- Secrets, passwords, API keys, or tokens
- Personal configuration files (IDE settings, OS-specific files)
- Build artifacts and compiled binaries
- Large binary files (use Git LFS if needed)
- Temporary files, logs, or cache files
- Environment-specific configuration (unless explicitly needed)

### Pre-commit Validation
Before any commit, verify:
- [ ] All staged files are intentionally included
- [ ] No secrets or sensitive data in staged files
- [ ] Code compiles and tests pass
- [ ] Formatting and linting rules are satisfied
- [ ] Commit message follows the conventional format

## Security and Safety

### Commit Content Security
- **NEVER** include passwords, API keys, database credentials, or other secrets
- **ALWAYS** use environment variables or secure vaults for sensitive data
- Scan commit content for patterns that look like secrets before committing
- Use tools like `git-secrets` or `detect-secrets` in pre-commit hooks

### Force Push Restrictions
- **AVOID** force pushing (`git push --force`) unless absolutely necessary
- **PREFER** `git push --force-with-lease` if force push is required
- **NEVER** force push to shared branches (main, develop, etc.)
- Always communicate with team before force pushing to any shared branch

### Backup and Recovery
- Keep local commits small and frequent for easier rollback
- Use `git stash` for temporary work that shouldn't be committed yet
- Create backup branches before risky operations: `git branch backup-<feature-name>`

## Workflow Integration

### Pre-commit Hooks
Set up automated checks that run before each commit:
- Code formatting (prettier, rustfmt, black, etc.)
- Linting (eslint, clippy, flake8, etc.)
- Test execution (unit tests at minimum)
- Secret scanning
- Commit message validation
- Branch name validation

### Multi-instance Coordination
When working with multiple AI coding agents (using git worktrees):
- Each worktree should have its own focused branch
- Coordinate commits to avoid conflicts between agents
- Use descriptive commit messages that identify which agent/context made the change
- Consider prefixing commits with context: `feat(agent-api): add user endpoints`

### Merge Strategy
- **PREFER** squash merging for feature branches to keep history clean
- Use meaningful merge commit messages when doing regular merges
- Always delete feature branches after merging
- Keep the main branch history linear and readable

## Git Configuration Best Practices

### Required Git Configuration
```bash
# Set up commit signing
git config --global user.signingkey <your-gpg-key-id>
git config --global commit.gpgsign true

# Set up consistent line endings
git config --global core.autocrlf input  # On Unix/Mac
git config --global core.autocrlf true   # On Windows

# Set up default push behavior
git config --global push.default simple

# Set up default pull behavior
git config --global pull.rebase true
```

### Recommended Aliases
```bash
git config --global alias.co checkout
git config --global alias.br branch
git config --global alias.ci "commit -s"  # Always sign commits
git config --global alias.st status
git config --global alias.unstage "reset HEAD --"
git config --global alias.last "log -1 HEAD"
git config --global alias.visual "!gitk"
git config --global alias.graph "log --oneline --graph --decorate --all"
```

## AI Agent Specific Guidelines

### Context Awareness
- When working within a specific context (feature, bug fix, etc.), only modify files related to that context
- If you need to make changes outside the current context, ask the user first
- Include context in commit messages: `feat(user-auth): add login validation`

### Change Verification
Before staging any files, the AI agent should:
1. **Explain** what files will be added and why
2. **Confirm** that all changes are intentional and related
3. **Ask for approval** if there's any uncertainty about including a file
4. **Validate** that no sensitive information is being committed

### Error Recovery
If a commit is made in error:
- For the last commit: Use `git reset --soft HEAD~1` to undo commit but keep changes
- For pushed commits: Create a revert commit instead of force pushing
- Always explain the recovery process to the user

## Repository Hygiene

### Regular Maintenance
- Clean up merged branches: `git branch -d <branch-name>`
- Remove tracking branches for deleted remotes: `git remote prune origin`
- Compact repository occasionally: `git gc`
- Verify repository integrity: `git fsck`

### Commit Frequency
- Make small, focused commits rather than large omnibus commits
- Each commit should represent a single logical change
- Commit working code frequently to avoid losing work
- Use WIP (Work In Progress) commits if needed, but clean them up before pushing

### History Management
- Keep commit history clean and meaningful
- Squash related commits before merging feature branches
- Use interactive rebase to clean up commit history: `git rebase -i`
- Write commit messages for future maintainers, not just current context

## Emergency Procedures

### Accidental Secret Commit
If secrets are accidentally committed:
1. **Immediately** revoke/rotate the exposed secret
2. Use `git filter-branch` or `BFG Repo-Cleaner` to remove from history
3. Force push the cleaned history (coordinate with team)
4. Notify all team members to re-clone the repository

### Recovery Commands
```bash
# Undo last commit (keep changes)
git reset --soft HEAD~1

# Undo last commit (discard changes) - DANGEROUS
git reset --hard HEAD~1

# Recover deleted branch (if you know the commit hash)
git checkout -b <branch-name> <commit-hash>

# Find lost commits
git reflog

# Revert a pushed commit
git revert <commit-hash>
```

## Integration with Development Tools

### IDE Integration
- Configure IDE to show git status in file explorer
- Set up automatic formatting on save to reduce style commits
- Use git blame and history features to understand code changes
- Enable pre-commit hook integration in IDE

### CI/CD Integration
- Trigger builds on all pushes to feature branches
- Run full test suites on pull requests
- Enforce commit message standards in CI
- Block merges if tests fail or security scans find issues

### Monitoring and Alerts
- Set up alerts for force pushes to protected branches
- Monitor for large commits that might indicate binary files
- Track commit frequency and patterns for team health metrics
- Alert on commits that bypass pre-commit hooks

---

## Quick Reference Checklist

Before every commit, verify:
- [ ] Commit is signed with `-s` flag
- [ ] Message follows conventional commit format
- [ ] Only relevant files are staged
- [ ] No secrets or sensitive data included
- [ ] Code compiles and tests pass
- [ ] Working in appropriate branch (not main/master)
- [ ] Ready to push with explicit remote/branch specification

## Common Git Commands for AI Agents

```bash
# Create and switch to new branch
git checkout -b feature/123-new-feature

# Stage specific files only
git add src/auth.rs src/user.rs

# Commit with signature and conventional message
git commit -s -m "feat(auth): add user authentication system"

# Push explicitly to remote
git push origin feature/123-new-feature

# Check status before actions
git status

# View staged changes
git diff --staged

# View commit history
git log --oneline -10
```

Remember: These rules prioritize **safety**, **clarity**, and **collaboration**. When in doubt, ask the user for guidance rather than making assumptions about what should be committed or how changes should be organized.
