# Publishing keyless-research-mcp

Two ways. Trusted Publishing is the one to use; the token path exists for a first manual
release.

---

## Option A — Trusted Publishing (no token, recommended)

GitHub mints a short-lived OIDC token that PyPI verifies. Nothing secret is stored anywhere.

**One-time setup on PyPI:**

1. Sign in at <https://pypi.org>.
2. *Your projects → Publishing → Add a new pending publisher* (for a project that does not
   exist yet, this is under *Account → Publishing*).
3. Fill in:

   | Field | Value |
   |---|---|
   | PyPI project name | `keyless-research-mcp` |
   | Owner | `EgiStr` |
   | Repository name | `keyless-research-mcp` (after you rename the repo, see below) |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

**Then, for every release:**

```bash
# 1. bump the version in pyproject.toml, then:
git add -A && git commit -m "chore: release 0.1.0"
git push

# 2. rehearse the build and the test gate without publishing
gh workflow run release.yml

# 3. publish
git tag v0.1.0
git push origin v0.1.0
```

Watch it: `gh run watch`. The workflow refuses to publish if tests, lint, or the
wheel-contents check fail — PyPI will not let you re-upload a version, so a bad release can
only be superseded, never withdrawn.

### Repository name

The GitHub repo is `EgiStr/keyless-research-mcp`, matching the package name, so the
trusted-publisher fields and the URLs in `pyproject.toml` all agree. Previously it was named
`academic-research-mcp`, before that name turned out to be taken on PyPI; the rename means
those URLs had to be updated in the same commit as the package rename, which they were.

---

## Option B — API token (manual, or as a one-off)

1. PyPI → *Account settings → API tokens → Add API token*.
   - Scope it to **project: keyless-research-mcp** if the project already exists; for the first
     upload it must be an account-wide token.
2. Set it **in your own shell**, not in any file that is committed:

   ```powershell
   $env:UV_PUBLISH_TOKEN = "pypi-..."
   ```

   In bash: `export UV_PUBLISH_TOKEN="pypi-..."`

3. Build and publish:

   ```bash
   uv build
   uv publish
   ```

4. Clear it afterwards: `Remove-Item Env:\UV_PUBLISH_TOKEN`

### Do not paste the token into a chat window

A token pasted into a conversation must be treated as leaked — transcripts are stored. If one
ends up there, revoke it at PyPI → *Account settings → API tokens* and mint a new one. The
token is also not needed to *build* or to *test*: only to upload.

---

## Verifying the result

```bash
# the package appears, usually within a minute
uvx keyless-research-mcp --help

# and from a clean install, in a throwaway dir
uv run --with keyless-research-mcp python -c "import keyless_research_mcp; print(keyless_research_mcp.__version__)"
```

Then point an MCP host at it:

```json
{ "mcpServers": { "keyless-research": { "command": "uvx", "args": ["keyless-research-mcp"] } } }
```
