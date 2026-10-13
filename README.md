# shopxz-japan-launch

Agent skill for planning, configuring and verifying Japanese B2C launches on ShopXZ / Fecify stores.

- Store settings (basic, checkout, country, tasks, captcha, payment) with read-back verification
- Japanese system email templates (15 types) with yen-first amounts and Japanese name/address order
- Enhanced cart recall (v1.3): 1h / 24h / 72h / 7d sequence, payment-pending (konbini / bank transfer) reminders,
  in-place migration of an existing recall activity, and reuse of existing coupons
- Journaled apply / resume, read-only inspect and verify, HTML + JSON reports

Requires the `fecify-site-manager-v6` skill for all store API calls (auto-discovered next to this skill,
in `~/.claude/skills`, `~/.codex/skills`, or via `--manager-skill` / `FECIFY_SKILL_DIR`). Python 3.10+, Node 18+.

```text
python scripts/launch.py --help
python scripts/launch.py validate-assets
```

See [SKILL.md](SKILL.md) for the workflow and `references/` for the detailed rules.
No credentials, store snapshots or customer data are stored in this repository.
