# Task template

Every groomed issue uses these sections, in this order.

## Goal

One or two sentences: what outcome this task produces, stated so it's
obvious whether the task is done.

## Context

Why this task exists — what prompted it, what already exists that's
relevant, links to the code paths involved. Enough for an engineer who has
never spoken to the PM to understand the starting point.

## Acceptance criteria

A checklist. Each item must be checkable by looking at the result (running
the app, reading a diff, running a test) — not a restatement of the goal.
Include the edge cases the original filer didn't consider.

## Out of scope

Anything that could reasonably be read into this task but isn't included.
If something was moved out during grooming, link the follow-up issue it was
filed as.

## Constraints

Any limits on the implementation: files it should stay within, dependencies
it must not add, behavior it must not change, conventions it must follow.
