# Trading-Agent repair workflow

Read `CLAUDE.md`, `docs/REPAIR_PLAN.md`, and the current checkpoint before implementation.
Latest explicit user decisions override older blueprint text. A planned policy is not
implemented merely because it appears in these documents.

## Work one numbered step at a time

1. Confirm the active step and base commit from the plan/checkpoint. Inspect local
   changes and upstream changes before editing. Preserve another contributor's work.
2. Before coding, record the requirement, its evidence label, affected producers and
   consumers, acceptance examples, unresolved decisions, and rollback in the step record.
3. Keep one implementer responsible for the step. Another agent may review or work
   on a specifically assigned independent package; do not assume delegation or send
   messages to other agents without user authorization. Do not edit shared interfaces
   concurrently. Neither model may silently invent a trading rule.
4. Use deterministic code for financial calculations, validation, sizing and orders.
   The restriction on runtime LLM decisions does not prohibit authorized developers
   from repairing and testing that code.
5. Implement the bounded change and update affected tests, cards, configuration and
   documentation together. Test successful behavior as well as rejection paths.
6. Run relevant checks; an integration change also needs the combined suite. Record
   actual commands and results. Distinguish automated tests, live-data checks,
   operational acceptance, and profitability observations.
7. Update the checklist and checkpoint with changed files, evidence, remaining
   limitations, rollback, and the next step. Stop at that checkpoint. Do not begin
   the next numbered step until Taz says to continue (unless he explicitly authorizes
   a batch of steps). Stop on an unresolved requirement rather than choosing it silently.

No live deployment, scheduled runner activation, broker orders or credentials are
needed for repair development. Webull remains market-data only. Never commit secrets.
Implementation completion does not authorize real-money activation.
