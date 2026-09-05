# Accessibility user-test protocol

Automated checks are a precondition, not a claim of WCAG conformance. This
protocol must be completed with people who have relevant disabilities before
FrontDesk is described as validated by real users.

## Participants and consent

- Recruit participants representing keyboard-only use, screen-reader use, low
  vision or magnification, and cognitive or motor access needs relevant to the
  intended customer population.
- Obtain informed consent, explain recording and compensation, and avoid
  collecting unnecessary health details.
- Record only participant codes, assistive technology, browser, operating system
  and test-language combination. Keep identity and payment records separately.

## Tasks

Each participant completes the English or US-Spanish flow that matches their
language: open the chat, switch language, understand the sensitive-data warning,
send a question, detect the response, correct a message, review the privacy page,
recover from a simulated service error, and request human help.

Observers record completion, assistance, time, errors, comments and impact.
Classify issues as blocker, critical, major or minor; do not average away a
blocker. Retest every blocker and critical issue with the affected access method.

## Acceptance record

The signed record must include participant count and coverage, consent reference,
environment and build hash, tasks and outcomes, issue IDs, fixes, retest results,
unresolved risks, facilitator and approval date. Publication claims must state
the actual coverage and must not generalise beyond it.

No automated agent may invent participants, consent, observations or approval.
