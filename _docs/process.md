- Tasks are GitHub issues, one at a time
- Read the acceptance criteria before starting and before closing
- Commit regularly


Roles
- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer - implements one groomed task, follows _docs/team/software-engineer.md
- UI/UX expert - checks the results against the design intent, follows _docs/team/ui_ux.md
- QA - checks the result against the acceptance criteria, follows _docs/team/qa-engineer.md


Orchestrator

The main session is the orchestrator. It launches the PM, the engineer
and QA as subagents. It does not groom, implement or test itself.

Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it
3. Engineer implements it
4. UI/UX expert
5. QA verifies it
6. On FAIL, back to step 3 with the QA comment as input
7. On PASS, close the issue
8. Repeat until the backlog is empty

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- The orchestrator closes the issue only after QA outputs PASS