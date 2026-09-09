- Tasks are GitHub issues, one at a time
- Read the acceptance criteria before starting and before closing
- Commit regularly


Roles
- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer - implements one groomed task, follows _docs/team/software-engineer.md
- QA - checks the result against the acceptance criteria, follows _docs/team/qa-engineer.md
- UI/UX expert - checks UI work against the design intent and usability, follows _docs/team/ui_ux.md


Orchestrator

The main session is the orchestrator. It launches the PM, the engineer,
QA and the UI/UX expert as subagents. It does not groom, implement, test
or review itself.

Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it
3. Engineer implements it
4. QA verifies it against the acceptance criteria
5. UI/UX expert reviews it (only if the issue touches the UI; skip for
   backend-only issues)
6. On any FAIL, back to step 3 with the failing QA / UX comment as input
7. On PASS from every reviewer that ran, close the issue
8. Repeat until the backlog is empty

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA and the UI/UX expert do not fix the code, only output PASS or FAIL
- The orchestrator closes the issue only after every reviewer that ran
  has output PASS